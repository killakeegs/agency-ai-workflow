#!/usr/bin/env python3
"""
migrate_client.py — Migrate a past client from Google Drive into Notion

Provisions the full Notion structure (4 base DBs + Business Profile + service DBs),
then scans the client's Google Drive folder, uses Claude to synthesize scattered
documents into structured client knowledge, and populates:
  - Client Info DB (contact, services, template, etc.)
  - Brand Guidelines DB (voice, colors, fonts, photography style, etc.)
  - Business Profile page (12 universal sections + vertical-specific)
  - Client Log DB (historical meeting notes if found)

Also adds the client to config/clients.json.

Usage:
    python scripts/onboarding/migrate_client.py \\
        --name "Summit Therapy" \\
        --services website_build care_plan seo \\
        --verticals speech_pathology occupational_therapy \\
        --drive-folder "https://drive.google.com/drive/folders/ABC123" \\
        --contact-email "sarah@summittherapy.com"

Dry run (doesn't write to Notion):
    python scripts/onboarding/migrate_client.py ... --dry-run

Requires:
    GOOGLE_GMAIL_REFRESH_TOKEN with drive.readonly + documents.readonly scopes.
    Re-run: python scripts/setup/google_auth.py --gmail
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import httpx
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Load .env into os.environ so Google OAuth token lookups via os.environ.get() work
load_dotenv(Path(__file__).parent.parent.parent / ".env")

import anthropic

from src.config import settings
from src.integrations.notion import NotionClient
from scripts.onboarding.setup_notion import (
    setup_client as notion_setup,
    UNIVERSAL_SECTIONS,
    VERTICAL_SECTIONS,
)


CLIENTS_JSON_PATH = Path(__file__).parent.parent.parent / "config" / "clients.json"

# ── Google Drive helpers ──────────────────────────────────────────────────────

async def _get_drive_access_token() -> str:
    """Exchange the Gmail refresh token for a Drive-scoped access token."""
    client_id     = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    refresh_token = os.environ.get("GOOGLE_GMAIL_REFRESH_TOKEN", "").strip()

    if not all([client_id, client_secret, refresh_token]):
        raise ValueError(
            "Missing Google OAuth credentials. Run: "
            "python scripts/setup/google_auth.py --gmail"
        )

    async with httpx.AsyncClient() as http:
        r = await http.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id":     client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type":    "refresh_token",
            },
        )
    r.raise_for_status()
    return r.json()["access_token"]


def _extract_folder_id(drive_url: str) -> str:
    """Extract Google Drive folder ID from a share URL."""
    # Formats:
    # https://drive.google.com/drive/folders/ABC123
    # https://drive.google.com/drive/u/0/folders/ABC123
    # https://drive.google.com/drive/folders/ABC123?usp=sharing
    m = re.search(r"/folders/([a-zA-Z0-9_-]+)", drive_url)
    if m:
        return m.group(1)
    # Plain folder ID?
    if re.match(r"^[a-zA-Z0-9_-]+$", drive_url):
        return drive_url
    raise ValueError(f"Could not extract folder ID from: {drive_url}")


async def _list_drive_files(
    http: httpx.AsyncClient,
    access_token: str,
    folder_id: str,
    depth: int = 0,
    max_depth: int = 3,
) -> list[dict]:
    """
    Recursively list files in a Drive folder.
    Returns: list of {id, name, mimeType, path, modifiedTime}
    """
    if depth > max_depth:
        return []

    # Query: files in this folder, not trashed
    query = f"'{folder_id}' in parents and trashed = false"
    params = {
        "q": query,
        "fields": "files(id,name,mimeType,modifiedTime),nextPageToken",
        "pageSize": 200,
    }

    files = []
    page_token = None
    while True:
        if page_token:
            params["pageToken"] = page_token

        r = await http.get(
            "https://www.googleapis.com/drive/v3/files",
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()

        for f in data.get("files", []):
            files.append(f)
            # Recurse into subfolders
            if f["mimeType"] == "application/vnd.google-apps.folder":
                sub_files = await _list_drive_files(
                    http, access_token, f["id"], depth + 1, max_depth
                )
                for sf in sub_files:
                    sf["path"] = f"{f['name']}/{sf.get('path', sf['name'])}"
                files.extend(sub_files)

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return files


async def _read_file_content(
    http: httpx.AsyncClient,
    access_token: str,
    file_id: str,
    mime_type: str,
) -> str:
    """Read content of a Google Drive file. Returns plain text."""
    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        if mime_type == "application/vnd.google-apps.document":
            # Export Google Doc as plain text
            r = await http.get(
                f"https://www.googleapis.com/drive/v3/files/{file_id}/export",
                headers=headers,
                params={"mimeType": "text/plain"},
                timeout=30,
            )
            r.raise_for_status()
            return r.text
        elif mime_type == "application/vnd.google-apps.spreadsheet":
            # Export Sheet as CSV
            r = await http.get(
                f"https://www.googleapis.com/drive/v3/files/{file_id}/export",
                headers=headers,
                params={"mimeType": "text/csv"},
                timeout=30,
            )
            r.raise_for_status()
            return r.text
        elif mime_type in ("text/plain", "text/markdown"):
            r = await http.get(
                f"https://www.googleapis.com/drive/v3/files/{file_id}",
                headers=headers,
                params={"alt": "media"},
                timeout=30,
            )
            r.raise_for_status()
            return r.text
        else:
            # Skip binary files (images, PDFs, etc.) for now
            return ""
    except Exception as e:
        print(f"    ⚠ Could not read file {file_id}: {e}")
        return ""


# ── Claude synthesis ──────────────────────────────────────────────────────────

SYNTHESIS_SYSTEM = """\
You are the RxMedia client migration agent. You read scattered documents from a
client's Google Drive and synthesize them into structured data for RxMedia's
Notion-based client knowledge system.

You are thorough, specific, and candid. Do not invent information that isn't
in the source material. If a field can't be filled from the docs, leave it empty.
"""

def _build_synthesis_prompt(
    client_name: str,
    services: list[str],
    verticals: list[str],
    docs_content: str,
) -> str:
    universal_section_list = "\n".join(
        f"- {name}: {prompt}" for name, prompt in UNIVERSAL_SECTIONS
    )

    vertical_sections_list = ""
    for v in verticals:
        sections = VERTICAL_SECTIONS.get(v, [])
        if sections:
            vertical_sections_list += f"\n{v.replace('_', ' ').title()}:\n"
            vertical_sections_list += "\n".join(
                f"  - {name}: {prompt}" for name, prompt in sections
            )

    return f"""\
Synthesize the client's Google Drive documents into RxMedia's Notion knowledge structure.

Client: {client_name}
Services: {', '.join(services)}
Verticals: {', '.join(verticals) or '(none)'}

DOCUMENTS FROM GOOGLE DRIVE:
{docs_content[:80000]}

Return ONLY this JSON (no markdown):
{{
  "client_info": {{
    "company": "full company name",
    "email": "primary email",
    "phone": "phone number",
    "website": "current website URL",
    "primary_contact_name": "name of day-to-day contact",
    "primary_contact_email": "that contact's email",
    "client_contacts": "comma-separated list of all known client email addresses",
    "business_type": "Local Business | National Business | E-Commerce | Service Business | SaaS / Tech",
    "vertical": "comma-separated vertical keys from: addiction_treatment, speech_pathology, occupational_therapy, physical_therapy, dermatology, mental_health",
    "monthly_retainer": <number or null>,
    "account_manager": "name if mentioned",
    "project_start": "YYYY-MM-DD or empty",
    "notes": "anything else worth knowing"
  }},
  "brand_guidelines": {{
    "voice_tone": "how the client talks about themselves",
    "primary_color": "hex or name",
    "secondary_color": "",
    "accent_color": "",
    "primary_font": "",
    "secondary_font": "",
    "tone_descriptors": "3-5 adjectives",
    "power_words": "words to lean into",
    "words_to_avoid": "words/phrases to avoid",
    "cta_style": "how CTAs should sound",
    "photography_style": "description of preferred imagery",
    "image_direction": "any visual direction notes",
    "blog_voice": "blog writing voice if mentioned",
    "blog_reviewer_name": "medical reviewer name if mentioned",
    "blog_reviewer_credentials": "reviewer credentials",
    "blog_reviewer_bio": "reviewer bio (2-3 sentences)",
    "raw_guidelines": "any raw brand content worth preserving"
  }},
  "business_profile": {{
    "universal_sections": {{
      "Company Credentials & Accreditations": "content from docs or empty",
      "Specialized Populations": "",
      "Staffing & Team": "",
      "Services Overview": "",
      "Insurance & Payment": "",
      "Admissions & Intake": "",
      "Facility & Environment": "",
      "Outcomes & Results": "",
      "Referral Network": "",
      "Compliance & Legal": "",
      "Tech Stack": "",
      "Common Objections & FAQs": ""
    }},
    "vertical_sections": {{
      "section_name": "content"
    }}
  }},
  "historical_meetings": [
    {{
      "date": "YYYY-MM-DD",
      "meeting_type": "Kickoff | Pipeline Review | Content Review | Design Review | Check-in | Ad Hoc",
      "attendees": "comma-separated names",
      "summary": "2-3 sentences",
      "key_decisions": "what was decided",
      "action_items": "what was agreed on",
      "source_doc": "original doc title"
    }}
  ],
  "confidence": "high | medium | low - how much of this came from actual docs vs inference"
}}

UNIVERSAL SECTIONS (fill these with content from the docs):
{universal_section_list}

VERTICAL-SPECIFIC SECTIONS:
{vertical_sections_list or '(none for these verticals)'}

Rules:
- Only use information actually in the documents. Do not invent.
- For fields with no source data, return empty string "" (not null, not "unknown").
- Meeting notes: extract up to 20 most recent/relevant meetings. Focus on substance.
- Be specific and detailed where source data supports it.
"""


async def _synthesize_with_claude(
    client_name: str,
    services: list[str],
    verticals: list[str],
    docs_content: str,
) -> dict:
    """Use Claude to synthesize docs into structured client data."""
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    response = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=12000,
        system=SYNTHESIS_SYSTEM,
        messages=[{
            "role": "user",
            "content": _build_synthesis_prompt(client_name, services, verticals, docs_content),
        }],
    )

    raw = response.content[0].text.strip()
    # Extract JSON
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if not match:
        raise ValueError(f"Could not parse synthesis JSON from Claude")

    return json.loads(match.group(0))


# ── Notion population ────────────────────────────────────────────────────────

def _rt(text: str) -> dict:
    if not text:
        return {"rich_text": []}
    return {"rich_text": [{"text": {"content": text[:2000]}}]}


async def _populate_client_info(
    notion: NotionClient,
    db_id: str,
    data: dict,
    services: list[str],
    verticals: list[str],
) -> None:
    """Populate the Client Info DB with synthesized data."""
    rows = await notion._client.request(
        path=f"databases/{db_id}/query", method="POST", body={"page_size": 1}
    )
    if not rows.get("results"):
        return
    entry_id = rows["results"][0]["id"]

    # Build services multi-select from active services
    service_labels = {
        "website_build":    "Website Build",
        "care_plan":        "Care Plan",
        "seo":              "SEO",
        "gbp_management":   "GBP Management",
        "blog":             "Blog",
        "social_media":     "Social Media",
        "newsletter":       "Newsletter",
        "paid_ads":         "Paid Ads",
    }
    services_multi = [
        {"name": service_labels[s]} for s in services if s in service_labels
    ]

    props: dict = {
        "Company": _rt(data.get("company", "")),
        "Primary Contact Name": _rt(data.get("primary_contact_name", "")),
        "Client Contacts": _rt(data.get("client_contacts", "")),
        "Vertical": _rt(", ".join(verticals)),
        "Account Manager": _rt(data.get("account_manager", "")),
        "Notes": _rt(data.get("notes", "")),
    }
    if services_multi:
        props["Services"] = {"multi_select": services_multi}
    if data.get("email"):
        props["Email"] = {"email": data["email"]}
    if data.get("primary_contact_email"):
        props["Primary Contact Email"] = {"email": data["primary_contact_email"]}
    if data.get("phone"):
        props["Phone"] = {"phone_number": data["phone"]}
    if data.get("website"):
        props["Website"] = {"url": data["website"]}
    if data.get("business_type"):
        props["Business Type"] = {"select": {"name": data["business_type"]}}
    if data.get("monthly_retainer"):
        try:
            props["Monthly Retainer"] = {"number": float(data["monthly_retainer"])}
        except (ValueError, TypeError):
            pass
    if data.get("project_start"):
        props["Project Start"] = {"date": {"start": data["project_start"]}}

    await notion.update_database_entry(entry_id, props)


async def _populate_brand_guidelines(
    notion: NotionClient,
    db_id: str,
    data: dict,
    client_name: str,
) -> None:
    """Create a Brand Guidelines entry with synthesized data."""
    props = {
        "Name": {"title": [{"text": {"content": f"{client_name} Brand Guidelines"}}]},
        "Primary Color":     _rt(data.get("primary_color", "")),
        "Secondary Color":   _rt(data.get("secondary_color", "")),
        "Accent Color":      _rt(data.get("accent_color", "")),
        "Primary Font":      _rt(data.get("primary_font", "")),
        "Secondary Font":    _rt(data.get("secondary_font", "")),
        "Tone Descriptors":  _rt(data.get("tone_descriptors", "")),
        "Voice & Tone":      _rt(data.get("voice_tone", "")),
        "Power Words":       _rt(data.get("power_words", "")),
        "Words to Avoid":    _rt(data.get("words_to_avoid", "")),
        "CTA Style":         _rt(data.get("cta_style", "")),
        "Photography Style": _rt(data.get("photography_style", "")),
        "Image Direction":   _rt(data.get("image_direction", "")),
        "Blog Voice":        _rt(data.get("blog_voice", "")),
        "Blog Reviewer Name":        _rt(data.get("blog_reviewer_name", "")),
        "Blog Reviewer Credentials": _rt(data.get("blog_reviewer_credentials", "")),
        "Blog Reviewer Bio":         _rt(data.get("blog_reviewer_bio", "")),
        "Raw Guidelines":            _rt(data.get("raw_guidelines", "")),
    }
    await notion.create_database_entry(db_id, props)


async def _populate_business_profile(
    notion: NotionClient,
    page_id: str,
    data: dict,
) -> None:
    """
    Update the Business Profile page — find each section heading and append
    content below it. The page was pre-built with empty sections by setup_notion.
    """
    universal = data.get("universal_sections", {}) or {}
    vertical  = data.get("vertical_sections", {}) or {}

    # Get existing blocks on the page
    resp = await notion._client.request(
        path=f"blocks/{page_id}/children",
        method="GET",
    )
    blocks = resp.get("results", [])

    # Find each H2 section and the empty paragraph placeholder below it.
    # Update the paragraph with synthesized content.
    current_section = None
    for i, block in enumerate(blocks):
        btype = block.get("type")
        if btype == "heading_2":
            rich = block["heading_2"].get("rich_text", [])
            current_section = "".join(r.get("text", {}).get("content", "") for r in rich)
        elif btype == "paragraph" and current_section:
            rich = block["paragraph"].get("rich_text", [])
            existing_text = "".join(r.get("text", {}).get("content", "") for r in rich)
            if existing_text:
                continue  # already populated, skip
            # Find content for this section
            content = universal.get(current_section, "") or vertical.get(current_section, "")
            if content:
                await notion._client.request(
                    path=f"blocks/{block['id']}",
                    method="PATCH",
                    body={
                        "paragraph": {
                            "rich_text": [{"text": {"content": content[:1900]}}]
                        }
                    },
                )
            current_section = None  # only fill the first paragraph after each section


async def _add_to_clients_db(
    notion: NotionClient,
    clients_db_id: str,
    client_name: str,
    client_page_id: str,
    services: list[str],
    verticals: list[str],
    data: dict,
) -> None:
    """Add a row to the top-level Clients DB (master command center)."""
    service_labels = {
        "website_build":  "Website Build",
        "care_plan":      "Care Plan",
        "seo":            "SEO",
        "gbp_management": "GBP Management",
        "blog":           "Blog",
        "social_media":   "Social Media",
        "newsletter":     "Newsletter",
        "paid_ads":       "Paid Ads",
    }
    services_multi = [
        {"name": service_labels[s]} for s in services if s in service_labels
    ]

    props = {
        "Client Name":  {"title": [{"text": {"content": client_name}}]},
        "Status":       {"select": {"name": "Active"}},
        "Services":     {"multi_select": services_multi} if services_multi else {"multi_select": []},
        "Vertical":     _rt(", ".join(verticals)),
        "Account Manager": _rt(data.get("account_manager", "")),
        "Primary Contact": _rt(data.get("primary_contact_name", "")),
        "Client Page":  {"url": f"https://notion.so/{client_page_id.replace('-', '')}"},
        "Notes":        _rt(f"Migrated from Google Drive {datetime.now().strftime('%Y-%m-%d')}."),
    }
    if data.get("primary_contact_email"):
        props["Contact Email"] = {"email": data["primary_contact_email"]}
    if data.get("monthly_retainer"):
        try:
            props["Monthly Retainer"] = {"number": float(data["monthly_retainer"])}
        except (ValueError, TypeError):
            pass

    await notion._client.request(
        path="pages",
        method="POST",
        body={"parent": {"database_id": clients_db_id}, "properties": props},
    )


async def _populate_client_log(
    notion: NotionClient,
    db_id: str,
    client_name: str,
    meetings: list[dict],
) -> int:
    """Create Client Log entries from historical meeting notes."""
    created = 0
    for m in meetings:
        date_str = m.get("date", "")
        if not date_str:
            continue
        try:
            datetime.strptime(date_str, "%Y-%m-%d")  # validate
        except ValueError:
            continue

        props = {
            "Title":         {"title": [{"text": {"content": f"{client_name} — {m.get('meeting_type', 'Meeting')} — {date_str}"}}]},
            "Date":          {"date": {"start": date_str}},
            "Type":          {"select": {"name": "Meeting"}},
            "Meeting Type":  {"select": {"name": m.get("meeting_type", "Ad Hoc")}},
            "Attendees":     _rt(m.get("attendees", "")),
            "Summary":       _rt(m.get("summary", "")),
            "Key Decisions": _rt(m.get("key_decisions", "")),
            "Action Items":  _rt(m.get("action_items", "")),
            "Processed":     {"checkbox": True},
            "Source":        _rt(f"Migrated from: {m.get('source_doc', 'Google Drive')}"),
        }
        try:
            await notion._client.request(
                path="pages",
                method="POST",
                body={"parent": {"database_id": db_id}, "properties": props},
            )
            created += 1
        except Exception as e:
            print(f"    ⚠ Could not create log entry for {date_str}: {e}")

    return created


# ── clients.json ──────────────────────────────────────────────────────────────

def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower().strip())
    return s.strip("_")


def _update_clients_json(
    client_key: str,
    client_name: str,
    services: list[str],
    verticals: list[str],
    databases: dict,
    business_profile_id: str,
) -> None:
    """Add or update client entry in config/clients.json."""
    services_config = {
        "website_build":            "website_build" in services,
        "care_plan":                "care_plan" in services,
        "seo":                      "seo" in services,
        "gbp_management":           "gbp_management" in services,
        "gbp_posts_per_month":      8,
        "blog":                     "blog" in services,
        "blog_posts_per_month":     4 if "blog" in services else 0,
        "social_media":             "social_media" in services,
        "social_posts_per_month":   8,
        "linkedin_posts_per_month": 2,
        "newsletter":               "newsletter" in services,
        "paid_ads":                 "paid_ads" in services,
    }

    entry = {
        "client_id":                client_key,
        "name":                     client_name,
        "services":                 services_config,
        "vertical":                 verticals,
        "client_info_db_id":        databases.get("Client Info", ""),
        "client_log_db_id":         databases.get("Client Log", ""),
        "brand_guidelines_db_id":   databases.get("Brand Guidelines", ""),
        "care_plan_db_id":          databases.get("Care Plan", ""),
        "business_profile_page_id": business_profile_id,
        "sitemap_db_id":            databases.get("Sitemap", ""),
        "content_db_id":            databases.get("Page Content", ""),
        "images_db_id":             databases.get("Images", ""),
        "competitors_db_id":        databases.get("Competitors", ""),
        "keywords_db_id":           databases.get("Keywords", ""),
        "seo_metrics_db_id":        "",
        "gbp_posts_db_id":          "",
        "blog_posts_db_id":         "",
        "social_posts_db_id":       "",
        "gbp_location_id":          "",
        "clickup_review_list_id":   "",
        "migration_source":         "google_drive",
    }

    existing = {}
    if CLIENTS_JSON_PATH.exists():
        try:
            existing = json.loads(CLIENTS_JSON_PATH.read_text()) or {}
        except json.JSONDecodeError:
            existing = {}
    existing[client_key] = entry
    CLIENTS_JSON_PATH.write_text(json.dumps(existing, indent=4))


# ── Main ──────────────────────────────────────────────────────────────────────

async def migrate(
    client_name: str,
    services: list[str],
    verticals: list[str],
    drive_folder_url: str,
    contact_email: str = "",
    dry_run: bool = False,
    max_files: int = 100,
    from_json: str = "",
) -> None:
    print(f"\n{'='*60}")
    print(f"  Migrating: {client_name}")
    print(f"  Services:  {', '.join(services)}")
    print(f"  Verticals: {', '.join(verticals) or '(none)'}")
    print(f"{'='*60}\n")

    # If we have pre-synthesized JSON (from a prior dry run + manual edits),
    # skip the Drive scan and Claude synthesis — just load and proceed.
    if from_json:
        print(f"Loading pre-synthesized data from: {from_json}")
        synthesized = json.loads(Path(from_json).read_text())
        print(f"  Confidence: {synthesized.get('confidence', 'unknown')}")
        print(f"  Meetings: {len(synthesized.get('historical_meetings', []))}")

        if dry_run:
            print("\n[DRY RUN] --from-json + --dry-run is a no-op (data already synthesized)")
            return

        # Skip to Notion population
        await _provision_and_populate(
            client_name, services, verticals, contact_email, synthesized
        )
        return

    # 1. Extract folder ID + scan Drive
    folder_id = _extract_folder_id(drive_folder_url)
    print(f"Scanning Google Drive folder: {folder_id}")

    access_token = await _get_drive_access_token()

    async with httpx.AsyncClient() as http:
        files = await _list_drive_files(http, access_token, folder_id)
        print(f"  Found {len(files)} files")

        # Filter to readable documents + cap at max_files
        readable_types = {
            "application/vnd.google-apps.document",
            "application/vnd.google-apps.spreadsheet",
            "text/plain",
            "text/markdown",
        }
        readable = [f for f in files if f["mimeType"] in readable_types]
        # Prioritize by most recently modified
        readable.sort(key=lambda f: f.get("modifiedTime", ""), reverse=True)
        readable = readable[:max_files]
        print(f"  Reading {len(readable)} text documents...")

        # 2. Read each doc
        all_content = []
        for i, f in enumerate(readable):
            text = await _read_file_content(http, access_token, f["id"], f["mimeType"])
            if text:
                title = f.get("name", "Untitled")
                modified = f.get("modifiedTime", "")[:10]
                all_content.append(f"### {title} ({modified})\n\n{text[:8000]}")

        docs_content = "\n\n---\n\n".join(all_content)
        print(f"  Total content: {len(docs_content):,} chars from {len(all_content)} docs")

    # 3. Synthesize with Claude
    print("\nSynthesizing with Claude...")
    synthesized = await _synthesize_with_claude(
        client_name, services, verticals, docs_content
    )
    print(f"  Confidence: {synthesized.get('confidence', 'unknown')}")
    print(f"  Meetings extracted: {len(synthesized.get('historical_meetings', []))}")

    if dry_run:
        out = Path(f"/tmp/migrate_{_slug(client_name)}_data.json")
        out.write_text(json.dumps(synthesized, indent=2))
        print(f"\n[DRY RUN] Synthesized data saved to: {out}")
        print("  Edit this file manually if needed, then re-run with:")
        print(f"  --from-json {out} (skips Drive scan + Claude synthesis)")
        return

    await _provision_and_populate(
        client_name, services, verticals, contact_email, synthesized
    )


async def _provision_and_populate(
    client_name: str,
    services: list[str],
    verticals: list[str],
    contact_email: str,
    synthesized: dict,
) -> None:
    """Provision Notion structure + populate with synthesized data."""
    # Provision Notion structure
    print("\nProvisioning Notion structure...")
    setup_result = await notion_setup(
        client_name=client_name,
        contact_email=contact_email or synthesized.get("client_info", {}).get("email", ""),
        dry_run=False,
        services=services,
        verticals=verticals,
    )
    client_page_id      = setup_result["client_page_id"]
    business_profile_id = setup_result["business_profile_id"]
    databases           = setup_result["databases"]

    # Populate Notion
    print("\nPopulating Notion with synthesized data...")
    notion = NotionClient(settings.notion_api_key)

    print("  Populating Client Info...")
    await _populate_client_info(
        notion, databases["Client Info"], synthesized.get("client_info", {}),
        services, verticals,
    )

    print("  Populating Brand Guidelines...")
    await _populate_brand_guidelines(
        notion, databases["Brand Guidelines"], synthesized.get("brand_guidelines", {}),
        client_name,
    )

    print("  Populating Business Profile...")
    await _populate_business_profile(
        notion, business_profile_id, synthesized.get("business_profile", {}),
    )

    meetings = synthesized.get("historical_meetings", []) or []
    if meetings:
        print(f"  Creating {len(meetings)} Client Log entries from historical meetings...")
        created = await _populate_client_log(
            notion, databases["Client Log"], client_name, meetings,
        )
        print(f"    ✓ {created} entries created")

    # Add to top-level Clients DB (master command center)
    clients_db_id = os.environ.get("NOTION_CLIENTS_DB_ID", "").strip()
    if clients_db_id:
        print("  Adding row to top-level Clients DB...")
        try:
            await _add_to_clients_db(
                notion, clients_db_id, client_name, client_page_id,
                services, verticals, synthesized.get("client_info", {}),
            )
            print("    ✓ Added to Clients DB")
        except Exception as e:
            print(f"    ⚠ Could not add to Clients DB: {e}")
    else:
        print("  ⚠ NOTION_CLIENTS_DB_ID not set in .env — skipping Clients DB entry")

    # Update clients.json
    print("\nUpdating config/clients.json...")
    client_key = _slug(client_name)
    _update_clients_json(
        client_key, client_name, services, verticals, databases, business_profile_id,
    )
    print(f"  ✓ client_key: {client_key}")

    # Summary
    print(f"\n{'='*60}")
    print(f"  MIGRATION COMPLETE: {client_name}")
    print(f"{'='*60}")
    print(f"\nClient page: https://notion.so/{client_page_id.replace('-', '')}")
    print(f"Business Profile: https://notion.so/{business_profile_id.replace('-', '')}")
    print(f"\nAdd these DBs to your 3 Notion agents (Meeting Processor, Email Monitor, Daily Digest):")
    for name, db_id in databases.items():
        print(f"  {name}")
    print(f"\nNext: review the Business Profile page and fill in any gaps the agent missed.\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate a past client from Google Drive to Notion")
    parser.add_argument("--name",          required=True, help="Client/company name")
    parser.add_argument("--services",      nargs="+", required=True,
                        help="Active services (e.g. website_build care_plan seo blog social_media)")
    parser.add_argument("--verticals",     nargs="*", default=[],
                        help="Industry verticals (e.g. speech_pathology occupational_therapy)")
    parser.add_argument("--drive-folder",  default="", help="Google Drive folder URL or ID (not needed with --from-json)")
    parser.add_argument("--contact-email", default="", help="Primary contact email")
    parser.add_argument("--dry-run",       action="store_true",
                        help="Don't write to Notion — save synthesized data to /tmp for review")
    parser.add_argument("--max-files",     type=int, default=100,
                        help="Max files to scan from Drive (default 100)")
    parser.add_argument("--from-json",     default="",
                        help="Skip Drive scan + Claude synthesis; load pre-synthesized JSON file")
    args = parser.parse_args()

    if not args.from_json and not args.drive_folder:
        parser.error("Either --drive-folder or --from-json is required")

    asyncio.run(migrate(
        client_name=args.name,
        services=args.services,
        verticals=args.verticals,
        drive_folder_url=args.drive_folder,
        contact_email=args.contact_email,
        dry_run=args.dry_run,
        max_files=args.max_files,
        from_json=args.from_json,
    ))


if __name__ == "__main__":
    main()
