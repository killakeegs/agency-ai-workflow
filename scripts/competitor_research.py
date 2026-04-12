#!/usr/bin/env python3
"""
competitor_research.py — Auto-seed Competitors DB from DataForSEO SERP analysis

Runs AFTER keyword research is reviewed and approved by the team.

Flow:
  1. Read High-priority keywords from Notion Keywords DB
  2. Call DataForSEO SERP API for each keyword (top 10 organic results)
  3. Count which domains appear most frequently across all SERPs
  4. Filter out directories, aggregators, and non-competitors
  5. Pre-flight: show the ranked domain list for team confirmation
  6. Write confirmed competitors to Notion Competitors DB

Usage:
    make competitor-research CLIENT=summit_therapy
    make competitor-research CLIENT=summit_therapy LIMIT=15   # cap SERP calls
    make competitor-research CLIENT=summit_therapy FORCE=1    # overwrite existing
    make competitor-research CLIENT=summit_therapy YES=1      # skip pre-flight
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

import anthropic

from config.clients import CLIENTS
from src.config import settings
from src.integrations.notion import NotionClient

DATAFORSEO_BASE = "https://api.dataforseo.com/v3"

# Domains that are directories, aggregators, or otherwise not real competitors
EXCLUDE_DOMAINS = {
    # Directories & review sites
    "yelp.com", "healthgrades.com", "zocdoc.com", "psychologytoday.com",
    "vitals.com", "ratemds.com", "usnews.com", "bbb.org", "thumbtack.com",
    "care.com", "angieslist.com", "expertise.com", "bark.com", "homeadvisor.com",
    # Health info sites
    "webmd.com", "mayoclinic.org", "medicalnewstoday.com", "verywellhealth.com",
    "healthline.com", "clevelandclinic.org", "hopkinsmedicine.org", "nih.gov",
    "cdc.gov", "medlineplus.gov", "drugs.com",
    # Professional associations
    "asha.org", "apta.org", "aota.org", "aamft.org", "nbcot.org",
    # Social / general
    "facebook.com", "instagram.com", "linkedin.com", "youtube.com",
    "twitter.com", "x.com", "tiktok.com", "pinterest.com",
    "wikipedia.org", "reddit.com", "quora.com",
    # Job sites
    "indeed.com", "glassdoor.com", "ziprecruiter.com",
    # Telehealth platforms (different competitive category)
    "betterhelp.com", "talkspace.com", "cerebral.com", "brightside.com",
    # Maps / local
    "google.com", "maps.google.com", "apple.com",
}

CLASSIFY_PROMPT = """\
You are an SEO analyst. Given a list of competitor domains found in Google search
results for a local business, classify each as Local or Organic competitor.

Business:
{business_summary}

Domains found (with keyword count = how many target keywords they rank for):
{domain_list}

Return ONLY a JSON object (no markdown, no preamble):
{{
  "competitors": [
    {{
      "domain": "example.com",
      "name": "Business Name (if you know it, else leave blank)",
      "type": "Local | Organic | Both",
      "keyword_count": 5,
      "notes": "one-line note — e.g. direct local competitor, national chain, regional player"
    }}
  ]
}}

Type definitions:
  Local    = appears in Google Map Pack or is a nearby physical location competitor
  Organic  = ranks in organic blue-link results; may be national or regional
  Both     = consistently appears in both Map Pack and organic results

Include ALL domains from the input. Order by keyword_count descending.
"""


# ── DataForSEO auth ───────────────────────────────────────────────────────────

def _dfs_headers() -> dict:
    if not settings.dataforseo_login or not settings.dataforseo_password:
        raise RuntimeError("DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD must be set in .env")
    token = base64.b64encode(
        f"{settings.dataforseo_login}:{settings.dataforseo_password}".encode()
    ).decode()
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}


# ── Load high-priority keywords from Notion ───────────────────────────────────

async def _load_target_keywords(
    notion: NotionClient, keywords_db_id: str, limit: int
) -> list[dict]:
    """Return High-priority keywords from Notion Keywords DB."""
    try:
        result = await notion._client.request(
            path=f"databases/{keywords_db_id}/query",
            method="POST",
            body={
                "filter": {"property": "Priority", "select": {"equals": "High"}},
                "sorts": [{"property": "Monthly Search Volume", "direction": "descending"}],
                "page_size": limit,
            },
        )
        keywords = []
        for entry in result.get("results", []):
            props = entry.get("properties", {})
            texts = props.get("Keyword", {}).get("title", [])
            if texts:
                keywords.append({
                    "keyword": texts[0].get("plain_text", ""),
                    "page_id": entry["id"],
                })
        return keywords
    except Exception as e:
        print(f"  ⚠ Could not load keywords from Notion: {e}")
        return []


# ── SERP calls ────────────────────────────────────────────────────────────────

async def _fetch_serp(
    keyword: str,
    location_code: int = 2840,
) -> list[str]:
    """
    Fetch top 10 organic domains for a keyword via DataForSEO SERP API.
    Returns list of domains (not full URLs).
    """
    headers = _dfs_headers()
    payload = [{
        "keyword":       keyword,
        "location_code": location_code,
        "language_code": "en",
        "device":        "desktop",
        "depth":         10,
    }]

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{DATAFORSEO_BASE}/serp/google/organic/live/advanced",
            headers=headers,
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

    domains = []
    for task in data.get("tasks", []):
        if task.get("status_code") != 20000:
            continue
        for result in (task.get("result") or []):
            for item in (result.get("items") or []):
                if item.get("type") != "organic":
                    continue
                url = item.get("url", "")
                if not url:
                    continue
                parsed = urlparse(url)
                domain = parsed.netloc.lower().removeprefix("www.")
                if domain:
                    domains.append(domain)

    return domains


async def _run_serp_analysis(
    keywords: list[dict],
    location_code: int,
) -> dict[str, int]:
    """
    Run SERP call for each keyword. Returns domain → appearance count.
    Adds a small delay between calls to be polite to the API.
    """
    domain_counts: dict[str, int] = defaultdict(int)
    keyword_domains: dict[str, list[str]] = {}  # keyword → domains (for position notes)

    for i, kw in enumerate(keywords):
        keyword = kw["keyword"]
        print(f"  [{i+1}/{len(keywords)}] {keyword}")
        try:
            domains = await _fetch_serp(keyword, location_code=location_code)
            keyword_domains[keyword] = domains
            for domain in set(domains):  # count once per keyword, not per position
                if domain not in EXCLUDE_DOMAINS:
                    domain_counts[domain] += 1
        except Exception as e:
            print(f"    ⚠ SERP failed: {e}")
        if i < len(keywords) - 1:
            await asyncio.sleep(0.5)  # rate limit courtesy

    return dict(domain_counts)


# ── Classify competitors via Claude ──────────────────────────────────────────

async def _classify_competitors(
    domain_counts: dict[str, int],
    business_summary: str,
    min_appearances: int = 2,
) -> list[dict]:
    """
    Use Claude to classify domains as Local / Organic / Both and add names.
    Only includes domains that appear for min_appearances+ keywords.
    """
    # Filter to meaningful competitors only
    filtered = {
        domain: count
        for domain, count in domain_counts.items()
        if count >= min_appearances
    }

    if not filtered:
        # If nothing hits the threshold, include everything
        filtered = domain_counts

    # Sort by frequency
    sorted_domains = sorted(filtered.items(), key=lambda x: x[1], reverse=True)

    domain_lines = "\n".join(
        f"- {domain} (appears for {count} keywords)"
        for domain, count in sorted_domains
    )

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    response = await client.messages.create(
        model=settings.anthropic_model,
        max_tokens=3000,
        messages=[{"role": "user", "content": CLASSIFY_PROMPT.format(
            business_summary=business_summary,
            domain_list=domain_lines,
        )}],
    )
    text = response.content[0].text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    try:
        competitors = json.loads(text).get("competitors", [])
    except json.JSONDecodeError:
        # Fall back to unclassified list
        competitors = [
            {
                "domain": domain,
                "name": "",
                "type": "Organic",
                "keyword_count": count,
                "notes": "",
            }
            for domain, count in sorted_domains
        ]

    return competitors


# ── Pre-flight: show findings before writing ──────────────────────────────────

def _show_competitor_preflight(competitors: list[dict]) -> list[dict]:
    """
    Show the discovered competitors and let the team confirm or exclude any.
    Returns the filtered list to write to Notion.
    """
    sep = "─" * 60
    print(f"\n{sep}")
    print(f"  Competitors Found — Review Before Writing to Notion")
    print(sep)
    print(f"\n  {'#':<4} {'Domain':<35} {'Type':<10} {'Keywords':<10} Notes")
    print(f"  {'─'*4} {'─'*35} {'─'*10} {'─'*10} {'─'*30}")

    for i, comp in enumerate(competitors, 1):
        name = f"{comp.get('name', '') or ''} ({comp['domain']})" if comp.get("name") else comp["domain"]
        print(
            f"  {i:<4} {name:<35} {comp.get('type', '?'):<10} "
            f"{comp.get('keyword_count', 0):<10} {comp.get('notes', '')[:40]}"
        )

    print(f"\n{sep}")
    print(f"  Enter numbers to EXCLUDE (comma-separated), or press Enter to write all:")
    print(f"  e.g. '3,7' excludes rows 3 and 7")
    print(sep)

    raw = input("\n  Exclude: ").strip()
    if not raw:
        return competitors

    try:
        exclude_indices = {int(x.strip()) - 1 for x in raw.split(",") if x.strip().isdigit()}
        filtered = [c for i, c in enumerate(competitors) if i not in exclude_indices]
        excluded = len(competitors) - len(filtered)
        print(f"  Excluded {excluded} domain(s). Writing {len(filtered)} competitors.")
        return filtered
    except Exception:
        print("  Could not parse exclusions — writing all.")
        return competitors


# ── Write to Competitors DB ───────────────────────────────────────────────────

async def _write_competitors(
    notion: NotionClient,
    competitors_db_id: str,
    competitors: list[dict],
    force: bool = False,
) -> int:
    # Load existing to avoid duplicates
    existing: dict[str, str] = {}
    if not force:
        try:
            result = await notion._client.request(
                path=f"databases/{competitors_db_id}/query",
                method="POST", body={},
            )
            for entry in result.get("results", []):
                props = entry.get("properties", {})
                texts = props.get("Name", {}).get("title", [])
                if texts:
                    name = texts[0].get("plain_text", "").lower().strip()
                    existing[name] = entry["id"]
        except Exception:
            pass

    written = 0
    skipped = 0

    for comp in competitors:
        domain = comp.get("domain", "").strip()
        display_name = comp.get("name", "").strip() or domain
        if not domain:
            continue

        lookup_key = display_name.lower()
        if not force and lookup_key in existing:
            skipped += 1
            continue

        comp_type = comp.get("type", "Organic")
        if comp_type not in ["Local", "Organic", "Both"]:
            comp_type = "Organic"

        keyword_count = comp.get("keyword_count", 0)
        notes = comp.get("notes", "")
        if keyword_count:
            notes = f"Ranks for {keyword_count} of our target keywords. {notes}".strip()

        properties: dict = {
            "Name":    {"title": [{"text": {"content": display_name}}]},
            "Type":    {"select": {"name": comp_type}},
            "Website": {"url": f"https://{domain}"},
            "Notes":   {"rich_text": [{"text": {"content": notes[:2000]}}]},
        }

        try:
            if force and lookup_key in existing:
                await notion._client.request(
                    path=f"pages/{existing[lookup_key]}",
                    method="PATCH",
                    body={"properties": properties},
                )
            else:
                await notion._client.request(
                    path="pages",
                    method="POST",
                    body={"parent": {"database_id": competitors_db_id}, "properties": properties},
                )
            written += 1
        except Exception as e:
            print(f"  ⚠ Failed to write '{display_name}': {e}")

    if skipped:
        print(f"  Skipped {skipped} existing competitors (use --force to overwrite)")
    return written


# ── Build business summary for Claude ─────────────────────────────────────────

async def _load_business_summary(notion: NotionClient, cfg: dict) -> str:
    parts = [f"Business: {cfg.get('name', 'Unknown')}"]
    try:
        result = await notion._client.request(
            path=f"databases/{cfg['brand_guidelines_db_id']}/query",
            method="POST", body={},
        )
        for entry in result.get("results", [])[:1]:
            props = entry.get("properties", {})

            def _rt(f: str) -> str:
                texts = props.get(f, {}).get("rich_text", [])
                return texts[0].get("plain_text", "") if texts else ""

            location = _rt("Location") or _rt("City") or _rt("Service Area")
            services = _rt("Services") or _rt("Primary Services")
            raw      = _rt("Raw Guidelines")

            if location:
                parts.append(f"Location: {location}")
            if services:
                parts.append(f"Services: {services}")
            if not services and raw:
                parts.append(f"Onboarding description: {raw[:800]}")
    except Exception:
        pass
    return "\n".join(parts)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(
    client_key: str,
    limit: int = 20,
    force: bool = False,
    yes: bool = False,
    min_appearances: int = 2,
) -> None:
    cfg = CLIENTS.get(client_key)
    if not cfg:
        print(f"Client '{client_key}' not found")
        sys.exit(1)

    keywords_db_id   = cfg.get("keywords_db_id", "")
    competitors_db_id = cfg.get("competitors_db_id", "")

    if not keywords_db_id:
        print(f"No keywords_db_id. Run 'make seo-init CLIENT={client_key}' first.")
        sys.exit(1)
    if not competitors_db_id:
        print(f"No competitors_db_id. Run 'make seo-init CLIENT={client_key}' first.")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"  Competitor Research — {cfg['name']}")
    print(f"{'='*60}\n")

    notion = NotionClient(settings.notion_api_key)

    # Load keywords and business context
    print(f"Loading High-priority keywords from Notion (up to {limit})...")
    keywords = await _load_target_keywords(notion, keywords_db_id, limit=limit)

    if not keywords:
        print("  ⚠ No High-priority keywords found.")
        print("  Run 'make keyword-research' first, then review and approve keywords in Notion.")
        sys.exit(1)

    print(f"  Found {len(keywords)} keywords to research\n")
    for kw in keywords:
        print(f"    • {kw['keyword']}")

    # Location — auto-detect from brand guidelines or default to USA
    business_summary = await _load_business_summary(notion, cfg)
    location_code = 2840  # default USA

    if not yes:
        print(f"\n  Target location for SERP data:")
        print(f"  State (TX, CA...), metro (Dallas, Austin...), or Enter for USA:")
        loc_input = input("  Location: ").strip()
        if loc_input:
            from scripts.keyword_research import _resolve_location
            location_code, location_label = _resolve_location(loc_input)
            print(f"  Using: {location_label}")
        else:
            location_label = "United States (nationwide)"
            print(f"  Using: {location_label}")
    else:
        location_label = "United States (nationwide)"

    # Run SERP analysis
    print(f"\nRunning SERP analysis ({len(keywords)} keywords × top 10 results)...")
    domain_counts = await _run_serp_analysis(keywords, location_code)

    if not domain_counts:
        print("  ⚠ No domains found in SERP results. Check DataForSEO credentials.")
        sys.exit(1)

    total_domains = len(domain_counts)
    above_threshold = sum(1 for c in domain_counts.values() if c >= min_appearances)
    print(f"\n  Found {total_domains} unique domains")
    print(f"  {above_threshold} appear for {min_appearances}+ keywords (likely competitors)")

    # Classify via Claude
    print("\nClassifying competitors via Claude...")
    competitors = await _classify_competitors(domain_counts, business_summary, min_appearances)
    print(f"  Classified {len(competitors)} competitors")

    # Pre-flight before writing
    if not yes:
        competitors = _show_competitor_preflight(competitors)
    else:
        print(f"\n  Writing {len(competitors)} competitors to Notion (--yes mode)...")

    # Write to Notion
    print(f"\nWriting to Competitors DB...")
    written = await _write_competitors(notion, competitors_db_id, competitors, force=force)
    print(f"  ✓ {written} competitors written to Notion")

    # Summary
    local   = sum(1 for c in competitors if c.get("type") == "Local")
    organic = sum(1 for c in competitors if c.get("type") == "Organic")
    both    = sum(1 for c in competitors if c.get("type") == "Both")

    print(f"\n{'─'*60}")
    print(f"  Results — {cfg['name']}")
    print(f"{'─'*60}")
    print(f"  Competitors written: {written}")
    print(f"  Local: {local}  |  Organic: {organic}  |  Both: {both}")
    print(f"\n  Next steps:")
    print(f"  1. Review Competitors DB in Notion — fill in any missing details")
    print(f"  2. make battle-plan CLIENT={client_key}  ← generate full SEO strategy")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Auto-seed Competitors DB from SERP analysis")
    parser.add_argument("--client",  required=True)
    parser.add_argument("--limit",   type=int, default=20, help="Max keywords to run SERPs for")
    parser.add_argument("--force",   action="store_true", help="Overwrite existing rows")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip pre-flight prompts")
    parser.add_argument("--min-appearances", type=int, default=2,
                        help="Minimum keyword appearances to include a domain (default: 2)")
    args = parser.parse_args()
    asyncio.run(main(
        client_key=args.client,
        limit=args.limit,
        force=args.force,
        yes=args.yes,
        min_appearances=args.min_appearances,
    ))
