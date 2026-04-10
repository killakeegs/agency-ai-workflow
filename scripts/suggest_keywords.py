#!/usr/bin/env python3
"""
suggest_keywords.py — Use Claude to suggest target keywords for every page in the sitemap.

Adds "Primary Keyword" and "Secondary Keywords" fields to the Sitemap DB (one-time
schema update), then calls Claude once per batch of pages to suggest keywords based
on each page's title, slug, purpose, and key sections.

Team reviews and edits keywords directly in Notion before running `make content`.
ContentAgent reads these fields when generating page copy.

Usage:
    python scripts/suggest_keywords.py --client summit_therapy
    python scripts/suggest_keywords.py --client summit_therapy --force   # overwrite existing
"""
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import anthropic

from config.clients import CLIENTS
from src.config import settings
from src.integrations.notion import NotionClient

SYSTEM_PROMPT = """\
You are an SEO strategist specializing in healthcare and therapy practice websites.

Your task: suggest target keywords for each page of a therapy clinic website.

Rules:
- Primary Keyword: the single most important keyword for this page. Should be specific,
  have commercial/local intent, and be something real patients search. For local pages,
  include the city or region (e.g. "speech therapy Frisco TX"). For CMS subcategory
  pages, target the condition + treatment (e.g. "articulation therapy for kids").
- Secondary Keywords: 3-5 supporting keywords, comma-separated. Include: condition
  variants, location modifiers, patient audience terms (e.g. "for toddlers", "for adults"),
  and long-tail question phrases (e.g. "how to help child with lisp"). Do NOT repeat the
  primary keyword verbatim.
- For hub pages: broader category terms (e.g. "speech therapy services")
- For CMS template pages: the keyword represents the template — use the most-searched
  version of that condition/location

Return a JSON object with this exact structure (no markdown, no preamble):
{
  "pages": [
    {
      "slug": "/url-slug",
      "primary_keyword": "most important target keyword",
      "secondary_keywords": "keyword two, keyword three, keyword four, keyword five"
    }
  ]
}
"""


def _get_rich_text(prop: dict) -> str:
    return "".join(p.get("text", {}).get("content", "") for p in prop.get("rich_text", []))


def _get_title(prop: dict) -> str:
    return "".join(p.get("text", {}).get("content", "") for p in prop.get("title", []))


def _get_select(prop: dict) -> str:
    sel = prop.get("select")
    return sel.get("name", "") if sel else ""


async def _add_keyword_fields(notion: NotionClient, sitemap_db_id: str) -> None:
    """Add Primary Keyword and Secondary Keywords fields to the Sitemap DB if not present."""
    db_info = await notion._client.request(
        path=f"databases/{sitemap_db_id}",
        method="GET",
    )
    existing_props = db_info.get("properties", {})

    fields_to_add = {}
    if "Primary Keyword" not in existing_props:
        fields_to_add["Primary Keyword"] = {"rich_text": {}}
    if "Secondary Keywords" not in existing_props:
        fields_to_add["Secondary Keywords"] = {"rich_text": {}}

    if fields_to_add:
        await notion._client.request(
            path=f"databases/{sitemap_db_id}",
            method="PATCH",
            body={"properties": fields_to_add},
        )
        print(f"  ✓ Added fields: {', '.join(fields_to_add.keys())}")
    else:
        print("  Fields already exist — skipping schema update")


async def main(client_key: str, force: bool = False) -> None:
    cfg = CLIENTS[client_key]
    sitemap_db_id = cfg["sitemap_db_id"]
    notion = NotionClient(settings.notion_api_key)
    anthropic_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    # ── Step 1: Add fields to Sitemap DB schema ───────────────────────────────
    print(f"\nAdding keyword fields to Sitemap DB for {client_key}...")
    await _add_keyword_fields(notion, sitemap_db_id)

    # ── Step 2: Read all sitemap pages ─────────────────────────────────────────
    print("\nReading sitemap pages...")
    entries = await notion.query_database(
        sitemap_db_id,
        sorts=[{"property": "Order", "direction": "ascending"}],
    )
    print(f"  Found {len(entries)} pages")

    # Build page list, skip pages that already have keywords (unless --force)
    pages_to_process = []
    for entry in entries:
        props = entry["properties"]
        slug = _get_rich_text(props.get("Slug", {}))
        title = _get_title(props.get("Page Title", {})) or _get_title(props.get("Name", {}))
        purpose = _get_rich_text(props.get("Purpose", {}))
        key_sections = _get_rich_text(props.get("Key Sections", {}))
        section = _get_select(props.get("Section", {}))
        page_type = _get_select(props.get("Page Type", {}))
        existing_primary = _get_rich_text(props.get("Primary Keyword", {}))

        if existing_primary and not force:
            print(f"  — [{title}] already has keywords, skipping (use --force to overwrite)")
            continue

        pages_to_process.append({
            "id": entry["id"],
            "title": title,
            "slug": slug,
            "section": section,
            "page_type": page_type,
            "purpose": purpose,
            "key_sections": key_sections,
        })

    if not pages_to_process:
        print("\nAll pages already have keywords. Use --force to overwrite.")
        return

    print(f"\n  {len(pages_to_process)} pages need keywords")

    # ── Step 3: Call Claude in batches ─────────────────────────────────────────
    batch_size = 15
    all_keyword_data: dict[str, dict] = {}  # slug → {primary, secondary}

    for batch_idx in range(0, len(pages_to_process), batch_size):
        batch = pages_to_process[batch_idx:batch_idx + batch_size]
        batch_num = batch_idx // batch_size + 1
        print(f"\nBatch {batch_num}: suggesting keywords for {len(batch)} pages...")

        page_list = ""
        for pg in batch:
            page_list += (
                f"\n---\n"
                f"Page: {pg['title']}\n"
                f"Slug: {pg['slug']}\n"
                f"Section: {pg['section']} | Type: {pg['page_type']}\n"
                f"Purpose: {pg['purpose'][:300]}\n"
            )
            if pg["key_sections"]:
                page_list += f"Key Sections: {pg['key_sections'][:200]}\n"

        user_message = (
            f"Client: Summit Therapy\n"
            f"Business: Multi-location pediatric and adult therapy clinic (speech, OT, PT)\n"
            f"Locations: Frisco TX (primary), McKinney TX\n"
            f"Audience: Parents of children with developmental delays, adults with communication/mobility needs\n\n"
            f"Suggest target keywords for these {len(batch)} pages:\n"
            f"{page_list}"
        )

        response = await anthropic_client.messages.create(
            model=settings.anthropic_model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        raw = response.content[0].text if response.content else ""
        try:
            clean = re.sub(r"```(?:json)?\n?", "", raw).strip()
            data = json.loads(clean)
            for page in data.get("pages", []):
                all_keyword_data[page["slug"]] = {
                    "primary": page.get("primary_keyword", ""),
                    "secondary": page.get("secondary_keywords", ""),
                }
        except json.JSONDecodeError as e:
            print(f"  ⚠ JSON parse failed for batch {batch_num}: {e}")
            print(f"  Raw output: {raw[:300]}")
            continue

    # ── Step 4: Write keywords to Notion ──────────────────────────────────────
    print(f"\nWriting keywords to Notion...")
    updated = 0
    missing = 0

    for pg in pages_to_process:
        kw = all_keyword_data.get(pg["slug"])
        if not kw:
            print(f"  ⚠ No keywords returned for: {pg['title']} ({pg['slug']})")
            missing += 1
            continue

        await notion._client.request(
            path=f"pages/{pg['id']}",
            method="PATCH",
            body={
                "properties": {
                    "Primary Keyword": {
                        "rich_text": [{"type": "text", "text": {"content": kw["primary"][:2000]}}]
                    },
                    "Secondary Keywords": {
                        "rich_text": [{"type": "text", "text": {"content": kw["secondary"][:2000]}}]
                    },
                }
            },
        )
        print(f"  ✓ {pg['title']}: \"{kw['primary']}\"")
        updated += 1

    print(f"\nDone — {updated} updated, {missing} missing from Claude response.")
    print("\nReview and edit keywords directly in the Sitemap DB in Notion,")
    print("then run `make content` when ready.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", default="summit_therapy")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite existing keywords (default: skip pages that already have keywords)")
    args = parser.parse_args()
    asyncio.run(main(args.client, force=args.force))
