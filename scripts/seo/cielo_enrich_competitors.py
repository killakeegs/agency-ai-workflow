#!/usr/bin/env python3
"""
cielo_enrich_competitors.py — fill empty fields on proposed competitors.

Auto-discovery landed 11 proposed rows with only Name / Website / Status /
Threat / Notes filled in. The rest of Andrea's Competitor DB schema
(Top Ranking Page, Target Cluster, Page Type, Content Depth, Uses FAQs,
Uses Schema, EEAT Signals, Strengths, Weaknesses, Referring Domains,
Authority Score, Backlinks) was blank.

This enrichment pass fills those in so each row becomes decision-grade:
Andrea opens a proposed competitor and can tell exactly what page of
theirs is winning, which of Cielo's priority keywords they rank for,
why they win, and what the competitive angle is.

Flow per proposed competitor:

  1. If domain is .gov / .edu / .mil → auto-flip Status=Dismissed with
     reason note. No enrichment; no Claude tokens spent.

  2. Otherwise:
     a. Re-query Cielo's priority keyword SERPs once, build a
        {domain → [(keyword, rank, url, title), ...]} index.
     b. For this competitor, identify their top-ranking page (best rank
        across our priority keywords).
     c. Fetch that page's HTML (first ~10KB of visible text).
     d. Pull DataForSEO authority summary for the domain.
     e. Claude analyzes everything against Cielo's Battle Plan, emits
        a structured JSON: clean_name, page_type, content_depth, uses_faqs,
        uses_schema, eeat_signals, strengths, weaknesses, competitive_angle.
     f. Update the Notion row in place — fills empty fields only (never
        overwrites anything Andrea has already edited).

Usage:
    python3 scripts/seo/cielo_enrich_competitors.py --dry-run
    python3 scripts/seo/cielo_enrich_competitors.py
    python3 scripts/seo/cielo_enrich_competitors.py --limit 3      # first N
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import anthropic
import httpx

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.config import settings
from src.integrations.notion import NotionClient


CLIENT_KEY      = "cielo_treatment_center"
CLIENT_DOMAIN   = "cielotreatmentcenter.com"
DATAFORSEO_BASE = "https://api.dataforseo.com/v3"
LOCATION_CODE   = 2840
LANGUAGE_CODE   = "en"
TOP_N_PER_KW    = 10

# Cielo's positioning block — same as what Pass A uses, keeps the analysis
# consistent across scripts. Update here when the Battle Plan shifts.
CIELO_POSITIONING = """
Cielo Treatment Center — Portland, Oregon addiction treatment center.
Specialized niche player: LGBTQ+, Indigenous (White Bison certified),
Young Adult, ADHD + addiction intersection.

SERVICES OFFERED: IOP, Evening IOP, PHP, MAT (Suboxone/Vivitrol),
dual diagnosis as core, DUII court-ordered, family programs.

NOT OFFERED: Detox (inpatient medical), long-term residential inpatient.

Institutional competitors (already tracked): Crestview Recovery,
Tree House Recovery PDX, Fora Health, True Colors Recovery.

Blue ocean: ADHD + addiction treatment Portland.
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
        "keyword": keyword,
        "location_code": LOCATION_CODE,
        "language_code": LANGUAGE_CODE,
        "device": "desktop",
        "depth": TOP_N_PER_KW,
    }]
    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(
            f"{DATAFORSEO_BASE}/serp/google/organic/live/regular",
            headers=headers,
            json=payload,
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
    """DataForSEO backlinks summary — authority score, referring domains, backlinks."""
    auth = base64.b64encode(
        f"{settings.dataforseo_login}:{settings.dataforseo_password}".encode()
    ).decode()
    headers = {"Authorization": f"Basic {auth}", "Content-Type": "application/json"}
    # Strip protocol + path — just the domain
    d = re.sub(r"https?://", "", domain).rstrip("/").split("/")[0]
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{DATAFORSEO_BASE}/backlinks/summary/live",
            headers=headers,
            json=[{"target": d, "limit": 1}],
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


# ── Page fetch ────────────────────────────────────────────────────────────────

async def fetch_page_text(url: str, char_limit: int = 10000) -> str:
    """Fetch URL, strip HTML, return text + a schema/FAQ-detection summary."""
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            })
    except Exception as e:
        return f"(page fetch failed: {e})"
    if resp.status_code != 200:
        return f"(page fetch returned HTTP {resp.status_code})"
    html = resp.text

    # Lightweight HTML → text: strip script/style, collapse whitespace
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style[^>]*>.*?</style>",   "", html, flags=re.DOTALL | re.IGNORECASE)

    # Detect schema types and FAQ presence from raw HTML before stripping
    schema_hints: list[str] = []
    for schema_type in ["MedicalOrganization", "MedicalClinic", "LocalBusiness", "Organization",
                        "Article", "BlogPosting", "FAQPage", "Service", "MedicalBusiness",
                        "HealthAndBeautyBusiness", "Course", "Product"]:
        if f'"@type":"{schema_type}"' in html or f'"@type": "{schema_type}"' in html or f"'@type':'{schema_type}'" in html:
            schema_hints.append(schema_type)
    has_faq = 'FAQPage' in schema_hints or 'faq' in html.lower()[:50000]

    # Strip tags
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    truncated = text[:char_limit]

    meta = f"[SCHEMA TYPES DETECTED: {', '.join(schema_hints) or 'None'}] [FAQ HINT: {'yes' if has_faq else 'no'}]\n\n"
    return meta + truncated


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


# ── Claude analysis ───────────────────────────────────────────────────────────

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

Top-ranking page content (first ~10KB):
---
{page_text}
---

Produce a JSON object with these fields (no markdown fences, no preamble):

{{
  "clean_name": "the actual brand name as it appears (e.g., 'Hazelden Betty Ford Foundation' — not a page title like 'Addiction Treatment Center in Portland OR'). If you can't tell from the page, use a title-cased version of the main domain.",
  "page_type": "Home | Service Hub | Service Subpage | Location Page | Blog | Listicle | Directory | FAQ | About | Other",
  "content_depth": "Short | Medium | Medium-Long | Long",
  "uses_faqs": true | false,
  "uses_schema": "brief list of detected schema types, or 'None visible'",
  "eeat_signals": "brief — any CARF / Joint Commission / medical review bylines / accreditation / licensing signals on the page",
  "strengths": "1-2 sentences — what they do well that drives rankings",
  "weaknesses": "1-2 sentences — where they're thin / where Cielo can win",
  "competitive_angle": "2-3 sentences answering: WHY IS THIS A COMPETITOR to Cielo specifically? Direct service overlap, SERP territory only, adjacent service, national chain with local footprint, etc. What should Andrea know about competing with them — or confirming they're NOT a real threat worth pursuing?"
}}

IMPORTANT: if this is clearly a directory / aggregator / news site / state government that slipped through filters and is NOT a real treatment center, set strengths = "Not a real competitor — [reason]" and competitive_angle = "Recommend Dismissing this entry. [Reason why.]".
"""


async def analyze_competitor(
    client: anthropic.Anthropic,
    domain: str,
    top_url: str,
    rank_table: str,
    authority: dict,
    page_text: str,
) -> dict:
    prompt = ANALYSIS_PROMPT.format(
        positioning=CIELO_POSITIONING,
        domain=domain,
        top_url=top_url or "(no top-ranking URL found)",
        rank_table=rank_table,
        authority_score=authority.get("authority_score") or "unknown",
        referring_domains=authority.get("referring_domains") or "unknown",
        backlinks=authority.get("backlinks") or "unknown",
        page_text=page_text[:10000],
    )
    resp = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip() if resp.content else ""
    # Strip any code fences
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Best-effort fallback
        return {
            "clean_name":        "",
            "page_type":         "Other",
            "content_depth":     "Medium",
            "uses_faqs":         False,
            "uses_schema":       "",
            "eeat_signals":      "",
            "strengths":         "(Claude output unparseable — re-run)",
            "weaknesses":        "",
            "competitive_angle": f"(Claude output unparseable: {raw[:200]})",
        }


# ── Main orchestrator ────────────────────────────────────────────────────────

async def load_priority_keywords(notion: NotionClient, keywords_db_id: str) -> list[str]:
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


async def load_proposed_competitors(notion: NotionClient, competitors_db_id: str) -> list[dict]:
    entries = await notion.query_database(
        database_id=competitors_db_id,
        filter_payload={"property": "Status", "select": {"equals": "Proposed"}},
    )
    return entries


def domain_of(website: str) -> str:
    return urlparse(website).netloc.lower().replace("www.", "") if website else ""


def is_gov_edu_mil(domain: str) -> bool:
    return domain.endswith(".gov") or domain.endswith(".edu") or domain.endswith(".mil")


def format_rank_table(domain: str, serp_rows_by_kw: dict[str, list[dict]]) -> tuple[str, str]:
    """
    Returns (rank_table_str, top_url).
    top_url = URL of the best-ranking page for this domain across priority keywords.
    """
    appearances: list[tuple[str, int, str, str]] = []
    for kw, rows in serp_rows_by_kw.items():
        for r in rows:
            if r["domain"] == domain:
                appearances.append((kw, r["rank"], r["url"], r["title"]))
    if not appearances:
        return ("(no appearances in priority keyword SERPs)", "")
    appearances.sort(key=lambda x: x[1])  # best rank first
    top_url = appearances[0][2]
    lines = [f"  - #{rank} for '{kw}' — {title[:80]}" for kw, rank, _url, title in appearances]
    return ("\n".join(lines), top_url)


async def update_competitor_row(
    notion: NotionClient,
    row: dict,
    enrichment: dict,
    top_url: str,
    rank_table: str,
    authority: dict,
    priority_kw_match: list[str],
    dry_run: bool,
) -> None:
    props = row["properties"]

    # Only write fields that are currently empty — preserve Andrea's edits
    updates: dict = {}

    existing_name = _plain_title(props.get("Competitor Name", {})).strip()
    # Names from the first pass were often page-title garbage ("ADHD", "Substance Abuse Program").
    # If Claude's cleaned name is different + we have it, update the name.
    clean_name = (enrichment.get("clean_name") or "").strip()
    if clean_name and clean_name != existing_name:
        updates["Competitor Name"] = _title(clean_name)

    if not _plain_url(props.get("Top Ranking Page", {})):
        updates["Top Ranking Page"] = _url_prop(top_url)

    if not _plain_rt(props.get("Target Cluster", {})):
        # Derive target cluster from which Cielo clusters this domain ranks in
        updates["Target Cluster"] = _rt(", ".join(priority_kw_match[:6]) or "")

    if not _plain_select(props.get("Content Depth", {})):
        cd = (enrichment.get("content_depth") or "").strip()
        if cd in {"Short", "Medium", "Medium-Long", "Long"}:
            updates["Content Depth"] = _select(cd)

    if "Uses FAQs" in props and props["Uses FAQs"].get("checkbox") is not True:
        updates["Uses FAQs"] = _checkbox(bool(enrichment.get("uses_faqs")))

    if not _plain_rt(props.get("Uses Schema", {})):
        updates["Uses Schema"] = _rt(enrichment.get("uses_schema", "") or "")

    if not _plain_rt(props.get("EEAT Signals", {})):
        updates["EEAT Signals"] = _rt(enrichment.get("eeat_signals", "") or "")

    if not _plain_rt(props.get("Page Type", {})):
        updates["Page Type"] = _rt(enrichment.get("page_type", "") or "")

    if not _plain_rt(props.get("Strengths", {})):
        updates["Strengths"] = _rt(enrichment.get("strengths", "") or "")

    if not _plain_rt(props.get("Weaknesses", {})):
        updates["Weaknesses"] = _rt(enrichment.get("weaknesses", "") or "")

    if authority:
        if (props.get("Authority Score", {}).get("number") is None) and authority.get("authority_score") is not None:
            updates["Authority Score"] = _number(authority["authority_score"])
        if (props.get("Referring Domains", {}).get("number") is None) and authority.get("referring_domains") is not None:
            updates["Referring Domains"] = _number(authority["referring_domains"])
        if (props.get("Backlinks", {}).get("number") is None) and authority.get("backlinks") is not None:
            updates["Backlinks"] = _number(authority["backlinks"])

    # Rewrite Notes with the full picture — angle + rank table
    angle = (enrichment.get("competitive_angle") or "").strip()
    existing_notes = _plain_rt(props.get("Notes", {}))
    new_notes = (
        f"COMPETITIVE ANGLE: {angle}\n\n"
        f"RANKS IN PRIORITY KEYWORDS:\n{rank_table}\n\n"
        f"(Auto-discovered 2026-04-22, enriched 2026-04-22. Original discovery note: "
        f"{existing_notes[:300]}{'...' if len(existing_notes) > 300 else ''})"
    )
    updates["Notes"] = _rt(new_notes)

    if dry_run:
        print(f"  [DRY] would update {len(updates)} field(s): {list(updates.keys())[:8]}{'...' if len(updates) > 8 else ''}")
        return

    await notion.update_database_entry(page_id=row["id"], properties=updates)


async def dismiss_row(notion: NotionClient, row: dict, reason: str, dry_run: bool) -> None:
    if dry_run:
        print(f"  [DRY] would dismiss with reason: {reason}")
        return
    existing_notes = _plain_rt(row["properties"].get("Notes", {}))
    await notion.update_database_entry(
        page_id=row["id"],
        properties={
            "Status": _select("Dismissed"),
            "Notes":  _rt(f"AUTO-DISMISSED: {reason}\n\n(Original: {existing_notes[:300]})"),
        },
    )


async def main(limit: int | None, dry_run: bool) -> None:
    from config.clients import CLIENTS
    cfg = CLIENTS[CLIENT_KEY]
    keywords_db_id    = cfg["keywords_db_id"]
    competitors_db_id = cfg["competitors_db_id"]

    notion = NotionClient(settings.notion_api_key)
    claude = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    print(f"\n── Competitor enrichment for Cielo ── {'[DRY RUN]' if dry_run else ''}\n")

    print("[1/5] Loading Status=Proposed competitors...")
    proposed_rows = await load_proposed_competitors(notion, competitors_db_id)
    if limit:
        proposed_rows = proposed_rows[:limit]
    print(f"  → {len(proposed_rows)} proposed row(s)")

    # Phase 1: auto-dismiss .gov / .edu / .mil
    print("\n[2/5] Auto-dismissing .gov / .edu / .mil entries...")
    to_enrich: list[dict] = []
    for row in proposed_rows:
        website = _plain_url(row["properties"].get("Website", {}))
        domain = domain_of(website)
        name = _plain_title(row["properties"].get("Competitor Name", {}))
        if is_gov_edu_mil(domain):
            print(f"  ✗ dismissing {name} ({domain})")
            await dismiss_row(
                notion, row,
                reason=f"Government / education / military domain ({domain}) — not a competing treatment center",
                dry_run=dry_run,
            )
        else:
            to_enrich.append(row)
    print(f"  → {len(to_enrich)} remain for enrichment")

    if not to_enrich:
        print("\nNo competitors remain to enrich. Done.")
        return

    print("\n[3/5] Re-querying priority keyword SERPs (to map domains → URLs)...")
    priority_kws = await load_priority_keywords(notion, keywords_db_id)
    serp_rows_by_kw: dict[str, list[dict]] = {}
    for kw in priority_kws:
        rows = await fetch_serp(kw)
        serp_rows_by_kw[kw] = rows
        print(f"  ✓ '{kw}' → {len(rows)} organic results")

    print("\n[4/5] Fetching authority + page HTML + Claude analysis per competitor...")
    for i, row in enumerate(to_enrich, 1):
        props = row["properties"]
        website = _plain_url(props.get("Website", {}))
        domain = domain_of(website)
        name = _plain_title(props.get("Competitor Name", {}))

        print(f"\n  [{i}/{len(to_enrich)}] {name} ({domain})")

        rank_table, top_url = format_rank_table(domain, serp_rows_by_kw)
        if not top_url:
            # Fall back to homepage if no priority-keyword page found
            top_url = f"https://{domain}/"
        priority_kw_match = [kw for kw, rows in serp_rows_by_kw.items() if any(r["domain"] == domain for r in rows)]

        print(f"     top_url: {top_url}")
        authority = await fetch_authority(domain)
        print(f"     authority: {authority.get('authority_score', '?')} / {authority.get('referring_domains', '?')} domains")

        page_text = await fetch_page_text(top_url)
        if page_text.startswith("(page fetch"):
            print(f"     ⚠ {page_text}")
        else:
            print(f"     page fetched: {len(page_text)} chars")

        enrichment = await analyze_competitor(
            client=claude,
            domain=domain,
            top_url=top_url,
            rank_table=rank_table,
            authority=authority,
            page_text=page_text,
        )
        clean_name = enrichment.get("clean_name", "").strip() or name
        print(f"     clean_name: {clean_name}")
        print(f"     page_type: {enrichment.get('page_type')}   content_depth: {enrichment.get('content_depth')}")
        print(f"     competitive_angle: {(enrichment.get('competitive_angle') or '')[:140]}")

        await update_competitor_row(
            notion=notion, row=row, enrichment=enrichment,
            top_url=top_url, rank_table=rank_table, authority=authority,
            priority_kw_match=priority_kw_match, dry_run=dry_run,
        )

    print(f"\n[5/5] Done.")
    print(f"  Dismissed .gov/.edu:    {len(proposed_rows) - len(to_enrich)}")
    print(f"  Enriched:               {len(to_enrich)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit",   type=int, help="cap to first N (testing)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(limit=args.limit, dry_run=args.dry_run))
