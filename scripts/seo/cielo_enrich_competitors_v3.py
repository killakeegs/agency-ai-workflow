#!/usr/bin/env python3
"""
cielo_enrich_competitors_v3.py — v3 enrichment pass.

Adds to v2:
  - GBP / Google Places API enrichment for any competitor with a local
    presence (review count, rating, GBP URL, categories, hours, address,
    photo count, business status). Auto-upgrades Type=Organic → Both for
    competitors Places finds a Portland/Oregon match on.
  - Top ~20 referring domains via DataForSEO, tagged by kind (news / gov /
    edu / industry / general) so Andrea can scan for local-civic backlinks.
  - Proper JSON-LD schema detection: parses <script type="application/ld+json">
    blocks and recursively walks @graph to extract @type values. Fixes the
    "None visible" false negatives from v2 for major healthcare brands.
  - Competing Keywords field: scannable "#3 'kw a' • #5 'kw b'" summary.
  - Atlas Treatment Center: auto-flip to Status=Partner (new status) with
    explicit note — not a competitor, sister RxMedia client, complementary
    service model, referral partner.

Idempotent / safe to re-run. Re-runs on Status ∈ {Proposed, Active}
(skips Dismissed rows entirely) and only fills EMPTY fields (never
overwrites anything the team has edited).

Usage:
    python3 scripts/seo/cielo_enrich_competitors_v3.py --dry-run
    python3 scripts/seo/cielo_enrich_competitors_v3.py
    python3 scripts/seo/cielo_enrich_competitors_v3.py --limit 2
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import anthropic
import httpx
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
load_dotenv(".env")

from src.config import settings
from src.integrations.notion import NotionClient


CLIENT_KEY      = "cielo_treatment_center"
CLIENT_DOMAIN   = "cielotreatmentcenter.com"
CLIENT_MARKET   = "Portland"   # city token for Places API match verification
CLIENT_STATE    = "OR"         # state token for Places API match verification

DATAFORSEO_BASE = "https://api.dataforseo.com/v3"
LOCATION_CODE   = 2840
LANGUAGE_CODE   = "en"
TOP_N_PER_KW    = 10
TOP_BACKLINKS_N = 20

# Sister agency client that auto-surfaces as a competitor in SERPs.
# Complementary service model (Atlas = OHP/state insurance; Cielo = private pay),
# referral partner, potential interlink — not a true competitor.
PARTNER_DOMAINS: dict[str, str] = {
    "atlastreatmentcenter.com": (
        "NOT A COMPETITOR — Atlas Addiction Treatment Center is a sister "
        "RxMedia client. Complementary service model (Atlas primarily takes "
        "OHP / state insurance; Cielo is primarily private pay). Referral "
        "partner relationship — Atlas feeds Cielo, Cielo feeds Atlas. "
        "Flagged as potential backlink / cross-link opportunity. Status=Partner "
        "means downstream agents treat this domain as OUT-of-competitor-set but "
        "IN-play for interlink suggestions (team-approved only)."
    ),
}

CIELO_POSITIONING = """
Cielo Treatment Center — Portland, Oregon addiction treatment center.
Specialized niche: LGBTQ+, Indigenous (White Bison certified), Young Adult,
ADHD + addiction intersection.

SERVICES OFFERED: IOP, Evening IOP, PHP, MAT, dual diagnosis core,
DUII court-ordered, family programs.

NOT OFFERED: Detox (inpatient medical), long-term residential inpatient.
"""


# ── DataForSEO ────────────────────────────────────────────────────────────────

def _dfs_headers() -> dict:
    tok = base64.b64encode(
        f"{settings.dataforseo_login}:{settings.dataforseo_password}".encode()
    ).decode()
    return {"Authorization": f"Basic {tok}", "Content-Type": "application/json"}


async def fetch_serp(keyword: str) -> list[dict]:
    headers = _dfs_headers()
    payload = [{
        "keyword": keyword, "location_code": LOCATION_CODE,
        "language_code": LANGUAGE_CODE, "device": "desktop", "depth": TOP_N_PER_KW,
    }]
    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(
            f"{DATAFORSEO_BASE}/serp/google/organic/live/regular",
            headers=headers, json=payload,
        )
    if resp.status_code != 200:
        return []
    out: list[dict] = []
    for task in resp.json().get("tasks", []) or []:
        if task.get("status_code") != 20000:
            continue
        for result in (task.get("result") or []):
            for item in (result.get("items") or []):
                if item.get("type") != "organic":
                    continue
                url = (item.get("url") or "").strip()
                if not url:
                    continue
                domain = urlparse(url).netloc.lower().replace("www.", "")
                out.append({
                    "keyword": keyword,
                    "rank":    item.get("rank_group") or item.get("rank_absolute") or 0,
                    "domain":  domain,
                    "url":     url,
                    "title":   item.get("title") or "",
                })
    return out


async def fetch_authority(domain: str) -> dict:
    auth = base64.b64encode(
        f"{settings.dataforseo_login}:{settings.dataforseo_password}".encode()
    ).decode()
    headers = {"Authorization": f"Basic {auth}", "Content-Type": "application/json"}
    d = re.sub(r"https?://", "", domain).rstrip("/").split("/")[0]
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{DATAFORSEO_BASE}/backlinks/summary/live",
            headers=headers, json=[{"target": d, "limit": 1}],
        )
    if resp.status_code != 200:
        return {}
    tasks = resp.json().get("tasks", [{}])
    result = (tasks[0].get("result") or [{}])[0] if tasks else {}
    return {
        "authority_score":   result.get("rank"),
        "referring_domains": result.get("referring_domains"),
        "backlinks":         result.get("backlinks"),
    }


async def fetch_referring_domains(domain: str, limit: int = 20) -> list[dict]:
    """DataForSEO referring_domains/live — list of top referring domains."""
    auth = base64.b64encode(
        f"{settings.dataforseo_login}:{settings.dataforseo_password}".encode()
    ).decode()
    headers = {"Authorization": f"Basic {auth}", "Content-Type": "application/json"}
    d = re.sub(r"https?://", "", domain).rstrip("/").split("/")[0]
    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(
            f"{DATAFORSEO_BASE}/backlinks/referring_domains/live",
            headers=headers,
            json=[{
                "target": d,
                "limit": limit,
                "order_by": ["rank,desc"],
                "exclude_internal_backlinks": True,
            }],
        )
    if resp.status_code != 200:
        return []
    out: list[dict] = []
    for task in resp.json().get("tasks", []) or []:
        if task.get("status_code") != 20000:
            continue
        for result in (task.get("result") or []):
            for item in (result.get("items") or []):
                dom = (item.get("domain") or "").strip().lower()
                if not dom or dom == d:
                    continue
                out.append({
                    "domain":            dom,
                    "rank":              item.get("rank") or 0,
                    "referring_domains": item.get("referring_domains") or 0,
                    "backlinks":         item.get("backlinks") or 0,
                })
    return out


def classify_backlink(domain: str) -> str:
    """Tag a referring domain by kind so the list is scannable for local-civic opportunities."""
    d = domain.lower()
    if d.endswith(".gov"):                         return "gov"
    if d.endswith(".edu"):                         return "edu"
    if d.endswith(".org"):                         return "org"
    news_hints = ["news", "herald", "tribune", "oregonlive", "portlandbusiness", "kgw", "katu",
                  "koin", "opb", "willametteweek", "wweek", "nytimes", "wsj", "washingtonpost",
                  "newsweek", "forbes", "businessinsider"]
    if any(h in d for h in news_hints):           return "news"
    industry_hints = ["recovery", "rehab", "addiction", "health", "medical", "therapy",
                      "samhsa", "nami", "carf", "jointcommission", "psychology", "mental"]
    if any(h in d for h in industry_hints):       return "industry"
    social = ["facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com",
              "youtube.com", "tiktok.com", "pinterest.com", "reddit.com"]
    if d in social:                                return "social"
    return "general"


def format_top_backlinks(backlinks: list[dict]) -> str:
    """Render backlinks as a compact scannable block."""
    if not backlinks:
        return "(no referring domains returned)"
    lines: list[str] = []
    for bl in backlinks:
        tag = classify_backlink(bl["domain"])
        lines.append(f"[{tag}] {bl['domain']} (rank {bl['rank']}, {bl['referring_domains']} RDs)")
    return "\n".join(lines)


# ── Google Places API (GBP enrichment) ────────────────────────────────────────

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") or getattr(settings, "google_api_key", "") or ""


async def places_find_place(business_name: str, market_hint: str = f"{CLIENT_MARKET} {CLIENT_STATE}") -> dict | None:
    """findplacefromtext — returns place_id if a match exists."""
    if not GOOGLE_API_KEY:
        return None
    query = f"{business_name} {market_hint}"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            "https://maps.googleapis.com/maps/api/place/findplacefromtext/json",
            params={
                "input": query, "inputtype": "textquery",
                "fields": "place_id,name,formatted_address,business_status",
                "key": GOOGLE_API_KEY,
            },
        )
    if resp.status_code != 200:
        return None
    data = resp.json()
    candidates = data.get("candidates") or []
    if not candidates:
        return None
    # Prefer the first candidate whose formatted_address mentions our state/market
    for c in candidates:
        addr = (c.get("formatted_address") or "").lower()
        if CLIENT_STATE.lower() in addr or CLIENT_MARKET.lower() in addr or "oregon" in addr:
            return c
    # No regional match — return None so we don't attach a Boston office to a Portland query
    return None


async def places_details(place_id: str) -> dict | None:
    if not GOOGLE_API_KEY or not place_id:
        return None
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            "https://maps.googleapis.com/maps/api/place/details/json",
            params={
                "place_id": place_id,
                "fields": (
                    "name,formatted_address,formatted_phone_number,website,url,"
                    "rating,user_ratings_total,types,business_status,"
                    "opening_hours,current_opening_hours,photos"
                ),
                "key": GOOGLE_API_KEY,
            },
        )
    if resp.status_code != 200:
        return None
    return resp.json().get("result") or None


def format_gbp_details(details: dict) -> str:
    """Render a readable GBP summary block."""
    if not details:
        return ""
    parts: list[str] = []
    status = details.get("business_status") or ""
    if status and status != "OPERATIONAL":
        parts.append(f"⚠️ Business Status: {status}")
    else:
        parts.append(f"Status: {status or 'OPERATIONAL'}")
    if details.get("formatted_address"):
        parts.append(f"Address: {details['formatted_address']}")
    if details.get("formatted_phone_number"):
        parts.append(f"Phone: {details['formatted_phone_number']}")
    if details.get("website"):
        parts.append(f"Website on GBP: {details['website']}")
    types = details.get("types") or []
    if types:
        parts.append(f"Categories: {', '.join(types[:6])}")
    rating = details.get("rating")
    reviews = details.get("user_ratings_total")
    if rating is not None or reviews is not None:
        parts.append(f"Rating: {rating or 'n/a'}  ({reviews or 0} reviews)")
    photos = details.get("photos") or []
    parts.append(f"Photos on GBP: {len(photos)} available via Places API")
    hours = (details.get("current_opening_hours") or details.get("opening_hours") or {}).get("weekday_text") or []
    if hours:
        parts.append("Hours:\n  " + "\n  ".join(hours))
    return "\n".join(parts)


# ── JSON-LD schema detection (fixed for @graph nesting) ──────────────────────

def detect_schema_types(html: str) -> list[str]:
    """
    Parse <script type="application/ld+json"> blocks, walk nested @graph and
    arrays recursively, collect every @type value. Robust against Yoast /
    RankMath / Schema Pro style nested structures that v2's regex missed.
    """
    types: set[str] = set()
    blocks = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, flags=re.DOTALL | re.IGNORECASE,
    )
    for block in blocks:
        try:
            data = json.loads(block.strip())
        except (json.JSONDecodeError, ValueError):
            # Some sites emit trailing commas or HTML comments — try to salvage
            cleaned = re.sub(r',\s*([}\]])', r'\1', block.strip())
            cleaned = re.sub(r'<!--.*?-->', '', cleaned, flags=re.DOTALL)
            try:
                data = json.loads(cleaned)
            except (json.JSONDecodeError, ValueError):
                continue
        _walk_for_types(data, types)
    return sorted(types)


def _walk_for_types(node, out: set[str]) -> None:
    if isinstance(node, dict):
        t = node.get("@type")
        if isinstance(t, str):
            out.add(t)
        elif isinstance(t, list):
            for tt in t:
                if isinstance(tt, str):
                    out.add(tt)
        for v in node.values():
            _walk_for_types(v, out)
    elif isinstance(node, list):
        for item in node:
            _walk_for_types(item, out)


async def fetch_page_text_and_schema(url: str, char_limit: int = 10000) -> tuple[str, list[str]]:
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            })
    except Exception as e:
        return (f"(page fetch failed: {e})", [])
    if resp.status_code != 200:
        return (f"(page fetch returned HTTP {resp.status_code})", [])
    html = resp.text

    schema_types = detect_schema_types(html)

    html_for_text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html_for_text = re.sub(r"<style[^>]*>.*?</style>",   "", html_for_text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html_for_text)
    text = re.sub(r"\s+", " ", text).strip()
    return (text[:char_limit], schema_types)


# ── Notion helpers ────────────────────────────────────────────────────────────

def _rt(text: str, limit: int = 1900) -> dict:
    return {"rich_text": [{"text": {"content": (text or "")[:limit]}}]}


def _title(text: str) -> dict:
    return {"title": [{"text": {"content": (text or "")[:200]}}]}


def _select(name: str) -> dict:
    return {"select": {"name": name}} if name else {"select": None}


def _url_prop(v: str) -> dict:
    return {"url": v or None}


def _number(v) -> dict:
    if v is None or v == "":
        return {"number": None}
    try:
        return {"number": float(v)}
    except (ValueError, TypeError):
        return {"number": None}


def _checkbox(v: bool) -> dict:
    return {"checkbox": bool(v)}


def _plain_title(prop: dict) -> str:
    return "".join(p.get("text", {}).get("content", "") for p in (prop or {}).get("title", []))


def _plain_rt(prop: dict) -> str:
    return "".join(p.get("text", {}).get("content", "") for p in (prop or {}).get("rich_text", []))


def _plain_select(prop: dict | None) -> str:
    if not prop:
        return ""
    sel = prop.get("select")
    return sel.get("name", "") if sel else ""


def _plain_url(prop: dict) -> str:
    return (prop or {}).get("url") or ""


# ── Schema self-heal ──────────────────────────────────────────────────────────

async def ensure_competitor_schema(notion: NotionClient, competitors_db_id: str, dry_run: bool) -> None:
    db = await notion._client.request(path=f"databases/{competitors_db_id}", method="GET")
    existing = db.get("properties", {})

    to_add: dict = {}
    if "Competing Keywords" not in existing:
        to_add["Competing Keywords"] = {"rich_text": {}}
    if "GBP Details" not in existing:
        to_add["GBP Details"] = {"rich_text": {}}
    if "Top Backlinks" not in existing:
        to_add["Top Backlinks"] = {"rich_text": {}}

    # Ensure Partner option exists on Status select
    status_options = (existing.get("Status", {}).get("select", {}).get("options", []))
    if status_options and not any(o.get("name") == "Partner" for o in status_options):
        merged = list(status_options) + [{"name": "Partner", "color": "purple"}]
        if dry_run:
            print("  [DRY] would add Partner option to Status")
        else:
            await notion._client.request(
                path=f"databases/{competitors_db_id}", method="PATCH",
                body={"properties": {"Status": {"select": {"options": merged}}}},
            )
            print("  ✓ added Partner option to Status")

    if to_add:
        if dry_run:
            print(f"  [DRY] would add fields: {list(to_add.keys())}")
        else:
            await notion._client.request(
                path=f"databases/{competitors_db_id}", method="PATCH",
                body={"properties": to_add},
            )
            print(f"  ✓ added fields: {list(to_add.keys())}")
    else:
        print("  ✓ Competitors DB already has all v3 fields")


# ── Claude analysis (schema-aware now) ────────────────────────────────────────

ANALYSIS_PROMPT = """\
You are analyzing one competitor of Cielo Treatment Center for Andrea Tamayo's SEO battle plan.

{positioning}

COMPETITOR: {domain}
Top-ranking page URL: {top_url}

Ranks in Cielo's priority-keyword SERPs at:
{rank_table}

Authority data:
- Authority score (0-1000): {authority_score}
- Referring domains: {referring_domains}
- Total backlinks: {backlinks}

Schema types detected in the page's JSON-LD (authoritative — trust this):
{schema_types}

GBP / Places API summary (only if present):
{gbp_summary}

Top-ranking page content (first ~10KB):
---
{page_text}
---

Produce a JSON object with these fields (no markdown fences, no preamble):

{{
  "clean_name": "the actual brand name as it appears (e.g., 'Hazelden Betty Ford Foundation'). If unclear, title-case the root domain.",
  "page_type": "Home | Service Hub | Service Subpage | Location Page | Blog | Listicle | Directory | FAQ | About | Other",
  "content_depth": "Short | Medium | Medium-Long | Long",
  "uses_faqs": true | false,
  "uses_schema": "comma-separated list of the schema types detected above, or 'None' if the list above is empty",
  "eeat_signals": "brief — CARF / Joint Commission / medical review / accreditation / licensing signals visible on the page",
  "strengths": "1-2 sentences — what drives their rankings",
  "weaknesses": "1-2 sentences — where they are thin / where Cielo could win",
  "competitive_angle": "2-3 sentences — WHY IS THIS A COMPETITOR to Cielo specifically? Direct service overlap, SERP territory only, adjacent service, cross-geography false positive? What should Andrea know about competing with them or confirming they are not a real threat worth pursuing?"
}}

If this is clearly a directory / aggregator / state government / wrong-geography site that slipped through filters and is NOT a real treatment center, set strengths = "Not a real competitor — [reason]" and competitive_angle = "Recommend Dismissing this entry. [Reason.]".
"""


async def analyze_competitor(
    client: anthropic.Anthropic,
    domain: str, top_url: str, rank_table: str,
    authority: dict, schema_types: list[str], gbp_summary: str, page_text: str,
) -> dict:
    prompt = ANALYSIS_PROMPT.format(
        positioning=CIELO_POSITIONING,
        domain=domain, top_url=top_url or "(no top-ranking URL found)",
        rank_table=rank_table,
        authority_score=authority.get("authority_score") or "unknown",
        referring_domains=authority.get("referring_domains") or "unknown",
        backlinks=authority.get("backlinks") or "unknown",
        schema_types=", ".join(schema_types) if schema_types else "(none detected)",
        gbp_summary=gbp_summary or "(no Places API match)",
        page_text=page_text[:10000],
    )
    resp = client.messages.create(
        model=settings.anthropic_model, max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip() if resp.content else ""
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {
            "clean_name": "", "page_type": "Other", "content_depth": "Medium",
            "uses_faqs": False, "uses_schema": "", "eeat_signals": "",
            "strengths": "(Claude output unparseable — re-run)",
            "weaknesses": "",
            "competitive_angle": f"(Claude output unparseable: {raw[:200]})",
        }


# ── Orchestrator ──────────────────────────────────────────────────────────────

async def load_priority_keywords(notion: NotionClient, keywords_db_id: str) -> list[str]:
    entries = await notion.query_database(database_id=keywords_db_id)
    out: list[str] = []
    for e in entries:
        props = e["properties"]
        if _plain_select(props.get("Priority")) == "High" and _plain_select(props.get("Status")) == "Target":
            kw = _plain_title(props.get("Keyword", {}))
            if kw.strip():
                out.append(kw.strip())
    return out


async def load_targetable_competitors(notion: NotionClient, competitors_db_id: str) -> list[dict]:
    """Status=Proposed or Active — skip Dismissed / Partner (already-resolved states)."""
    entries = await notion.query_database(
        database_id=competitors_db_id,
        filter_payload={
            "or": [
                {"property": "Status", "select": {"equals": "Proposed"}},
                {"property": "Status", "select": {"equals": "Active"}},
            ]
        },
    )
    return entries


def domain_of(website: str) -> str:
    return urlparse(website).netloc.lower().replace("www.", "") if website else ""


def format_rank_table(domain: str, serp_rows_by_kw: dict[str, list[dict]]) -> tuple[str, str, str]:
    appearances: list[tuple[str, int, str, str]] = []
    for kw, rows in serp_rows_by_kw.items():
        for r in rows:
            if r["domain"] == domain:
                appearances.append((kw, r["rank"], r["url"], r["title"]))
    if not appearances:
        return ("(no appearances in priority keyword SERPs)", "", "")
    appearances.sort(key=lambda x: x[1])
    top_url = appearances[0][2]
    # Full detail for prompt / Notes
    detail = "\n".join(f"  - #{rank} for '{kw}' — {title[:80]}" for kw, rank, _, title in appearances)
    # Compact single-line for Competing Keywords field
    compact = " • ".join(f"#{rank} '{kw}'" for kw, rank, _, _ in appearances[:6])
    if len(appearances) > 6:
        compact += f" • +{len(appearances)-6} more"
    return (detail, top_url, compact)


async def update_row(
    notion: NotionClient, row: dict,
    enrichment: dict, top_url: str, rank_detail: str, rank_compact: str,
    authority: dict, schema_types: list[str],
    gbp_place_url: str, gbp_details: str, gbp_rating: float | None, gbp_reviews: int | None,
    backlinks_formatted: str, target_cluster_keywords: list[str],
    dry_run: bool,
) -> None:
    props = row["properties"]
    updates: dict = {}

    existing_name = _plain_title(props.get("Competitor Name", {})).strip()
    clean_name = (enrichment.get("clean_name") or "").strip()
    if clean_name and clean_name != existing_name:
        updates["Competitor Name"] = _title(clean_name)

    if not _plain_url(props.get("Top Ranking Page", {})):
        updates["Top Ranking Page"] = _url_prop(top_url)

    if not _plain_rt(props.get("Target Cluster", {})) and target_cluster_keywords:
        updates["Target Cluster"] = _rt(", ".join(target_cluster_keywords[:6]))

    # Always (re)write Competing Keywords so v3 re-runs refresh the ranks
    updates["Competing Keywords"] = _rt(rank_compact or "(no priority-keyword appearances)")

    if not _plain_select(props.get("Content Depth", {})):
        cd = (enrichment.get("content_depth") or "").strip()
        if cd in {"Short", "Medium", "Medium-Long", "Long"}:
            updates["Content Depth"] = _select(cd)

    # Uses FAQs — only flip to True if Claude says True and current is False
    if "Uses FAQs" in props and not props["Uses FAQs"].get("checkbox"):
        if bool(enrichment.get("uses_faqs")):
            updates["Uses FAQs"] = _checkbox(True)

    # Uses Schema — prefer the actual detected types (authoritative) over Claude's string
    if schema_types:
        current = _plain_rt(props.get("Uses Schema", {})).strip()
        new_val = ", ".join(schema_types)
        if not current or current.lower() in ("", "none", "none visible", "(none detected)"):
            updates["Uses Schema"] = _rt(new_val)

    if not _plain_rt(props.get("EEAT Signals", {})):
        updates["EEAT Signals"] = _rt(enrichment.get("eeat_signals", "") or "")

    if not _plain_rt(props.get("Page Type", {})):
        updates["Page Type"] = _rt(enrichment.get("page_type", "") or "")

    if not _plain_rt(props.get("Strengths", {})):
        updates["Strengths"] = _rt(enrichment.get("strengths", "") or "")

    if not _plain_rt(props.get("Weaknesses", {})):
        updates["Weaknesses"] = _rt(enrichment.get("weaknesses", "") or "")

    # Authority numbers — fill only if empty
    if authority:
        if (props.get("Authority Score", {}).get("number") is None) and authority.get("authority_score") is not None:
            updates["Authority Score"] = _number(authority["authority_score"])
        if (props.get("Referring Domains", {}).get("number") is None) and authority.get("referring_domains") is not None:
            updates["Referring Domains"] = _number(authority["referring_domains"])
        if (props.get("Backlinks", {}).get("number") is None) and authority.get("backlinks") is not None:
            updates["Backlinks"] = _number(authority["backlinks"])

    # GBP fields
    if gbp_place_url and not _plain_url(props.get("GBP URL", {})):
        updates["GBP URL"] = _url_prop(gbp_place_url)
    if gbp_reviews is not None and props.get("Review Count", {}).get("number") in (None, 0):
        updates["Review Count"] = _number(gbp_reviews)
    if gbp_rating is not None and props.get("Review Rating", {}).get("number") in (None, 0):
        updates["Review Rating"] = _number(gbp_rating)
    if gbp_details and not _plain_rt(props.get("GBP Details", {})):
        updates["GBP Details"] = _rt(gbp_details)

    # If we found a GBP hit, upgrade Type=Organic → Both (has local presence)
    current_type = _plain_select(props.get("Type", {}))
    if gbp_place_url and current_type == "Organic":
        updates["Type"] = _select("Both")

    # Top Backlinks — always (re)write during enrichment run
    if backlinks_formatted:
        updates["Top Backlinks"] = _rt(backlinks_formatted)

    # Notes rewrite — competitive angle + rank detail + enrichment timestamp
    angle = (enrichment.get("competitive_angle") or "").strip()
    existing_notes = _plain_rt(props.get("Notes", {}))
    new_notes = (
        f"COMPETITIVE ANGLE: {angle}\n\n"
        f"RANKS IN PRIORITY KEYWORDS:\n{rank_detail}\n\n"
        f"(Enriched 2026-04-22 v3. Previous note: {existing_notes[:200]}{'...' if len(existing_notes) > 200 else ''})"
    )
    updates["Notes"] = _rt(new_notes)

    if dry_run:
        print(f"     [DRY] would update {len(updates)} field(s)")
        return

    await notion.update_database_entry(page_id=row["id"], properties=updates)


async def flip_partner(notion: NotionClient, row: dict, reason: str, dry_run: bool) -> None:
    existing_notes = _plain_rt(row["properties"].get("Notes", {}))
    if dry_run:
        print(f"     [DRY] would flip to Partner with note")
        return
    await notion.update_database_entry(
        page_id=row["id"],
        properties={
            "Status": _select("Partner"),
            "Notes":  _rt(f"{reason}\n\n(Original: {existing_notes[:300]})"),
        },
    )


async def main(limit: int | None, dry_run: bool) -> None:
    from config.clients import CLIENTS
    cfg = CLIENTS[CLIENT_KEY]
    keywords_db_id    = cfg["keywords_db_id"]
    competitors_db_id = cfg["competitors_db_id"]

    notion = NotionClient(settings.notion_api_key)
    claude = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    print(f"\n── Competitor enrichment v3 for Cielo {'[DRY RUN]' if dry_run else ''} ──\n")

    print("[1/6] Self-heal Competitors DB schema (Partner status + 3 new fields)...")
    await ensure_competitor_schema(notion, competitors_db_id, dry_run)

    print("\n[2/6] Loading Status ∈ {Proposed, Active} competitors...")
    rows = await load_targetable_competitors(notion, competitors_db_id)
    if limit:
        rows = rows[:limit]
    print(f"  → {len(rows)} row(s) to enrich")

    # Phase 2a: flip Partner domains before any API work
    print("\n[3/6] Flipping partner domains to Status=Partner...")
    enrich_rows: list[dict] = []
    for row in rows:
        domain = domain_of(_plain_url(row["properties"].get("Website", {})))
        if domain in PARTNER_DOMAINS:
            print(f"  ✓ partner: {domain}")
            await flip_partner(notion, row, PARTNER_DOMAINS[domain], dry_run)
        else:
            enrich_rows.append(row)
    print(f"  → {len(enrich_rows)} remain for enrichment")

    if not enrich_rows:
        return

    print("\n[4/6] Re-querying priority keyword SERPs...")
    priority_kws = await load_priority_keywords(notion, keywords_db_id)
    serp_rows_by_kw: dict[str, list[dict]] = {}
    for kw in priority_kws:
        rows2 = await fetch_serp(kw)
        serp_rows_by_kw[kw] = rows2
        print(f"  ✓ '{kw}' → {len(rows2)} results")

    print("\n[5/6] Per-competitor enrichment (authority + GBP + backlinks + schema + Claude)...")
    for i, row in enumerate(enrich_rows, 1):
        props = row["properties"]
        website = _plain_url(props.get("Website", {}))
        domain = domain_of(website)
        name = _plain_title(props.get("Competitor Name", {}))

        print(f"\n  [{i}/{len(enrich_rows)}] {name} ({domain})")

        rank_detail, top_url, rank_compact = format_rank_table(domain, serp_rows_by_kw)
        if not top_url:
            top_url = f"https://{domain}/"
        target_cluster_keywords = [kw for kw, rs in serp_rows_by_kw.items() if any(r["domain"] == domain for r in rs)]

        authority = await fetch_authority(domain)
        print(f"     authority: {authority.get('authority_score', '?')} / {authority.get('referring_domains', '?')} RDs")

        # Top referring domains
        bl = await fetch_referring_domains(domain, limit=TOP_BACKLINKS_N)
        backlinks_formatted = format_top_backlinks(bl)
        tagged_counts = {}
        for b in bl:
            tag = classify_backlink(b["domain"])
            tagged_counts[tag] = tagged_counts.get(tag, 0) + 1
        print(f"     backlinks top {len(bl)}: {tagged_counts}")

        # GBP enrichment via Places API
        gbp_place_url = ""
        gbp_details = ""
        gbp_rating = None
        gbp_reviews = None
        place = await places_find_place(name or domain.split('.')[0])
        if place and place.get("place_id"):
            details = await places_details(place["place_id"])
            if details:
                # Verify website match — prevents attaching GBP of a different
                # Portland business that shares the same name as an out-of-market
                # competitor (e.g. Dallas Greenhouse Treatment vs. unrelated
                # Portland business also called Greenhouse).
                place_website = details.get("website") or ""
                place_domain = urlparse(place_website).netloc.lower().replace("www.", "") if place_website else ""
                if place_domain and place_domain != domain and not place_domain.endswith("." + domain) and not domain.endswith("." + place_domain):
                    print(f"     GBP: Oregon match found but website mismatch ({place_domain} != {domain}) — discarding")
                else:
                    gbp_place_url = details.get("url") or ""
                    gbp_rating = details.get("rating")
                    gbp_reviews = details.get("user_ratings_total")
                    gbp_details = format_gbp_details(details)
                    print(f"     GBP: ⭐ {gbp_rating} ({gbp_reviews} reviews) — {details.get('formatted_address', '')[:60]}")
            else:
                print(f"     GBP: place_id found but details failed")
        else:
            print(f"     GBP: no Oregon-market match")

        # Page + schema detection
        page_text, schema_types = await fetch_page_text_and_schema(top_url)
        print(f"     schema detected: {schema_types or 'none'}")

        # Claude analysis
        enrichment = await analyze_competitor(
            client=claude, domain=domain, top_url=top_url,
            rank_table=rank_detail, authority=authority,
            schema_types=schema_types, gbp_summary=gbp_details, page_text=page_text,
        )
        print(f"     clean_name: {enrichment.get('clean_name')}")
        print(f"     competitive_angle: {(enrichment.get('competitive_angle') or '')[:120]}")

        await update_row(
            notion=notion, row=row,
            enrichment=enrichment, top_url=top_url,
            rank_detail=rank_detail, rank_compact=rank_compact,
            authority=authority, schema_types=schema_types,
            gbp_place_url=gbp_place_url, gbp_details=gbp_details,
            gbp_rating=gbp_rating, gbp_reviews=gbp_reviews,
            backlinks_formatted=backlinks_formatted,
            target_cluster_keywords=target_cluster_keywords,
            dry_run=dry_run,
        )

    print(f"\n[6/6] Done.")
    print(f"  Enriched: {len(enrich_rows)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(limit=args.limit, dry_run=args.dry_run))
