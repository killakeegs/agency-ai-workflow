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
results for a local business, classify each and assess threat level.

Business:
{business_summary}

Domains found (keyword count | avg position | specific keywords ranked):
{domain_list}

Return ONLY a JSON object (no markdown, no preamble):
{{
  "competitors": [
    {{
      "domain": "example.com",
      "name": "Business Name (if you know it, else leave blank)",
      "type": "Local | Organic | Both",
      "threat": "High | Medium | Low",
      "notes": "one-line note — e.g. dominant local competitor in Frisco, national chain with local branch, regional content site"
    }}
  ]
}}

Type definitions:
  Local    = nearby physical location competitor (same market area)
  Organic  = ranks in organic results; may be national or regional
  Both     = competes in both map pack and organic results

Threat definitions (for a local business):
  High   = keyword_count >= 5 AND avg_position <= 4  (eating your lunch today)
  Medium = keyword_count >= 3 OR avg_position <= 5    (visible, beatable)
  Low    = keyword_count < 3 AND avg_position > 5     (worth watching, not urgent)

Include ALL domains from the input. Order by threat (High first), then keyword_count descending.
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
) -> list[dict]:
    """
    Fetch top 10 organic results for a keyword via DataForSEO SERP API.
    Returns list of {domain, position} dicts.
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

    results = []
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
                    results.append({
                        "domain":   domain,
                        "position": item.get("rank_absolute") or item.get("rank_group") or 99,
                    })

    return results


async def _run_serp_analysis(
    keywords: list[dict],
    location_code: int,
    own_domains: set[str] | None = None,
) -> dict[str, dict]:
    """
    Run SERP call for each keyword.
    Returns domain → {keyword_count, positions, keywords_ranked}
    where keywords_ranked is a list of {keyword, position}.
    """
    # domain → {keyword_count, sum_positions, keywords_ranked: [{keyword, position}]}
    domain_data: dict[str, dict] = defaultdict(lambda: {
        "keyword_count": 0,
        "sum_positions": 0,
        "keywords_ranked": [],
    })

    for i, kw in enumerate(keywords):
        keyword = kw["keyword"]
        print(f"  [{i+1}/{len(keywords)}] {keyword}")
        try:
            results = await _fetch_serp(keyword, location_code=location_code)
            seen_domains: set[str] = set()
            for r in results:
                domain = r["domain"]
                position = r["position"]
                if domain in EXCLUDE_DOMAINS or domain in seen_domains:
                    continue
                if own_domains and domain in own_domains:
                    continue
                seen_domains.add(domain)
                domain_data[domain]["keyword_count"] += 1
                domain_data[domain]["sum_positions"] += position
                domain_data[domain]["keywords_ranked"].append({
                    "keyword":  keyword,
                    "position": position,
                })
        except Exception as e:
            print(f"    ⚠ SERP failed: {e}")
        if i < len(keywords) - 1:
            await asyncio.sleep(0.5)

    # Compute avg_position and sort keywords_ranked by position
    for domain, data in domain_data.items():
        kc = data["keyword_count"]
        data["avg_position"] = round(data["sum_positions"] / kc, 1) if kc else 99
        data["keywords_ranked"].sort(key=lambda x: x["position"])

    return dict(domain_data)


# ── Classify competitors via Claude ──────────────────────────────────────────

async def _classify_competitors(
    domain_data: dict[str, dict],
    business_summary: str,
    min_appearances: int = 2,
) -> list[dict]:
    """
    Use Claude to classify domains as Local / Organic / Both + assign threat tier.
    Enriches each entry with keyword_count, avg_position, and keywords_ranked.
    """
    # Filter to meaningful competitors only
    filtered = {
        domain: data
        for domain, data in domain_data.items()
        if data["keyword_count"] >= min_appearances
    }
    if not filtered:
        filtered = domain_data

    # Sort by keyword_count desc, then avg_position asc (bigger threat first)
    sorted_domains = sorted(
        filtered.items(),
        key=lambda x: (-x[1]["keyword_count"], x[1]["avg_position"]),
    )

    def _fmt_domain_line(domain: str, data: dict) -> str:
        top = ", ".join(
            "{} [#{}]".format(r["keyword"], r["position"])
            for r in data["keywords_ranked"][:3]
        )
        return (
            f"- {domain} | {data['keyword_count']} keywords | "
            f"avg pos {data['avg_position']} | top: {top}"
        )

    domain_lines = "\n".join(
        _fmt_domain_line(domain, data) for domain, data in sorted_domains
    )

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    response = await client.messages.create(
        model=settings.anthropic_model,
        max_tokens=4000,
        messages=[{"role": "user", "content": CLASSIFY_PROMPT.format(
            business_summary=business_summary,
            domain_list=domain_lines,
        )}],
    )
    text = response.content[0].text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    try:
        classified = json.loads(text).get("competitors", [])
    except json.JSONDecodeError:
        classified = [
            {"domain": domain, "name": "", "type": "Organic", "threat": "Medium", "notes": ""}
            for domain, _ in sorted_domains
        ]

    # Merge Claude's classification back with the raw SERP data
    data_map = {domain: data for domain, data in sorted_domains}
    competitors = []
    for comp in classified:
        domain = comp.get("domain", "")
        raw = data_map.get(domain, {})
        comp["keyword_count"]   = raw.get("keyword_count", 0)
        comp["avg_position"]    = raw.get("avg_position", 99)
        comp["keywords_ranked"] = raw.get("keywords_ranked", [])
        competitors.append(comp)

    return competitors


# ── LLM / AI Overview mentions via DataForSEO ────────────────────────────────

async def _fetch_ai_mentions(
    keywords: list[dict],
    own_domains: set[str],
) -> tuple[dict[str, dict], dict[str, int]]:
    """
    For each target keyword, call DataForSEO LLM Mentions search/live.
    Returns:
      - competitor_ai: domain → {ai_mentions, ai_keywords: [{keyword, position}]}
      - client_ai:     client_domain → count of keywords where client appears in AI results
    """
    headers = _dfs_headers()
    competitor_ai: dict[str, dict] = defaultdict(lambda: {"ai_mentions": 0, "ai_keywords": []})
    client_ai_count: dict[str, int] = defaultdict(int)
    subscription_error = False

    print(f"\nFetching AI Overview mentions ({len(keywords)} keywords)...")
    for i, kw in enumerate(keywords):
        keyword = kw["keyword"]
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{DATAFORSEO_BASE}/ai_optimization/llm_mentions/search/live",
                    headers=headers,
                    json=[{
                        "target": [{"keyword": keyword}],
                        "location_code": 2840,
                        "language_code": "en",
                        "limit": 10,
                    }],
                )
                resp.raise_for_status()
                data = resp.json()

            for task in data.get("tasks", []):
                status = task.get("status_code")
                if status == 40204:
                    if not subscription_error:
                        print(f"  ⚠ LLM Mentions API requires a DataForSEO AI Optimization subscription")
                        print(f"     Activate at: app.dataforseo.com/ai-optimization-subscription")
                        subscription_error = True
                    return {}, {}
                if status != 20000:
                    continue
                for result in (task.get("result") or []):
                    for item in (result.get("items") or []):
                        sources = item.get("sources") or []
                        for source in sources:
                            domain = (source.get("domain") or "").lower().removeprefix("www.")
                            if not domain:
                                continue
                            position = source.get("position") or 99
                            if domain in own_domains:
                                # Client's own domain appearing in AI results
                                client_ai_count[domain] += 1
                            else:
                                competitor_ai[domain]["ai_mentions"] += 1
                                competitor_ai[domain]["ai_keywords"].append({
                                    "keyword":  keyword,
                                    "position": position,
                                })

            print(f"  [{i+1}/{len(keywords)}] {keyword}")
        except Exception as e:
            print(f"  ⚠ LLM fetch error for '{keyword}': {e}")

        if i < len(keywords) - 1:
            await asyncio.sleep(0.3)

    return dict(competitor_ai), dict(client_ai_count)


# ── Backlink data via DataForSEO ─────────────────────────────────────────────

async def _fetch_backlink_summaries(domains: list[str]) -> dict[str, dict]:
    """
    Call DataForSEO backlinks/summary/live for each domain.
    Returns domain → {authority_score, referring_domains, backlinks}
    Batches 5 at a time to stay within API limits.
    """
    headers = _dfs_headers()
    results: dict[str, dict] = {}
    BATCH = 5

    for i in range(0, len(domains), BATCH):
        batch = domains[i : i + BATCH]
        payload = [{"target": domain, "include_subdomains": True} for domain in batch]

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{DATAFORSEO_BASE}/backlinks/summary/live",
                    headers=headers,
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()

            subscription_error = False
            for task in data.get("tasks", []):
                status_code = task.get("status_code")
                if status_code == 40204:
                    if not subscription_error:
                        print(f"  ⚠ Backlinks API requires a DataForSEO Backlinks subscription")
                        print(f"     Activate at: app.dataforseo.com/backlinks-subscription")
                        print(f"     Skipping — all other data will still be written to Notion.")
                        subscription_error = True
                    return {}  # exit early — no point batching further
                if status_code != 20000:
                    continue
                for item in (task.get("result") or []):
                    target = (item.get("target") or "").lower().removeprefix("www.")
                    results[target] = {
                        "authority_score":   item.get("rank") or 0,
                        "referring_domains":  item.get("referring_domains") or 0,
                        "backlinks":          item.get("backlinks") or 0,
                    }
        except Exception as e:
            print(f"  ⚠ Backlinks fetch error (batch {i//BATCH + 1}): {e}")

        if i + BATCH < len(domains):
            await asyncio.sleep(0.5)

    return results


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
    print(f"\n  {'#':<4} {'Competitor':<30} {'Threat':<8} {'KWs':<5} {'Pos':<6} {'DA':<5} {'Ref Dom':<9} Top Keywords")
    print(f"  {'─'*4} {'─'*30} {'─'*8} {'─'*5} {'─'*6} {'─'*5} {'─'*9} {'─'*30}")

    for i, comp in enumerate(competitors, 1):
        name = comp.get("name") or comp["domain"]
        if len(name) > 28:
            name = name[:26] + ".."
        top_kws = ", ".join(
            f"{r['keyword']} [#{r['position']}]"
            for r in comp.get("keywords_ranked", [])[:2]
        )
        threat = comp.get("threat", "Medium")
        threat_icon = {"High": "🔴", "Medium": "🟡", "Low": "🟢"}.get(threat, "")
        da  = comp.get("authority_score", "—")
        ref = comp.get("referring_domains", "—")
        print(
            f"  {i:<4} {name:<30} {threat_icon}{threat:<7} {comp.get('keyword_count', 0):<5} "
            f"{str(comp.get('avg_position', '?')):<6} {str(da):<5} {str(ref):<9} {top_kws}"
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

async def _ensure_keyword_count_field(notion: NotionClient, competitors_db_id: str) -> None:
    """Add Keyword Count (number) and Threat (select) fields if missing."""
    try:
        db = await notion._client.request(
            path=f"databases/{competitors_db_id}",
            method="GET",
        )
        existing_props = set(db.get("properties", {}).keys())
        updates = {}
        if "Keyword Count" not in existing_props:
            updates["Keyword Count"] = {"number": {"format": "number"}}
        if "Threat" not in existing_props:
            updates["Threat"] = {
                "select": {
                    "options": [
                        {"name": "High",   "color": "red"},
                        {"name": "Medium", "color": "yellow"},
                        {"name": "Low",    "color": "green"},
                    ]
                }
            }
        if "Avg Position" not in existing_props:
            updates["Avg Position"] = {"number": {"format": "number"}}
        if "Authority Score" not in existing_props:
            updates["Authority Score"] = {"number": {"format": "number"}}
        if "Referring Domains" not in existing_props:
            updates["Referring Domains"] = {"number": {"format": "number"}}
        if "Backlinks" not in existing_props:
            updates["Backlinks"] = {"number": {"format": "number"}}
        if "AI Mentions" not in existing_props:
            updates["AI Mentions"] = {"number": {"format": "number"}}
        if updates:
            await notion._client.request(
                path=f"databases/{competitors_db_id}",
                method="PATCH",
                body={"properties": updates},
            )
    except Exception as e:
        print(f"  ⚠ Could not self-heal Competitors DB schema: {e}")


async def _write_competitors(
    notion: NotionClient,
    competitors_db_id: str,
    competitors: list[dict],
    force: bool = False,
) -> int:
    # Self-heal schema
    await _ensure_keyword_count_field(notion, competitors_db_id)

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
                texts = (props.get("Competitor Name") or props.get("Name") or {}).get("title", [])
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

        threat = comp.get("threat", "Medium")
        if threat not in ["High", "Medium", "Low"]:
            threat = "Medium"

        keyword_count  = comp.get("keyword_count", 0)
        avg_position   = comp.get("avg_position", 0)
        keywords_ranked = comp.get("keywords_ranked", [])

        # Build rich notes: summary line + specific keywords with positions
        notes_parts = [comp.get("notes", "").strip()]
        if keywords_ranked:
            kw_list = ", ".join(
                f"{r['keyword']} [#{r['position']}]"
                for r in keywords_ranked
            )
            notes_parts.append(f"Ranks for: {kw_list}")
        notes = " | ".join(p for p in notes_parts if p)

        properties: dict = {
            "Competitor Name": {"title":  [{"text": {"content": display_name}}]},
            "Type":            {"select": {"name": comp_type}},
            "Threat":          {"select": {"name": threat}},
            "Website":         {"url":    f"https://{domain}"},
            "Notes":           {"rich_text": [{"text": {"content": notes[:2000]}}]},
        }
        if keyword_count:
            properties["Keyword Count"] = {"number": keyword_count}
        if avg_position:
            properties["Avg Position"] = {"number": avg_position}
        if comp.get("authority_score"):
            properties["Authority Score"] = {"number": comp["authority_score"]}
        if comp.get("referring_domains"):
            properties["Referring Domains"] = {"number": comp["referring_domains"]}
        if comp.get("backlinks"):
            properties["Backlinks"] = {"number": comp["backlinks"]}
        if comp.get("ai_mentions"):
            properties["AI Mentions"] = {"number": comp["ai_mentions"]}

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

async def _load_business_summary(notion: NotionClient, cfg: dict) -> tuple[str, set[str]]:
    """Returns (business_summary, own_domains) where own_domains are excluded from competitor results."""
    parts = [f"Business: {cfg.get('name', 'Unknown')}"]
    own_domains: set[str] = set()

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
            website  = _rt("Website") or _rt("Current Website URL") or ""

            if location:
                parts.append(f"Location: {location}")
            if services:
                parts.append(f"Services: {services}")
            if not services and raw:
                parts.append(f"Onboarding description: {raw[:800]}")
            if website:
                from urllib.parse import urlparse
                parsed = urlparse(website if website.startswith("http") else f"https://{website}")
                domain = parsed.netloc.lower().removeprefix("www.")
                if domain:
                    own_domains.add(domain)
    except Exception:
        pass

    # Also pull from Client Info DB
    try:
        result = await notion._client.request(
            path=f"databases/{cfg['client_info_db_id']}/query",
            method="POST", body={},
        )
        for entry in result.get("results", [])[:1]:
            props = entry.get("properties", {})
            for field in ["Current Website URL", "Website", "Website URL"]:
                val = props.get(field, {})
                url = ""
                if val.get("type") == "url":
                    url = val.get("url") or ""
                elif val.get("type") == "rich_text":
                    texts = val.get("rich_text", [])
                    url = texts[0].get("plain_text", "") if texts else ""
                if url:
                    from urllib.parse import urlparse
                    parsed = urlparse(url if url.startswith("http") else f"https://{url}")
                    domain = parsed.netloc.lower().removeprefix("www.")
                    if domain:
                        own_domains.add(domain)
                    break
    except Exception:
        pass

    return "\n".join(parts), own_domains


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
    business_summary, own_domains = await _load_business_summary(notion, cfg)
    if own_domains:
        print(f"  Excluding client's own domain(s): {', '.join(own_domains)}")
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
    domain_data = await _run_serp_analysis(keywords, location_code, own_domains=own_domains)

    if not domain_data:
        print("  ⚠ No domains found in SERP results. Check DataForSEO credentials.")
        sys.exit(1)

    total_domains = len(domain_data)
    above_threshold = sum(1 for d in domain_data.values() if d["keyword_count"] >= min_appearances)
    print(f"\n  Found {total_domains} unique domains")
    print(f"  {above_threshold} appear for {min_appearances}+ keywords (likely competitors)")

    # Classify via Claude
    print("\nClassifying competitors via Claude...")
    competitors = await _classify_competitors(domain_data, business_summary, min_appearances)
    print(f"  Classified {len(competitors)} competitors")

    # Fetch backlink data for all competitors
    print("\nFetching backlink data from DataForSEO...")
    domains = [c["domain"] for c in competitors if c.get("domain")]
    backlink_data = await _fetch_backlink_summaries(domains)
    for comp in competitors:
        bl = backlink_data.get(comp.get("domain", ""), {})
        comp["authority_score"]   = bl.get("authority_score", 0)
        comp["referring_domains"] = bl.get("referring_domains", 0)
        comp["backlinks"]         = bl.get("backlinks", 0)
    with_bl = sum(1 for c in competitors if c.get("authority_score", 0) > 0)
    print(f"  Got backlink data for {with_bl}/{len(competitors)} competitors")

    # Fetch AI / LLM Overview mention data
    ai_competitor_data, client_ai_data = await _fetch_ai_mentions(keywords, own_domains)
    for comp in competitors:
        ai = ai_competitor_data.get(comp.get("domain", ""), {})
        comp["ai_mentions"] = ai.get("ai_mentions", 0)
    with_ai = sum(1 for c in competitors if c.get("ai_mentions", 0) > 0)
    print(f"  Got AI mention data for {with_ai}/{len(competitors)} competitors")

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
    high    = sum(1 for c in competitors if c.get("threat") == "High")
    medium  = sum(1 for c in competitors if c.get("threat") == "Medium")
    low     = sum(1 for c in competitors if c.get("threat") == "Low")

    print(f"\n{'─'*60}")
    print(f"  Results — {cfg['name']}")
    print(f"{'─'*60}")
    print(f"  Competitors written: {written}")
    print(f"  By type:   Local: {local}  |  Organic: {organic}  |  Both: {both}")
    print(f"  By threat: 🔴 High: {high}  |  🟡 Medium: {medium}  |  🟢 Low: {low}")
    if high:
        print(f"\n  🔴 High-threat competitors (address first in Battle Plan):")
        for c in competitors:
            if c.get("threat") == "High":
                da  = f"DA {c['authority_score']}" if c.get("authority_score") else ""
                ref = f"{c['referring_domains']:,} ref domains" if c.get("referring_domains") else ""
                ai  = f"{c['ai_mentions']} AI mentions" if c.get("ai_mentions") else ""
                meta = " · ".join(p for p in [da, ref, ai] if p)
                print(f"     • {c.get('name') or c['domain']} — {c.get('keyword_count', 0)} keywords, avg pos {c.get('avg_position', '?')}{(' · ' + meta) if meta else ''}")

    # Client AI visibility
    if client_ai_data:
        total_client_ai = sum(client_ai_data.values())
        print(f"\n  🤖 Client AI visibility: appears in AI Overviews for {total_client_ai} keyword(s)")
    else:
        print(f"\n  🤖 Client AI visibility: not appearing in AI Overviews for any target keywords yet")
        ai_leaders = sorted(
            [(c.get("name") or c["domain"], c["ai_mentions"]) for c in competitors if c.get("ai_mentions", 0) > 0],
            key=lambda x: x[1], reverse=True,
        )[:3]
        if ai_leaders:
            leaders_str = ", ".join("{} ({})".format(n, m) for n, m in ai_leaders)
            print(f"     Competitors in AI results: {leaders_str}")
    print(f"\n  Next steps:")
    print(f"  1. Review Competitors DB in Notion — remove any that aren't real competitors")
    print(f"  2. Add any local competitors you know about that are missing")
    print(f"  3. make battle-plan CLIENT={client_key}  ← generate full SEO strategy")


async def enrich_only(client_key: str) -> None:
    """
    --enrich-only mode: read existing Competitors DB entries, fetch fresh
    backlink + AI mentions data, and PATCH each entry in-place.
    Does NOT re-run SERP analysis or create new entries.
    """
    cfg = CLIENTS.get(client_key)
    if not cfg:
        print(f"Client '{client_key}' not found")
        sys.exit(1)

    notion = NotionClient(settings.notion_api_key)
    competitors_db_id = cfg.get("competitors_db_id", "")
    keywords_db_id = cfg.get("keywords_db_id", "")

    print(f"\n{'='*60}")
    print(f"  Competitor Enrichment — {cfg['name']}")
    print(f"  (backlinks + AI mentions only — no SERP re-run)")
    print(f"{'='*60}\n")

    # Load existing competitor entries
    result = await notion._client.request(
        path=f"databases/{competitors_db_id}/query",
        method="POST",
        body={"page_size": 100},
    )
    entries = result.get("results", [])
    if not entries:
        print("No competitors found in DB. Run make competitor-research first.")
        sys.exit(1)

    # Build domain → page_id map
    domain_page: list[dict] = []
    for e in entries:
        props = e.get("properties", {})
        site = (props.get("Website") or {}).get("url") or ""
        name_parts = (props.get("Competitor Name") or {}).get("title", [])
        name = name_parts[0].get("plain_text", "") if name_parts else ""
        domain = re.sub(r"^https?://(www\.)?", "", site.lower().rstrip("/"))
        if domain:
            domain_page.append({"page_id": e["id"], "domain": domain, "name": name})

    print(f"  Found {len(domain_page)} competitors to enrich\n")

    # ── Backlinks ──────────────────────────────────────────────────────────────
    print("Fetching backlink data...")
    domains = [d["domain"] for d in domain_page]
    backlink_data = await _fetch_backlink_summaries(domains)
    with_bl = sum(1 for d in domains if d in backlink_data and backlink_data[d].get("authority_score", 0) > 0)
    print(f"  Got backlink data for {with_bl}/{len(domains)} competitors")

    # ── AI mentions ────────────────────────────────────────────────────────────
    _, own_domains = await _load_business_summary(notion, cfg)

    keywords: list[dict] = []
    if keywords_db_id:
        keywords = await _load_target_keywords(notion, keywords_db_id, limit=20)
        print(f"\nFetching AI Overview mentions ({len(keywords)} keywords)...")
    else:
        print("\n  ⚠ No keywords_db_id — skipping AI mentions")

    ai_competitor_data: dict = {}
    client_ai_data: dict = {}
    if keywords:
        ai_competitor_data, client_ai_data = await _fetch_ai_mentions(keywords, own_domains)
        with_ai = sum(1 for d in domains if d in ai_competitor_data and ai_competitor_data[d].get("ai_mentions", 0) > 0)
        print(f"  Got AI mention data for {with_ai}/{len(domains)} competitors with mentions")

    # ── Patch each Notion entry ────────────────────────────────────────────────
    print("\nPatching Notion entries...")
    patched = 0
    for entry in domain_page:
        page_id = entry["page_id"]
        domain = entry["domain"]

        bl = backlink_data.get(domain, {})
        ai = ai_competitor_data.get(domain, {})

        updates: dict = {}
        if bl.get("authority_score", 0) > 0:
            updates["Authority Score"]   = {"number": bl["authority_score"]}
            updates["Referring Domains"] = {"number": bl["referring_domains"]}
            updates["Backlinks"]         = {"number": bl["backlinks"]}
        if ai.get("ai_mentions", 0) > 0:
            updates["AI Mentions"] = {"number": ai["ai_mentions"]}

        if not updates:
            continue

        try:
            await notion._client.request(
                path=f"pages/{page_id}",
                method="PATCH",
                body={"properties": updates},
            )
            bl_str = f"DA {bl.get('authority_score','?')} / {bl.get('referring_domains','?')} ref domains" if bl else ""
            ai_str = f"{ai.get('ai_mentions','?')} AI mentions" if ai.get("ai_mentions") else ""
            info = " · ".join(p for p in [bl_str, ai_str] if p)
            print(f"  ✓ {entry['name'] or domain} — {info}")
            patched += 1
        except Exception as ex:
            print(f"  ⚠ Failed to patch {domain}: {ex}")

    print(f"\n  ✓ Enriched {patched} competitors")

    # Summary
    if client_ai_data:
        total = sum(client_ai_data.values())
        print(f"\n  🤖 Client AI visibility: appears in AI Overviews for {total} keyword(s)")
    else:
        print(f"\n  🤖 Client AI visibility: not appearing in AI Overviews for any target keywords yet")
        ai_leaders = sorted(
            [(d["name"] or d["domain"], ai_competitor_data[d["domain"]]["ai_mentions"])
             for d in domain_page if ai_competitor_data.get(d["domain"], {}).get("ai_mentions", 0) > 0],
            key=lambda x: x[1], reverse=True,
        )[:3]
        if ai_leaders:
            leaders_str = ", ".join("{} ({})".format(n, m) for n, m in ai_leaders)
            print(f"     Competitors in AI results: {leaders_str}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Auto-seed Competitors DB from SERP analysis")
    parser.add_argument("--client",  required=True)
    parser.add_argument("--limit",   type=int, default=20, help="Max keywords to run SERPs for")
    parser.add_argument("--force",   action="store_true", help="Overwrite existing rows")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip pre-flight prompts")
    parser.add_argument("--enrich-only", action="store_true",
                        help="Fetch backlinks + AI mentions for existing entries; skip SERP re-run")
    parser.add_argument("--min-appearances", type=int, default=2,
                        help="Minimum keyword appearances to include a domain (default: 2)")
    args = parser.parse_args()

    if args.enrich_only:
        asyncio.run(enrich_only(client_key=args.client))
    else:
        asyncio.run(main(
            client_key=args.client,
            limit=args.limit,
            force=args.force,
            yes=args.yes,
            min_appearances=args.min_appearances,
        ))
