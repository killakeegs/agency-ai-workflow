#!/usr/bin/env python3
"""
cielo_discover_competitors.py — auto-discover competitors from priority keyword SERPs.

Reads Cielo's Priority=High + Status=Target keywords (Andrea's approved set),
pulls the organic top-10 SERP for each via DataForSEO, aggregates domains
across keywords, filters out directories + the client + already-known
competitors, and proposes net-new competitors in the Competitors DB at
Status=Proposed for Andrea's review.

Why this matters: new treatment centers open, SERPs shift, national brands
encroach on local terms. The system sees the SERP every day — it should
flag what the team might be missing. Andrea's manual list is the starting
point; auto-discovery keeps it current without guesswork.

Andrea reviews by filtering Competitors DB → Status=Proposed. She flips
real competitors to Status=Active (they become part of the competitor set
downstream agents read), or Status=Dismissed for false positives.

Usage:
    python3 scripts/seo/cielo_discover_competitors.py --dry-run
    python3 scripts/seo/cielo_discover_competitors.py
    python3 scripts/seo/cielo_discover_competitors.py --min-appearances 3  # stricter
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.config import settings
from src.integrations.notion import NotionClient


CLIENT_KEY         = "cielo_treatment_center"
CLIENT_DOMAIN      = "cielotreatmentcenter.com"
DATAFORSEO_BASE    = "https://api.dataforseo.com/v3"
LOCATION_CODE      = 2840  # USA — keywords carry geo modifier already
LANGUAGE_CODE      = "en"
DEFAULT_TOP_N      = 10    # per keyword — pull top 10 organic results
DEFAULT_MIN_APPEAR = 2     # domain must appear across ≥ 2 priority keywords

# Directories / aggregators / non-competitor domains — these rank for our
# keywords but they aren't competing treatment centers. Filtered out.
# Mirrors (with minor extensions) the DIRECTORY_DOMAINS list in
# keyword_research.py so behavior stays consistent across the SEO scripts.
EXCLUDE_DOMAINS = {
    "yelp.com", "healthgrades.com", "zocdoc.com", "psychologytoday.com",
    "webmd.com", "mayoclinic.org", "medicalnewstoday.com", "verywellhealth.com",
    "verywellmind.com", "health.com", "cnn.com", "nytimes.com", "latimes.com",
    "oregonlive.com", "wsj.com", "reuters.com",
    "google.com", "maps.google.com", "business.google.com",
    "facebook.com", "instagram.com", "linkedin.com", "youtube.com", "x.com", "twitter.com",
    "wikipedia.org", "reddit.com", "quora.com", "medium.com",
    "indeed.com", "glassdoor.com", "ziprecruiter.com",
    "bbb.org", "angieslist.com", "houzz.com",
    "vitals.com", "ratemds.com", "usnews.com",
    "aamft.org", "apta.org", "asha.org", "aota.org",
    "goodtherapy.org", "betterhelp.com", "talkspace.com", "thriveworks.com",
    # Addiction-specific aggregators that rank for rehab terms but aren't competitors
    "rehabs.com", "recovery.com", "rehab.com", "help.org",
    "addictionresource.com", "addictioncenter.com", "drugabuse.gov",
    "samhsa.gov", "niaaa.nih.gov", "nih.gov", "drugfree.org",
    "startyourrecovery.org", "thetreatmentspecialist.com",
    "betteraddictioncare.com", "rehabnet.com",
    # Generic informational / news sites
    "inc.com", "forbes.com", "businessinsider.com",
}


# ── DataForSEO ────────────────────────────────────────────────────────────────

def _dfs_headers() -> dict:
    if not settings.dataforseo_login or not settings.dataforseo_password:
        raise RuntimeError("DATAFORSEO_LOGIN + DATAFORSEO_PASSWORD required in .env")
    tok = base64.b64encode(
        f"{settings.dataforseo_login}:{settings.dataforseo_password}".encode()
    ).decode()
    return {"Authorization": f"Basic {tok}", "Content-Type": "application/json"}


async def fetch_serp(keyword: str, top_n: int) -> list[dict]:
    """
    DataForSEO Organic SERP live/regular — returns top organic results
    for the keyword. Each item includes url, domain, title, rank.
    """
    headers = _dfs_headers()
    payload = [{
        "keyword":       keyword,
        "location_code": LOCATION_CODE,
        "language_code": LANGUAGE_CODE,
        "device":        "desktop",
        "depth":         top_n,
    }]
    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(
            f"{DATAFORSEO_BASE}/serp/google/organic/live/regular",
            headers=headers,
            json=payload,
        )
    if resp.status_code != 200:
        print(f"  ⚠ SERP {resp.status_code} for '{keyword}': {resp.text[:200]}")
        return []
    out: list[dict] = []
    for task in resp.json().get("tasks", []) or []:
        if task.get("status_code") != 20000:
            print(f"  ⚠ task error '{keyword}': {task.get('status_message')}")
            continue
        for result in (task.get("result") or []):
            for item in (result.get("items") or []):
                # Only organic results — skip Map Pack, ads, People Also Ask, etc.
                if item.get("type") != "organic":
                    continue
                url = (item.get("url") or "").strip()
                if not url:
                    continue
                domain = urlparse(url).netloc.lower().lstrip("www.").replace("www.", "")
                if not domain:
                    continue
                out.append({
                    "keyword":  keyword,
                    "rank":     item.get("rank_group") or item.get("rank_absolute") or 0,
                    "domain":   domain,
                    "url":      url,
                    "title":    item.get("title") or "",
                })
    return out


# ── Notion helpers ────────────────────────────────────────────────────────────

def _rt(text: str, limit: int = 1900) -> dict:
    return {"rich_text": [{"text": {"content": (text or "")[:limit]}}]}


def _title(text: str) -> dict:
    return {"title": [{"text": {"content": (text or "")[:200]}}]}


def _select(name: str) -> dict:
    return {"select": {"name": name}} if name else {"select": None}


def _url_prop(v: str) -> dict:
    return {"url": v or None}


def _plain_rt(prop: dict) -> str:
    return "".join(p.get("text", {}).get("content", "") for p in (prop or {}).get("rich_text", []))


def _plain_title(prop: dict) -> str:
    return "".join(p.get("text", {}).get("content", "") for p in (prop or {}).get("title", []))


def _plain_select(prop: dict | None) -> str:
    if not prop:
        return ""
    sel = prop.get("select")
    return sel.get("name", "") if sel else ""


def _plain_url(prop: dict) -> str:
    return (prop or {}).get("url") or ""


# ── Keyword + competitor loading ──────────────────────────────────────────────

async def load_priority_keywords(notion: NotionClient, keywords_db_id: str) -> list[str]:
    """Pull Priority=High + Status=Target (Andrea's approved set) keywords."""
    entries = await notion.query_database(database_id=keywords_db_id)
    out: list[str] = []
    for e in entries:
        props = e["properties"]
        priority = _plain_select(props.get("Priority"))
        status   = _plain_select(props.get("Status"))
        if priority == "High" and status == "Target":
            kw = "".join(p.get("text", {}).get("content", "") for p in props.get("Keyword", {}).get("title", []))
            if kw.strip():
                out.append(kw.strip())
    return out


async def load_existing_competitors(notion: NotionClient, competitors_db_id: str) -> set[str]:
    """Domains we already track — any row in Competitors DB regardless of Status."""
    entries = await notion.query_database(database_id=competitors_db_id)
    out: set[str] = set()
    for e in entries:
        website = _plain_url(e["properties"].get("Website", {}))
        if website:
            domain = urlparse(website).netloc.lower().replace("www.", "")
            if domain:
                out.add(domain)
        # Also dedupe by name (for entries that don't have a website set)
        name = _plain_title(e["properties"].get("Competitor Name", {})).strip().lower()
        if name:
            out.add(name)
    return out


# ── Aggregation + filtering ───────────────────────────────────────────────────

def aggregate_and_filter(
    serp_rows: list[dict],
    existing_domains: set[str],
    min_appearances: int,
) -> list[dict]:
    """
    Aggregate domain appearances across keywords, filter, return proposed
    competitors sorted by appearance count.
    """
    by_domain: dict[str, dict] = defaultdict(lambda: {"keywords": [], "titles": [], "avg_rank": 0})

    for row in serp_rows:
        d = row["domain"]
        if d == CLIENT_DOMAIN or d.endswith("." + CLIENT_DOMAIN):
            continue
        if d in EXCLUDE_DOMAINS:
            continue
        # Sub-domains of excluded domains
        if any(d.endswith("." + ex) for ex in EXCLUDE_DOMAINS):
            continue
        # TLD exclusion — .gov, .edu, .mil are never competing treatment centers.
        # (Added 2026-04-22 after Andrea flagged oregon.gov in the first run.)
        if d.endswith(".gov") or d.endswith(".edu") or d.endswith(".mil"):
            continue

        entry = by_domain[d]
        entry["keywords"].append(row["keyword"])
        entry["titles"].append(row["title"])
        entry["avg_rank"] += row["rank"]

    proposed: list[dict] = []
    for domain, info in by_domain.items():
        # Dedup by existing competitors (by domain)
        if domain in existing_domains:
            continue
        # Also skip if a sibling domain is already tracked
        if any(ex_d.endswith(domain) or domain.endswith(ex_d) for ex_d in existing_domains if "." in ex_d):
            continue

        appearances = len(info["keywords"])
        if appearances < min_appearances:
            continue

        avg_rank = round(info["avg_rank"] / appearances, 1)

        # Derive a name from the first title found for this domain
        name = ""
        if info["titles"]:
            # Clean up title: "Crestview Recovery | Addiction Treatment Center ..." → "Crestview Recovery"
            raw_title = info["titles"][0]
            for sep in [" | ", " - ", " : ", " — "]:
                if sep in raw_title:
                    raw_title = raw_title.split(sep)[0]
                    break
            name = raw_title.strip()
        if not name:
            # Fallback to domain
            name = domain.split(".")[0].replace("-", " ").title()

        proposed.append({
            "domain":        domain,
            "name":          name,
            "appearances":   appearances,
            "avg_rank":      avg_rank,
            "keywords":      info["keywords"],
        })

    proposed.sort(key=lambda x: (-x["appearances"], x["avg_rank"]))
    return proposed


# ── Write proposals to Notion ─────────────────────────────────────────────────

async def write_proposals(
    notion: NotionClient,
    competitors_db_id: str,
    proposals: list[dict],
    dry_run: bool,
) -> int:
    existing = await load_existing_competitors(notion, competitors_db_id)
    written = 0
    for p in proposals:
        if p["domain"] in existing:
            print(f"  ↳ skip (exists): {p['name']} ({p['domain']})")
            continue

        # Threat heuristic: more appearances = bigger threat
        if p["appearances"] >= 5:
            threat = "High"
        elif p["appearances"] >= 3:
            threat = "Medium"
        else:
            threat = "Low"

        kw_list = ", ".join(f'"{k}"' for k in p["keywords"][:8])
        if len(p["keywords"]) > 8:
            kw_list += f", +{len(p['keywords']) - 8} more"

        notes = (
            f"Auto-discovered 2026-04-22 via SERP aggregation on Cielo's "
            f"Priority=High / Status=Target keywords. Ranks in top {DEFAULT_TOP_N} "
            f"for {p['appearances']} of the priority keywords. Average position: "
            f"{p['avg_rank']}. Keywords: {kw_list}. "
            f"Review: flip Status to Active to add to the tracked competitor set, "
            f"or Dismissed if this isn't a real competitor."
        )

        if dry_run:
            print(f"  [DRY] {p['appearances']} app.  rank~{p['avg_rank']:>4}  threat={threat}  {p['name']} ({p['domain']})")
            written += 1
            continue

        await notion.create_database_entry(
            database_id=competitors_db_id,
            properties={
                "Competitor Name":     _title(p["name"]),
                "Website":             _url_prop(f"https://{p['domain']}"),
                "Status":              _select("Proposed"),
                "Threat":              _select(threat),
                "Type":                _select("Organic"),
                "Notes":               _rt(notes),
            },
        )
        print(f"  ✓ {p['appearances']} app.  rank~{p['avg_rank']:>4}  threat={threat}  {p['name']} ({p['domain']})")
        written += 1
    return written


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(min_appearances: int, top_n: int, dry_run: bool) -> None:
    from config.clients import CLIENTS
    cfg = CLIENTS[CLIENT_KEY]
    keywords_db_id    = cfg["keywords_db_id"]
    competitors_db_id = cfg["competitors_db_id"]

    notion = NotionClient(settings.notion_api_key)

    print(f"\n── Competitor auto-discovery for Cielo ── {'[DRY RUN]' if dry_run else ''}")
    print(f"  Min appearances: {min_appearances} | Top N per keyword: {top_n}\n")

    print("[1/4] Loading priority keywords (Priority=High + Status=Target)...")
    priority_kws = await load_priority_keywords(notion, keywords_db_id)
    print(f"  → {len(priority_kws)} priority keyword(s)")
    if not priority_kws:
        print("  No priority keywords found. Nothing to do.")
        return

    print("\n[2/4] Fetching SERPs from DataForSEO...")
    all_rows: list[dict] = []
    for kw in priority_kws:
        rows = await fetch_serp(kw, top_n)
        all_rows.extend(rows)
        print(f"  ✓ '{kw}' → {len(rows)} organic results")
    print(f"  → {len(all_rows)} total organic SERP entries across {len(priority_kws)} keywords")

    print("\n[3/4] Loading existing competitors + aggregating SERP domains...")
    existing = await load_existing_competitors(notion, competitors_db_id)
    print(f"  → {len(existing)} existing competitor domain(s) in DB")
    proposals = aggregate_and_filter(all_rows, existing, min_appearances)
    print(f"  → {len(proposals)} net-new competitor candidate(s)")

    if not proposals:
        print("No new competitors proposed. Filters may be too strict, or the market is well-covered by Andrea's set.")
        return

    print(f"\n[4/4] Writing proposals to Competitors DB (Status=Proposed)...")
    written = await write_proposals(notion, competitors_db_id, proposals, dry_run)

    print(f"\n── Summary ──")
    print(f"  Proposed: {written}")
    print(f"  Review: filter Competitors DB → Status=Proposed in Notion.")
    print(f"  Flip to Active for real competitors, Dismissed for false positives.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-appearances", type=int, default=DEFAULT_MIN_APPEAR)
    parser.add_argument("--top-n",           type=int, default=DEFAULT_TOP_N)
    parser.add_argument("--dry-run",         action="store_true")
    args = parser.parse_args()
    asyncio.run(main(
        min_appearances=args.min_appearances,
        top_n=args.top_n,
        dry_run=args.dry_run,
    ))
