#!/usr/bin/env python3
"""
keyword_research.py — Keyword research via DataForSEO Keywords Data API

Runs for any client after onboarding. Outputs keyword volumes + CPC to the
client's Keywords DB in Notion.

Flow:
  1. Load client context from Notion (Brand Guidelines, Sitemap)
  2. Pre-flight: show what we found, accept corrections + target location
  3. Generate seed keywords via Claude
  4. Fetch volumes + CPC from DataForSEO Keywords Data API
  5. Cluster + prioritize + assign target pages via Claude
  6. Write to Notion Keywords DB
  7. Optional CSV export

Usage:
    make keyword-research CLIENT=summit_therapy
    make keyword-research CLIENT=summit_therapy EXPORT=1
    make keyword-research CLIENT=summit_therapy FORCE=1   # overwrite existing
    make keyword-research CLIENT=summit_therapy YES=1     # skip pre-flight
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

import anthropic

from config.clients import CLIENTS
from src.config import settings
from src.integrations.notion import NotionClient

OUTPUT_DIR = Path(__file__).parent.parent / "output"

DATAFORSEO_BASE = "https://api.dataforseo.com/v3"

# Intent labels must match the Keywords DB schema options exactly
INTENT_LABELS = ["Informational", "Commercial", "Transactional", "Local", "Navigational"]

# Directories / aggregators to exclude from competitor seeding
DIRECTORY_DOMAINS = {
    "yelp.com", "healthgrades.com", "zocdoc.com", "psychologytoday.com",
    "webmd.com", "mayoclinic.org", "medicalnewstoday.com", "verywellhealth.com",
    "google.com", "facebook.com", "instagram.com", "linkedin.com", "youtube.com",
    "twitter.com", "wikipedia.org", "reddit.com", "thumbtack.com", "care.com",
    "indeed.com", "glassdoor.com", "bbb.org", "angieslist.com", "houzz.com",
    "vitals.com", "ratemds.com", "usnews.com", "theknot.com", "aamft.org",
    "apta.org", "asha.org", "aota.org", "goodtherapy.org", "betterhelp.com",
    "talkspace.com", "thriveworks.com", "psychologytoday.com",
}

# ── DataForSEO auth ────────────────────────────────────────────────────────────

def _dfs_headers() -> dict:
    """HTTP Basic auth header for DataForSEO."""
    if not settings.dataforseo_login or not settings.dataforseo_password:
        raise RuntimeError("DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD must be set in .env")
    token = base64.b64encode(
        f"{settings.dataforseo_login}:{settings.dataforseo_password}".encode()
    ).decode()
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}


# ── Prompts ───────────────────────────────────────────────────────────────────

SEED_KEYWORDS_PROMPT = """\
You are an SEO strategist for a LOCAL service business. Your seeds will be used
to discover keyword ideas from a keyword research API — the more specific and
local your seeds, the more relevant the returned keyword ideas will be.

Business:
{business_summary}

Return ONLY a JSON object (no markdown, no preamble):
{{
  "seeds": [
    "keyword phrase 1",
    "keyword phrase 2"
  ]
}}

Rules:
- 40–60 seed phrases
- PRIORITIZE local/specific phrases — at least 60% of seeds should include:
    - City + state (e.g. "speech therapy frisco tx", "OT clinic mckinney texas")
    - "near me" variants (e.g. "pediatric speech therapy near me")
    - Condition + location (e.g. "autism therapy frisco", "sensory processing disorder mckinney tx")
    - Audience + location (e.g. "speech therapy for toddlers frisco")
- Also include: condition/service terms WITHOUT location (10-15 seeds), question phrases,
  insurance/cost phrases ("does insurance cover speech therapy"), comparison phrases
- Use natural patient/parent language — not clinical jargon
- Do NOT include job/career terms, salary, certification, or school-related phrases
"""

CLUSTER_KEYWORDS_PROMPT = """\
You are an SEO strategist. Review the following keywords with real search volume
and CPC data, then cluster them into intent groups and prioritize opportunities
for this business.

Business:
{business_summary}

Keywords with data:
{keyword_data}

Return ONLY a JSON object (no markdown, no preamble):
{{
  "keywords": [
    {{
      "keyword": "exact keyword phrase",
      "monthly_volume": "170",
      "cpc": "4.50",
      "competition": "Low | Medium | High | Unknown",
      "intent": "Informational | Commercial | Transactional | Local | Navigational",
      "priority": "High | Medium | Low",
      "target_page": "/suggested-slug",
      "notes": "one-line note on opportunity or caveat"
    }}
  ]
}}

Intent definitions:
  Local        = includes city, neighborhood, region, or "near me"
  Informational = questions, how-to, what is, symptoms — educational, no purchase intent
  Commercial   = specific service type, condition, treatment — research before booking
  Transactional = ready to book — "schedule", "find", "book appointment", "cost of"
  Navigational  = branded searches, specific practice name

Priority rules (this is a LOCAL service business — local opportunity matters more than raw volume):
  IMPORTANT: Many hyper-local keywords return no volume data from APIs — this is expected.
  A null/zero volume does NOT mean the keyword is Low priority for a local business.

  High   = includes city/region/near me (e.g. "frisco tx", "mckinney", "plano", "near me") → always High, regardless of volume
            OR condition-specific + local modifier → always High, regardless of volume
            OR strong transactional intent (book, schedule, find, cost of) AND volume > 100/mo
  Medium = national/broad service terms with volume > 500/mo (worth monitoring, not primary target)
            OR question/informational terms good for blog content
            OR local terms missing a specific city modifier but still geo-relevant
  Low    = national broad terms where a local clinic realistically cannot compete
            OR generic informational terms with no local intent and volume < 50/mo

Target page: pick the most relevant slug from the sitemap. If no page exists, suggest one.
Include ALL keywords from the input — do not drop any.
"""


# ── Load client context ───────────────────────────────────────────────────────

async def _load_client_context(notion: NotionClient, cfg: dict) -> dict:
    ctx: dict = {"name": cfg.get("name", ""), "competitors": [], "sitemap_pages": []}

    # Brand Guidelines
    try:
        result = await notion._client.request(
            path=f"databases/{cfg['brand_guidelines_db_id']}/query",
            method="POST", body={},
        )
        for entry in result.get("results", [])[:1]:
            props = entry.get("properties", {})

            def _rt(field: str) -> str:
                texts = props.get(field, {}).get("rich_text", [])
                return texts[0].get("plain_text", "") if texts else ""

            def _title(field: str) -> str:
                texts = props.get(field, {}).get("title", [])
                return texts[0].get("plain_text", "") if texts else ""

            ctx["business_name"] = _title("Name") or cfg.get("name", "")
            ctx["location"]      = _rt("Location") or _rt("City") or _rt("Service Area") or ""
            ctx["services_text"] = _rt("Services") or _rt("Primary Services") or ""
            ctx["audience"]      = _rt("Target Audience") or _rt("Audience") or ""
            ctx["website"]       = _rt("Website") or ""
            ctx["raw_guidelines"] = _rt("Raw Guidelines") or ""

            # Also check Client Info for website URL
            if not ctx["website"]:
                for f in ["Current Website URL", "Website URL"]:
                    val = props.get(f, {})
                    if val.get("type") == "url" and val.get("url"):
                        ctx["website"] = val["url"]
    except Exception:
        pass

    # Client Info — grab website URL if not found above
    if not ctx.get("website"):
        try:
            result = await notion._client.request(
                path=f"databases/{cfg['client_info_db_id']}/query",
                method="POST", body={},
            )
            for entry in result.get("results", [])[:1]:
                props = entry.get("properties", {})
                for field in ["Current Website URL", "Website", "Website URL"]:
                    val = props.get(field, {})
                    if val.get("type") == "url" and val.get("url"):
                        ctx["website"] = val["url"]
                        break
                    if val.get("type") == "rich_text":
                        texts = val.get("rich_text", [])
                        if texts:
                            ctx["website"] = texts[0].get("plain_text", "")
                            break
        except Exception:
            pass

    # Sitemap — approved pages
    try:
        result = await notion._client.request(
            path=f"databases/{cfg['sitemap_db_id']}/query",
            method="POST",
            body={"filter": {"property": "Status", "select": {"equals": "Approved"}}},
        )
        for entry in result.get("results", []):
            props = entry.get("properties", {})
            texts = props.get("Page Name", {}).get("title", [])
            if texts:
                ctx["sitemap_pages"].append(texts[0].get("plain_text", ""))
        ctx["sitemap_pages"] = ctx["sitemap_pages"][:30]
    except Exception:
        pass

    # Competitors DB — show existing entries in pre-flight
    competitors_db_id = cfg.get("competitors_db_id", "")
    if competitors_db_id:
        try:
            result = await notion._client.request(
                path=f"databases/{competitors_db_id}/query",
                method="POST", body={},
            )
            for entry in result.get("results", []):
                props = entry.get("properties", {})
                texts = props.get("Name", {}).get("title", [])
                if texts:
                    name = texts[0].get("plain_text", "")
                    sel = props.get("Type", {}).get("select") or {}
                    type_label = sel.get("name", "")
                    ctx["competitors"].append(
                        f"{name}{' (' + type_label + ')' if type_label else ''}"
                    )
        except Exception:
            pass

    return ctx


def _build_business_summary(ctx: dict, corrections: str = "") -> str:
    parts = [f"Business: {ctx.get('business_name') or ctx.get('name', 'Unknown')}"]
    if ctx.get("location"):
        parts.append(f"Location: {ctx['location']}")
    if ctx.get("services_text"):
        parts.append(f"Services: {ctx['services_text']}")
    if ctx.get("audience"):
        parts.append(f"Target audience: {ctx['audience']}")
    if ctx.get("website"):
        parts.append(f"Website: {ctx['website']}")
    if not ctx.get("services_text") and not ctx.get("location") and ctx.get("raw_guidelines"):
        parts.append(f"\nOnboarding description:\n{ctx['raw_guidelines'][:1500]}")
    if ctx.get("sitemap_pages"):
        parts.append(f"\nWebsite pages: {', '.join(ctx['sitemap_pages'])}")
    if corrections:
        parts.append(f"\nTeam corrections / additional context:\n{corrections}")
    return "\n".join(parts)


# ── Context validation ────────────────────────────────────────────────────────

def _validate_context(ctx: dict) -> None:
    """
    Validate that required Notion fields are populated before seed generation.
    Exits with a clear error message listing exactly what's missing.

    Hard fail: no location AND no services AND no raw onboarding description —
               Claude has nothing to work from.
    Warning:   dedicated fields are empty but raw_guidelines has context —
               seeds will run but will be less precise.
    """
    missing: list[str] = []
    warnings: list[str] = []

    has_location = bool(ctx.get("location"))
    has_services = bool(ctx.get("services_text"))
    has_raw      = bool(ctx.get("raw_guidelines"))

    # Hard fail — no usable context at all
    if not has_location and not has_services and not has_raw:
        missing.append(
            "Location / Service Area  →  Brand Guidelines DB: fill in 'Location' or 'Service Area'\n"
            "                            (e.g. 'Frisco TX and McKinney TX')"
        )
        missing.append(
            "Services  →  Brand Guidelines DB: fill in 'Services' or 'Primary Services'\n"
            "             (e.g. 'Speech Therapy, Occupational Therapy, Physical Therapy, ABA Therapy')"
        )
    else:
        # Partial data — warn but continue
        if not has_location and not has_raw:
            missing.append(
                "Location / Service Area  →  Brand Guidelines DB: fill in 'Location' or 'Service Area'\n"
                "                            (e.g. 'Frisco TX and McKinney TX')"
            )
        elif not has_location:
            warnings.append(
                "No dedicated Location field — Claude will infer from onboarding description "
                "(add 'Location' to Brand Guidelines DB for more precise seeds)"
            )

        if not has_services and not has_raw:
            missing.append(
                "Services  →  Brand Guidelines DB: fill in 'Services' or 'Primary Services'\n"
                "             (e.g. 'Speech Therapy, Occupational Therapy, Physical Therapy, ABA Therapy')"
            )
        elif not has_services:
            warnings.append(
                "No dedicated Services field — Claude will infer from onboarding description "
                "(add 'Services' to Brand Guidelines DB for more precise seeds)"
            )

    # Soft warnings — always checked
    if not ctx.get("audience"):
        warnings.append("Target Audience is empty — seeds won't be audience-specific (children vs adults, etc.)")
    if not ctx.get("sitemap_pages"):
        warnings.append("No approved sitemap pages found — keyword-to-page assignments will be guesses")
    if not ctx.get("website"):
        warnings.append("No website URL found — competitor and topical context will be limited")

    sep = "─" * 60

    if missing:
        print(f"\n{sep}")
        print(f"  ✗ Keyword research cannot run — required data is missing from Notion")
        print(sep)
        for item in missing:
            print(f"\n  Missing: {item}")
        if warnings:
            print(f"\n  Also recommended (won't block, but seeds will be weaker):")
            for w in warnings:
                print(f"  • {w}")
        print(f"\n{sep}")
        print(f"  Fix the above in Notion, then re-run:")
        print(f"  make keyword-research CLIENT={ctx.get('name', 'client_key').lower().replace(' ', '_')}")
        print(sep)
        sys.exit(1)

    if warnings:
        print(f"\n{sep}")
        print(f"  ⚠ Context loaded with gaps — seeds will run but may be less precise:")
        for w in warnings:
            print(f"  • {w}")
        print(sep)


# ── Pre-flight confirmation ───────────────────────────────────────────────────

# State → DataForSEO location_code (same as Google Ads geo IDs)
STATE_LOCATION_CODES: dict[str, tuple[int, str]] = {
    "TX": (21167, "Texas"),
    "CA": (21137, "California"),
    "FL": (21139, "Florida"),
    "NY": (21136, "New York"),
    "IL": (21142, "Illinois"),
    "WA": (21183, "Washington"),
    "OR": (21174, "Oregon"),
    "CO": (21138, "Colorado"),
    "AZ": (21132, "Arizona"),
    "GA": (21141, "Georgia"),
    "NC": (21164, "North Carolina"),
    "VA": (21182, "Virginia"),
    "PA": (21175, "Pennsylvania"),
    "OH": (21165, "Ohio"),
    "MI": (21148, "Michigan"),
    "MN": (21149, "Minnesota"),
}

METRO_LOCATION_CODES: dict[str, tuple[int, str]] = {
    "dallas":      (1026493, "Dallas, TX metro"),
    "houston":     (1026330, "Houston, TX metro"),
    "austin":      (1026313, "Austin, TX metro"),
    "los angeles": (1014221, "Los Angeles, CA metro"),
    "new york":    (1023191, "New York, NY metro"),
    "chicago":     (1016367, "Chicago, IL metro"),
    "seattle":     (1027741, "Seattle, WA metro"),
    "portland":    (1027253, "Portland, OR metro"),
    "denver":      (1017477, "Denver, CO metro"),
    "phoenix":     (1023585, "Phoenix, AZ metro"),
    "atlanta":     (1012873, "Atlanta, GA metro"),
    "miami":       (1020961, "Miami, FL metro"),
}


def _resolve_location(user_input: str) -> tuple[int, str]:
    """Parse a typed location into a DataForSEO location_code. Falls back to USA."""
    if not user_input.strip():
        return 2840, "United States (nationwide)"
    loc = user_input.strip().lower()
    for key, (code, label) in METRO_LOCATION_CODES.items():
        if key in loc:
            return code, label
    upper = user_input.strip().upper()
    if upper in STATE_LOCATION_CODES:
        code, label = STATE_LOCATION_CODES[upper]
        return code, label
    for abbr, (code, label) in STATE_LOCATION_CODES.items():
        if label.lower() in loc:
            return code, label
    print(f"  ⚠ '{user_input}' not recognized — defaulting to USA.")
    return 2840, "United States (nationwide)"


def _show_preflight(ctx: dict, cfg: dict) -> tuple[str, int, str]:
    """
    Display pre-flight summary and prompt for confirmation.
    Returns (corrections_text, location_code, location_label).
    """
    sep = "─" * 60
    print(f"\n{sep}")
    print(f"  Pre-flight Check — {cfg['name']}")
    print(sep)

    print(f"\n  Business:  {ctx.get('business_name') or ctx.get('name', '—')}")
    print(f"  Location:  {ctx.get('location') or '— (not found in Notion)'}")
    print(f"  Services:  {ctx.get('services_text') or '— (not found in Notion)'}")
    if ctx.get("audience"):
        print(f"  Audience:  {ctx['audience']}")
    if ctx.get("website"):
        print(f"  Website:   {ctx['website']}")

    raw = ctx.get("raw_guidelines", "")
    if raw and (not ctx.get("services_text") or not ctx.get("location")):
        snippet = raw[:300].replace("\n", " ")
        if len(raw) > 300:
            snippet += "..."
        print(f"\n  Onboarding description (used as context):")
        print(f"  {snippet}")

    pages = ctx.get("sitemap_pages", [])
    if pages:
        print(f"\n  Sitemap pages ({len(pages)}):")
        for p in pages[:8]:
            print(f"    • {p}")
        if len(pages) > 8:
            print(f"    ... and {len(pages) - 8} more")
    else:
        print(f"\n  Sitemap:   — (no approved pages yet)")

    competitors = ctx.get("competitors", [])
    if competitors:
        print(f"\n  Competitors in Notion ({len(competitors)}):")
        for c in competitors:
            print(f"    • {c}")
    else:
        print(f"\n  Competitors: — (none yet — run make competitor-research after this)")

    print(f"\n  DataForSEO: keyword discovery is USA-wide (DataForSEO Labs limitation).")
    print(f"  Local targeting is handled via city-specific seed phrases in the next step.")

    print(f"\n{sep}")
    print(f"  Does this look correct?")
    print(f"  • Press Enter to continue")
    print(f"  • Type corrections or additional context, then press Enter")
    print(f"    e.g. 'Frisco and Flower Mound TX only, main service is speech therapy for kids'")
    print(sep)

    corrections = input("\n  Corrections / additions: ").strip()
    return corrections


# ── Step 1: Generate seed keywords via Claude ─────────────────────────────────

async def _generate_seeds(business_summary: str) -> list[str]:
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    response = await client.messages.create(
        model=settings.anthropic_model,
        max_tokens=2000,
        messages=[{"role": "user", "content": SEED_KEYWORDS_PROMPT.format(
            business_summary=business_summary
        )}],
    )
    text = response.content[0].text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    seeds = json.loads(text).get("seeds", [])
    print(f"  Generated {len(seeds)} seed keywords")
    return seeds


# ── Step 2: Fetch keyword ideas + volumes from DataForSEO Labs ────────────────

# Job/career terms that aren't client searches — filter these out
_JOB_TERMS = {"jobs", "job", "salary", "salaries", "hiring", "career",
               "certification", "degree", "school", "program", "course",
               "assistant jobs", "aide jobs", "aide salary"}


def _is_job_term(keyword: str) -> bool:
    kw_lower = keyword.lower()
    return any(term in kw_lower for term in _JOB_TERMS)


async def _fetch_volumes(keywords: list[str]) -> list[dict]:
    """
    Call DataForSEO Keywords Data search_volume/live.
    Validates exact volumes + CPC for Claude-generated seed keywords.
    Returns all keywords including those with null volume (local/niche terms
    still belong in the DB for strategic value).
    Batches in groups of 1000 (API max per request).
    """
    headers = _dfs_headers()
    all_results: list[dict] = []
    seen: set[str] = set()

    for i in range(0, len(keywords), 1000):
        batch = keywords[i : i + 1000]
        payload = [{"keywords": batch, "location_code": 2840, "language_code": "en"}]
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                f"{DATAFORSEO_BASE}/keywords_data/google_ads/search_volume/live",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        for task in data.get("tasks", []):
            if task.get("status_code") != 20000:
                print(f"  ⚠ DataForSEO error: {task.get('status_message', 'unknown')}")
                continue
            for item in (task.get("result") or []):
                keyword = item.get("keyword", "").strip().lower()
                if not keyword or keyword in seen:
                    continue
                if _is_job_term(keyword):
                    continue
                seen.add(keyword)

                vol = item.get("search_volume") or 0
                cpc = round(item.get("cpc") or 0, 2)
                comp_raw = item.get("competition") or ""
                comp_str = str(comp_raw).upper()
                if comp_str in ("HIGH", "MEDIUM", "LOW"):
                    comp_level = comp_str.capitalize()
                else:
                    try:
                        comp_float = float(comp_raw or 0)
                        if comp_float < 0.33:
                            comp_level = "Low"
                        elif comp_float < 0.66:
                            comp_level = "Medium"
                        elif comp_float > 0:
                            comp_level = "High"
                        else:
                            comp_level = "Unknown"
                    except (TypeError, ValueError):
                        comp_level = "Unknown"

                all_results.append({
                    "keyword":              keyword,
                    "avg_monthly_searches": vol,
                    "cpc":                  cpc,
                    "competition":          comp_level,
                })

    with_vol = sum(1 for r in all_results if r["avg_monthly_searches"] > 0)
    all_results.sort(key=lambda x: x["avg_monthly_searches"], reverse=True)
    print(
        f"  Got volume for {with_vol}/{len(all_results)} keywords "
        f"({len(all_results) - with_vol} local/niche have no data — still included)"
    )
    return all_results


# ── Step 3: Cluster + prioritize via Claude ───────────────────────────────────

async def _cluster_keywords(ideas: list[dict], business_summary: str) -> list[dict]:
    """
    Annotate keywords in batches of 80 to stay within Claude's output limits.
    All batches are processed and merged into one final list.
    """
    BATCH_SIZE = 80
    all_keywords: list[dict] = []
    batches = [ideas[i:i + BATCH_SIZE] for i in range(0, len(ideas), BATCH_SIZE)]
    print(f"  Annotating {len(ideas)} keywords in {len(batches)} batch(es)...")

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    for batch_num, batch in enumerate(batches, 1):
        kw_lines = []
        for idea in batch:
            vol = f"{idea['avg_monthly_searches']:,}" if idea["avg_monthly_searches"] else "<10"
            cpc = f"${idea['cpc']:.2f}" if idea["cpc"] else "—"
            kw_lines.append(
                f"- {idea['keyword']} | Volume: {vol}/mo | CPC: {cpc} | Competition: {idea['competition']}"
            )

        response = await client.messages.create(
            model=settings.anthropic_model,
            max_tokens=16000,
            messages=[{"role": "user", "content": CLUSTER_KEYWORDS_PROMPT.format(
                business_summary=business_summary,
                keyword_data="\n".join(kw_lines),
            )}],
        )
        text = response.content[0].text.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

        try:
            batch_keywords = json.loads(text).get("keywords", [])
            all_keywords.extend(batch_keywords)
            print(f"  Batch {batch_num}/{len(batches)}: {len(batch_keywords)} annotated")
        except json.JSONDecodeError:
            print(f"  ⚠ Batch {batch_num} non-JSON — writing as unannotated")
            all_keywords.extend([{
                "keyword":       idea["keyword"],
                "monthly_volume": str(idea["avg_monthly_searches"]) or "—",
                "cpc":           str(idea["cpc"]) if idea["cpc"] else "—",
                "competition":   idea["competition"],
                "intent":        "Commercial",
                "priority":      "Medium",
                "target_page":   "",
                "notes":         "",
            } for idea in batch])

    print(f"  Clustered {len(all_keywords)} annotated keywords total")
    return all_keywords


# ── Step 4: Write to Notion Keywords DB ───────────────────────────────────────

async def _ensure_cpc_field(notion: NotionClient, keywords_db_id: str) -> None:
    """Add CPC field to Keywords DB if it doesn't already exist."""
    try:
        db = await notion._client.request(
            path=f"databases/{keywords_db_id}", method="GET"
        )
        if "CPC" not in db.get("properties", {}):
            await notion._client.request(
                path=f"databases/{keywords_db_id}",
                method="PATCH",
                body={"properties": {"CPC": {"number": {"format": "dollar"}}}},
            )
    except Exception:
        pass  # Non-fatal — CPC just won't be written as a number field


async def _write_to_notion(
    notion: NotionClient,
    keywords_db_id: str,
    keywords: list[dict],
    force: bool = False,
) -> int:
    await _ensure_cpc_field(notion, keywords_db_id)

    # Load existing keywords to avoid duplicates
    existing: dict[str, str] = {}
    if not force:
        try:
            result = await notion._client.request(
                path=f"databases/{keywords_db_id}/query",
                method="POST", body={},
            )
            for entry in result.get("results", []):
                props = entry.get("properties", {})
                texts = props.get("Keyword", {}).get("title", [])
                if texts:
                    kw = texts[0].get("plain_text", "").lower().strip()
                    existing[kw] = entry["id"]
        except Exception:
            pass

    written = 0
    skipped = 0

    for kw_data in keywords:
        keyword = kw_data.get("keyword", "").strip()
        if not keyword:
            continue

        if not force and keyword.lower() in existing:
            skipped += 1
            continue

        priority = kw_data.get("priority", "Medium")
        if priority not in ["High", "Medium", "Low"]:
            priority = "Medium"

        intent = kw_data.get("intent", "Commercial")
        if intent not in INTENT_LABELS:
            intent = "Commercial"

        # Volume as rich_text (handles blanks gracefully)
        vol_str = str(kw_data.get("monthly_volume", "")).strip()
        if not vol_str or vol_str == "0":
            vol_str = "—"

        properties: dict = {
            "Keyword":              {"title": [{"text": {"content": keyword}}]},
            "Monthly Search Volume": {"rich_text": [{"text": {"content": vol_str}}]},
            "Intent":               {"select": {"name": intent}},
            "Priority":             {"select": {"name": priority}},
            "Status":               {"select": {"name": "Target"}},
            "Notes":                {"rich_text": [{"text": {
                "content": kw_data.get("notes", "")[:2000]
            }}]},
        }

        # CPC as number field (added via self-heal above)
        cpc_raw = kw_data.get("cpc", "")
        try:
            cpc_num = float(str(cpc_raw).replace("$", "").strip())
            if cpc_num > 0:
                properties["CPC"] = {"number": cpc_num}
        except (ValueError, TypeError):
            pass

        if kw_data.get("target_page"):
            properties["Target Page"] = {
                "rich_text": [{"text": {"content": kw_data["target_page"]}}]
            }

        try:
            if force and keyword.lower() in existing:
                await notion._client.request(
                    path=f"pages/{existing[keyword.lower()]}",
                    method="PATCH",
                    body={"properties": properties},
                )
            else:
                await notion._client.request(
                    path="pages",
                    method="POST",
                    body={"parent": {"database_id": keywords_db_id}, "properties": properties},
                )
            written += 1
        except Exception as e:
            print(f"  ⚠ Failed to write '{keyword}': {e}")

    if skipped:
        print(f"  Skipped {skipped} existing keywords (use --force to overwrite)")
    return written


# ── Step 5: Optional CSV export ───────────────────────────────────────────────

def _export_csv(keywords: list[dict], client_key: str) -> Path:
    out_dir = OUTPUT_DIR / client_key
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"keyword_research_{datetime.now().strftime('%Y%m%d')}.csv"
    fields = ["keyword", "monthly_volume", "cpc", "competition", "intent",
              "priority", "target_page", "notes"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(keywords)
    return path


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(
    client_key: str,
    export: bool = False,
    force: bool = False,
    yes: bool = False,
) -> None:
    cfg = CLIENTS.get(client_key)
    if not cfg:
        print(f"Client '{client_key}' not found in config/clients.py")
        sys.exit(1)

    keywords_db_id = cfg.get("keywords_db_id", "")
    if not keywords_db_id:
        print(f"No keywords_db_id for {cfg['name']}. Run 'make seo-init CLIENT={client_key}' first.")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"  Keyword Research — {cfg['name']}")
    print(f"{'='*60}\n")

    notion = NotionClient(settings.notion_api_key)

    print("Loading client context from Notion...")
    ctx = await _load_client_context(notion, cfg)

    # Validate required fields before wasting an LLM call on bad seeds
    _validate_context(ctx)

    # Pre-flight
    corrections = ""
    if not yes:
        corrections = _show_preflight(ctx, cfg)
        print(f"\n  Starting...\n")

    business_summary = _build_business_summary(ctx, corrections=corrections)

    # Step 1: Seeds
    print("Generating seed keywords...")
    seeds = await _generate_seeds(business_summary)

    # Step 2: Validate volumes + CPC via DataForSEO search_volume/live
    print(f"\nFetching volumes + CPC from DataForSEO...")
    try:
        ideas = await _fetch_volumes(seeds)
        if not ideas:
            print("  ⚠ DataForSEO returned 0 results — check credentials and account balance")
            ideas = [{"keyword": s, "avg_monthly_searches": 0, "cpc": 0.0,
                      "competition": "Unknown"} for s in seeds]
    except Exception as e:
        print(f"  ⚠ DataForSEO API error: {e}")
        print("  Continuing with Claude-only analysis (no volume data)...")
        ideas = [{"keyword": s, "avg_monthly_searches": 0, "cpc": 0.0,
                  "competition": "Unknown"} for s in seeds]

    # Step 3: Cluster + annotate
    print("\nClustering and prioritizing via Claude...")
    keywords = await _cluster_keywords(ideas, business_summary)

    # Step 4: Write to Notion
    print(f"\nWriting to Notion Keywords DB...")
    written = await _write_to_notion(notion, keywords_db_id, keywords, force=force)
    print(f"  ✓ {written} keywords written to Notion")

    # Step 5: CSV export
    if export:
        csv_path = _export_csv(keywords, client_key)
        print(f"  ✓ Exported to {csv_path}")

    # Summary
    high   = sum(1 for k in keywords if k.get("priority") == "High")
    medium = sum(1 for k in keywords if k.get("priority") == "Medium")
    low    = sum(1 for k in keywords if k.get("priority") == "Low")
    intents: dict[str, int] = {}
    for k in keywords:
        i = k.get("intent", "Other")
        intents[i] = intents.get(i, 0) + 1

    print(f"\n{'─'*60}")
    print(f"  Results — {cfg['name']}")
    print(f"{'─'*60}")
    print(f"  Total: {len(keywords)}  |  High: {high}  Medium: {medium}  Low: {low}")
    print(f"  By intent:")
    for intent, count in sorted(intents.items()):
        print(f"    {intent}: {count}")
    print(f"\n  Next steps:")
    print(f"  1. Review Keywords DB in Notion — filter Priority = High")
    print(f"  2. Adjust target pages and intent as needed")
    print(f"  3. make competitor-research CLIENT={client_key}  ← auto-seed Competitors DB from SERPs")
    print(f"  4. make battle-plan CLIENT={client_key}          ← generate SEO strategy")
    if export:
        print(f"  5. Share {client_key}/keyword_research_*.csv with team for review")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Keyword research via DataForSEO")
    parser.add_argument("--client", required=True)
    parser.add_argument("--export",  action="store_true", help="Also export CSV")
    parser.add_argument("--force",   action="store_true", help="Overwrite existing rows")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip pre-flight prompt")
    args = parser.parse_args()
    asyncio.run(main(client_key=args.client, export=args.export, force=args.force, yes=args.yes))
