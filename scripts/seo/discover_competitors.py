#!/usr/bin/env python3
"""
discover_competitors.py — auto-discover competitors from priority keyword SERPs.

For any SEO client: reads their Priority=High + Status=Target keywords
(team-approved set), pulls the organic top-10 SERP for each via DataForSEO,
aggregates domains across keywords, filters directories / the client's
own domain / already-known competitors, and proposes net-new competitors
in the client's Competitors DB at Status=Proposed for team review.

Team reviews in Notion: filter Competitors DB → Status=Proposed, flip
real competitors to Status=Active, false positives to Status=Dismissed,
fellow RxMedia clients to Status=Partner.

Usage:
    make discover-competitors CLIENT=lotus_recovery
    make discover-competitors CLIENT=lotus_recovery DRY=1
    python3 scripts/seo/discover_competitors.py --client x --min-appearances 3
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.config import settings
from src.integrations.notion import NotionClient


DATAFORSEO_BASE    = "https://api.dataforseo.com/v3"
LOCATION_CODE      = 2840  # USA — priority keywords already carry their geo modifier
LANGUAGE_CODE      = "en"
DEFAULT_TOP_N      = 10    # per keyword — pull top 10 organic results
DEFAULT_MIN_APPEAR = 2     # domain must appear across ≥ 2 priority keywords

# Directories / aggregators / non-competitor domains — rank for rehab-adjacent
# keywords but aren't competing treatment centers. Mirror of DIRECTORY_DOMAINS
# in keyword_research.py; keep extensions consistent across scripts.
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
    "rehabs.com", "recovery.com", "rehab.com", "help.org",
    "addictionresource.com", "addictioncenter.com", "drugabuse.gov",
    "samhsa.gov", "niaaa.nih.gov", "nih.gov", "drugfree.org",
    "startyourrecovery.org", "thetreatmentspecialist.com",
    "betteraddictioncare.com", "rehabnet.com",
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
    """DataForSEO Organic SERP live/regular — top organic results per keyword."""
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
            headers=headers, json=payload,
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
                if item.get("type") != "organic":
                    continue
                url = (item.get("url") or "").strip()
                if not url:
                    continue
                domain = urlparse(url).netloc.lower().replace("www.", "")
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

def _plain_title(prop: dict) -> str:
    return "".join(p.get("text", {}).get("content", "") for p in (prop or {}).get("title", []))

def _plain_select(prop: dict | None) -> str:
    if not prop:
        return ""
    sel = prop.get("select")
    return sel.get("name", "") if sel else ""

def _plain_url(prop: dict) -> str:
    return (prop or {}).get("url") or ""


# ── Client domain + roster resolution ────────────────────────────────────────

def resolve_client_domain(cfg: dict) -> str:
    """Derive the client's canonical domain from gsc_site_url or website."""
    url = cfg.get("gsc_site_url") or cfg.get("website") or ""
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    return urlparse(url).netloc.lower().replace("www.", "")


def resolve_sister_client_domains(skip_client_key: str) -> set[str]:
    """
    Build a set of all other RxMedia client domains. When competitor
    discovery finds one of these, we flag it as a Partner rather than
    proposing it as a competitor (sister clients aren't true competitors,
    even if they rank for overlapping terms).
    """
    from config.clients import CLIENTS
    out: set[str] = set()
    for key, cfg in CLIENTS.items():
        if key == skip_client_key:
            continue
        for field in ("gsc_site_url", "website"):
            url = cfg.get(field) or ""
            if not url:
                continue
            if "://" not in url:
                url = "https://" + url
            d = urlparse(url).netloc.lower().replace("www.", "")
            if d:
                out.add(d)
    return out


# ── Keyword + competitor loading ──────────────────────────────────────────────

async def load_priority_keywords(notion: NotionClient, keywords_db_id: str) -> list[str]:
    """Pull Priority=High + Status=Target (team-approved set) keywords."""
    entries = await notion.query_database(database_id=keywords_db_id)
    out: list[str] = []
    for e in entries:
        props = e["properties"]
        priority = _plain_select(props.get("Priority"))
        status   = _plain_select(props.get("Status"))
        if priority == "High" and status == "Target":
            kw = _plain_title(props.get("Keyword", {}))
            if kw.strip():
                out.append(kw.strip())
    return out


async def load_existing_competitors(notion: NotionClient, competitors_db_id: str) -> set[str]:
    """Domains already tracked — any row in Competitors DB regardless of Status."""
    entries = await notion.query_database(database_id=competitors_db_id)
    out: set[str] = set()
    for e in entries:
        website = _plain_url(e["properties"].get("Website", {}))
        if website:
            domain = urlparse(website).netloc.lower().replace("www.", "")
            if domain:
                out.add(domain)
        name = _plain_title(e["properties"].get("Competitor Name", {})).strip().lower()
        if name:
            out.add(name)
    return out


# ── Aggregation + filtering ───────────────────────────────────────────────────

def aggregate_and_filter(
    serp_rows: list[dict],
    client_domain: str,
    existing_domains: set[str],
    sister_domains: set[str],
    min_appearances: int,
) -> tuple[list[dict], list[dict]]:
    """
    Aggregate domain appearances across keywords, filter, return:
      (competitor_proposals, partner_proposals)
    — separate lists so Partner domains land at Status=Partner directly.
    """
    by_domain: dict[str, dict] = defaultdict(lambda: {"keywords": [], "titles": [], "avg_rank": 0})

    for row in serp_rows:
        d = row["domain"]
        if d == client_domain or d.endswith("." + client_domain):
            continue
        if d in EXCLUDE_DOMAINS:
            continue
        if any(d.endswith("." + ex) for ex in EXCLUDE_DOMAINS):
            continue
        if d.endswith(".gov") or d.endswith(".edu") or d.endswith(".mil"):
            continue

        entry = by_domain[d]
        entry["keywords"].append(row["keyword"])
        entry["titles"].append(row["title"])
        entry["avg_rank"] += row["rank"]

    competitors: list[dict] = []
    partners: list[dict] = []
    for domain, info in by_domain.items():
        if domain in existing_domains:
            continue
        if any(ex_d.endswith(domain) or domain.endswith(ex_d) for ex_d in existing_domains if "." in ex_d):
            continue

        appearances = len(info["keywords"])
        if appearances < min_appearances:
            continue

        avg_rank = round(info["avg_rank"] / appearances, 1)

        name = ""
        if info["titles"]:
            raw_title = info["titles"][0]
            for sep in [" | ", " - ", " : ", " — "]:
                if sep in raw_title:
                    raw_title = raw_title.split(sep)[0]
                    break
            name = raw_title.strip()
        if not name:
            name = domain.split(".")[0].replace("-", " ").title()

        record = {
            "domain":      domain,
            "name":        name,
            "appearances": appearances,
            "avg_rank":    avg_rank,
            "keywords":    info["keywords"],
            "is_partner":  domain in sister_domains,
        }
        if record["is_partner"]:
            partners.append(record)
        else:
            competitors.append(record)

    competitors.sort(key=lambda x: (-x["appearances"], x["avg_rank"]))
    partners.sort(key=lambda x: (-x["appearances"], x["avg_rank"]))
    return (competitors, partners)


# ── Write proposals to Notion ─────────────────────────────────────────────────

async def write_proposals(
    notion: NotionClient,
    competitors_db_id: str,
    client_name: str,
    competitors: list[dict],
    partners: list[dict],
    top_n: int,
    dry_run: bool,
) -> tuple[int, int]:
    today_iso = date.today().isoformat()
    written_c = 0
    written_p = 0

    for p in competitors:
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
            f"Auto-discovered {today_iso} via SERP aggregation on {client_name}'s "
            f"Priority=High / Status=Target keywords. Ranks in top {top_n} for "
            f"{p['appearances']} of the priority keywords. Avg position: {p['avg_rank']}. "
            f"Keywords: {kw_list}. Review: flip Status to Active (real competitor) "
            f"or Dismissed (false positive)."
        )

        if dry_run:
            print(f"  [DRY] {p['appearances']} app.  rank~{p['avg_rank']:>4}  threat={threat}  {p['name']} ({p['domain']})")
            written_c += 1
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
        written_c += 1

    for p in partners:
        kw_list = ", ".join(f'"{k}"' for k in p["keywords"][:6])
        notes = (
            f"PARTNER (sister RxMedia client) — auto-detected {today_iso}. "
            f"Appears in {p['appearances']} of {client_name}'s priority-keyword SERPs "
            f"(avg position {p['avg_rank']}) but is not a true competitor. "
            f"Complementary service relationship — referral / interlink opportunity only. "
            f"Downstream agents treat Status=Partner as OUT-of-competitor-set but IN-play "
            f"for interlink suggestions (team-approved only). "
            f"Keywords: {kw_list}."
        )
        if dry_run:
            print(f"  [DRY-PARTNER] {p['appearances']} app.  {p['name']} ({p['domain']})")
            written_p += 1
            continue
        await notion.create_database_entry(
            database_id=competitors_db_id,
            properties={
                "Competitor Name":     _title(p["name"]),
                "Website":             _url_prop(f"https://{p['domain']}"),
                "Status":              _select("Partner"),
                "Threat":              _select("Low"),
                "Type":                _select("Organic"),
                "Notes":               _rt(notes),
            },
        )
        print(f"  ✓ [Partner] {p['appearances']} app.  {p['name']} ({p['domain']})")
        written_p += 1

    return (written_c, written_p)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(client_key: str, min_appearances: int, top_n: int, dry_run: bool) -> None:
    from config.clients import CLIENTS
    cfg = CLIENTS.get(client_key)
    if not cfg:
        print(f"✗ Client '{client_key}' not found")
        sys.exit(1)

    keywords_db_id    = cfg.get("keywords_db_id", "")
    competitors_db_id = cfg.get("competitors_db_id", "")
    if not keywords_db_id or not competitors_db_id:
        print(f"✗ {client_key} missing keywords_db_id or competitors_db_id")
        sys.exit(1)

    client_domain = resolve_client_domain(cfg)
    if not client_domain:
        print(f"✗ Could not resolve client domain from gsc_site_url / website for {client_key}")
        sys.exit(1)

    client_name = cfg.get("name", client_key)
    sister_domains = resolve_sister_client_domains(client_key)

    notion = NotionClient(settings.notion_api_key)

    print(f"\n── Competitor auto-discovery for {client_name} {'[DRY RUN]' if dry_run else ''} ──")
    print(f"  Client domain: {client_domain}")
    print(f"  Sister RxMedia client domains in cross-check: {len(sister_domains)}")
    print(f"  Min appearances: {min_appearances} | Top N per keyword: {top_n}\n")

    print("[1/4] Loading priority keywords (Priority=High + Status=Target)...")
    priority_kws = await load_priority_keywords(notion, keywords_db_id)
    print(f"  → {len(priority_kws)} priority keyword(s)")
    if not priority_kws:
        print("  No priority keywords. Approve some in Keywords DB first.")
        return

    print("\n[2/4] Fetching SERPs from DataForSEO...")
    all_rows: list[dict] = []
    for kw in priority_kws:
        rows = await fetch_serp(kw, top_n)
        all_rows.extend(rows)
        print(f"  ✓ '{kw}' → {len(rows)} organic results")
    print(f"  → {len(all_rows)} total SERP entries")

    print("\n[3/4] Loading existing competitors + aggregating...")
    existing = await load_existing_competitors(notion, competitors_db_id)
    print(f"  → {len(existing)} existing competitor domain(s) in DB")
    competitors, partners = aggregate_and_filter(
        all_rows, client_domain, existing, sister_domains, min_appearances
    )
    print(f"  → {len(competitors)} competitor candidate(s) | {len(partners)} partner(s) detected")

    if not competitors and not partners:
        print("\nNothing new to propose. Market may already be well-covered by the team's set.")
        return

    print(f"\n[4/4] Writing to Competitors DB (Status=Proposed / Status=Partner)...")
    written_c, written_p = await write_proposals(
        notion, competitors_db_id, client_name, competitors, partners, top_n, dry_run
    )

    print(f"\n── Summary ──")
    print(f"  Competitors proposed: {written_c}")
    print(f"  Partners auto-flagged: {written_p}")
    print(f"  Review: filter Competitors DB → Status=Proposed in Notion.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Auto-discover competitors from priority keyword SERPs")
    parser.add_argument("--client", required=True, help="client_key (e.g. lotus_recovery)")
    parser.add_argument("--min-appearances", type=int, default=DEFAULT_MIN_APPEAR)
    parser.add_argument("--top-n",           type=int, default=DEFAULT_TOP_N)
    parser.add_argument("--dry-run",         action="store_true")
    args = parser.parse_args()
    asyncio.run(main(
        client_key=args.client,
        min_appearances=args.min_appearances,
        top_n=args.top_n,
        dry_run=args.dry_run,
    ))
