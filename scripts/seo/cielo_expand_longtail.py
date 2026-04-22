#!/usr/bin/env python3
"""
cielo_expand_longtail.py — Pass A of the Cielo keyword expansion.

Uses Andrea's 10 cluster seeds (from the workbook ingestion) as inputs to
DataForSEO's keywords_for_keywords/live endpoint. Pulls related long-tail
variations per cluster, filters out anything already in Cielo's Keywords
DB, and writes new candidates as Priority=Medium so they sit cleanly
alongside Andrea's Priority=High set without overwriting anything.

Andrea's strategic direction is NOT replaced — these are proposals she
can review and promote to Priority=High if she wants to pursue them.

Usage:
    python3 scripts/seo/cielo_expand_longtail.py --dry-run
    python3 scripts/seo/cielo_expand_longtail.py              # live write
    python3 scripts/seo/cielo_expand_longtail.py --per-seed 5 # how many per cluster
    python3 scripts/seo/cielo_expand_longtail.py --min-volume 50
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import anthropic

from src.config import settings
from src.integrations.notion import NotionClient

from scripts.seo.ingest_cielo_workbook import KEYWORDS as ANDREAS_KEYWORDS


CLIENT_KEY         = "cielo_treatment_center"
DATAFORSEO_BASE    = "https://api.dataforseo.com/v3"
LOCATION_CODE_US   = 2840     # USA — since keywords carry geo modifier "Portland Oregon"
LANGUAGE_CODE      = "en"

DEFAULT_PER_SEED   = 10       # per cluster; ideas only — strategic fit decides final selection
DEFAULT_MIN_VOLUME = 0        # LOCAL SEO: volume is informational, not a gate (see seo_mode=local)
DEFAULT_MIN_KEYWORD_WORDS = 3 # genuine long-tail starts at 3 words

# Cielo's positioning, ingested from Andrea's Battle Plan — used to give Claude
# the context for strategic-fit evaluation. Keep this reflective of the
# Battle Plan page; when strategy shifts, update here.
CIELO_POSITIONING = """
Cielo Treatment Center — Portland, Oregon addiction treatment center (1805 NE Sandy Blvd).

POSITIONING: Specialized niche player against institutional generalists (Crestview,
Tree House PDX, Fora Health). Excels at identity-based care — LGBTQ+, Indigenous
(White Bison certified), Young Adult, and ADHD + addiction intersection.

CRITICAL WEAKNESS: Currently invisible for high-volume core local terms
("drug rehab Portland," "addiction treatment Portland") — competitors own top 3.

BLUE OCEAN: "ADHD and addiction treatment Portland" — none of top 3 competitors
rank for this. Clear opportunity.

STRATEGIC CLUSTERS (Andrea Tamayo's battle plan):
- Core Substance Abuse — broad local terms (Create gap; need to establish presence)
- Mental Health — counseling/therapy terms (Optimize existing pages)
- Dual Diagnosis — ADHD + addiction intersection (BLUE OCEAN; Optimize)
- Culturally Specific — Native American addiction treatment
- Age-Specific — young adult rehab, young adult residential
- LGBTQ+ Affirming — lgbtq rehab centers, lgbtq substance abuse treatment

SERVICES OFFERED: IOP, Evening IOP (Mon/Wed/Thu 6-9 PM), PHP, MAT (Suboxone/Vivitrol),
dual diagnosis as core philosophy, DUII court-ordered treatment, family programs.

NOT OFFERED: Detox (inpatient medical), long-term residential inpatient.
"""

# Junk / off-topic filters — these terms almost always indicate the seed
# bled into a vertical we don't serve or into a job/career query.
EXCLUDE_SUBSTRINGS = [
    "salary", "jobs", "career", "license", "certification", "near me hiring",
    "degree", "school", "course", "training program",
    "free", "cheap",  # Cielo isn't competing on free/cheap keywords
    "veteran only",
]


# ── DataForSEO auth ───────────────────────────────────────────────────────────

def _dfs_headers() -> dict:
    if not settings.dataforseo_login or not settings.dataforseo_password:
        raise RuntimeError("DATAFORSEO_LOGIN + DATAFORSEO_PASSWORD required in .env")
    tok = base64.b64encode(
        f"{settings.dataforseo_login}:{settings.dataforseo_password}".encode()
    ).decode()
    return {"Authorization": f"Basic {tok}", "Content-Type": "application/json"}


# ── DataForSEO: keywords_for_keywords/live ────────────────────────────────────

async def _fetch_suggestions_for_seed(seed: str, limit: int) -> list[dict]:
    """DataForSEO Labs keyword_suggestions — returns related long-tail ideas per seed.
    Volume data on this endpoint is often null for hyper-local healthcare terms,
    so we enrich with a second call to search_volume/live."""
    headers = _dfs_headers()
    payload = [{
        "keyword":        seed,
        "location_code":  LOCATION_CODE_US,
        "language_code":  LANGUAGE_CODE,
        "limit":          limit,
        "include_seed_keyword": False,
    }]
    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(
            f"{DATAFORSEO_BASE}/dataforseo_labs/google/keyword_suggestions/live",
            headers=headers,
            json=payload,
        )
    if resp.status_code != 200:
        print(f"  ⚠ suggestions {resp.status_code} for '{seed}': {resp.text[:200]}")
        return []
    out: list[dict] = []
    for task in resp.json().get("tasks", []) or []:
        if task.get("status_code") != 20000:
            print(f"  ⚠ task error for '{seed}': {task.get('status_message')}")
            continue
        for item in (task.get("result") or []):
            for row in (item.get("items") or []):
                kw = (row.get("keyword") or "").strip().lower()
                if kw:
                    out.append({"keyword": kw, "seed": seed})
    return out


async def _enrich_with_volumes(candidates: list[dict]) -> None:
    """In-place: add volume/competition/cpc to each candidate from search_volume/live."""
    if not candidates:
        return
    headers = _dfs_headers()
    # DataForSEO rejects keywords > 10 words or empty
    unique_kws: list[str] = []
    seen: set[str] = set()
    for c in candidates:
        k = c["keyword"]
        if k in seen or len(k.split()) > 10 or not k:
            continue
        seen.add(k)
        unique_kws.append(k)

    vol_map: dict[str, dict] = {}
    for i in range(0, len(unique_kws), 1000):
        batch = unique_kws[i:i + 1000]
        payload = [{
            "keywords":       batch,
            "location_code":  LOCATION_CODE_US,
            "language_code":  LANGUAGE_CODE,
        }]
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{DATAFORSEO_BASE}/keywords_data/google_ads/search_volume/live",
                headers=headers,
                json=payload,
            )
        if resp.status_code != 200:
            print(f"  ⚠ search_volume {resp.status_code}: {resp.text[:200]}")
            continue
        for task in resp.json().get("tasks", []) or []:
            if task.get("status_code") != 20000:
                continue
            for item in (task.get("result") or []):
                kw = (item.get("keyword") or "").strip().lower()
                if not kw:
                    continue
                vol_map[kw] = {
                    "volume":      int(item.get("search_volume") or 0),
                    "competition": str(item.get("competition") or ""),
                    "cpc":         round(float(item.get("cpc") or 0), 2),
                }

    for c in candidates:
        info = vol_map.get(c["keyword"], {})
        c["volume"]      = info.get("volume", 0)
        c["competition"] = info.get("competition", "")
        c["cpc"]         = info.get("cpc", 0.0)


async def fetch_related_keywords(seeds: list[str], per_seed: int) -> list[dict]:
    """
    Two-step: (1) pull keyword_suggestions per seed for ideas, (2) enrich the
    combined list with search_volume/live for volumes. Healthcare long-tail
    often has null volumes on the suggestions endpoint alone, so the second
    call is required to filter meaningfully.
    """
    candidates: list[dict] = []
    for seed in seeds:
        batch = await _fetch_suggestions_for_seed(seed, limit=per_seed * 4)
        candidates.extend(batch)
        print(f"  ✓ seed '{seed}': {len(batch)} ideas")

    print(f"  → {len(candidates)} total ideas; enriching with volume data...")
    await _enrich_with_volumes(candidates)
    with_vol = sum(1 for c in candidates if c.get("volume", 0) > 0)
    print(f"  → {with_vol} / {len(candidates)} have volume > 0")
    return candidates


# ── Filter + cluster assignment ───────────────────────────────────────────────

def seed_to_cluster_map() -> dict[str, str]:
    """Map each of Andrea's seeds to the cluster she assigned it to."""
    return {row["keyword"].lower(): row["cluster"] for row in ANDREAS_KEYWORDS}


def existing_keyword_set() -> set[str]:
    """Lowercased keywords from Andrea's ingestion — used to dedupe proposals."""
    return {row["keyword"].strip().lower() for row in ANDREAS_KEYWORDS}


def filter_candidates(
    candidates: list[dict],
    min_volume: int,
    min_words: int,
) -> list[dict]:
    """
    Strip obvious junk (dedup against Andrea's set, word count, exclusion
    substrings, optional volume gate). LOCAL SEO DEFAULT: min_volume=0 —
    volume is informational, strategic fit decides selection (see Keegan's
    note 2026-04-22 on local long-tail philosophy).
    """
    existing = existing_keyword_set()
    seed_map = seed_to_cluster_map()

    cleaned: list[dict] = []
    seen: set[str] = set()
    for c in candidates:
        kw = c["keyword"]
        if kw in existing or kw in seen:
            continue
        if len(kw.split()) < min_words:
            continue
        if min_volume > 0 and c.get("volume", 0) < min_volume:
            continue
        if any(bad in kw for bad in EXCLUDE_SUBSTRINGS):
            continue
        c["cluster"] = seed_map.get(c["seed"].lower(), "Other")
        seen.add(kw)
        cleaned.append(c)
    return cleaned


# ── Claude strategic-fit evaluation ───────────────────────────────────────────

STRATEGIC_FIT_PROMPT = """\
You are evaluating keyword candidates for Cielo Treatment Center's local SEO strategy.

{positioning}

For EACH candidate keyword below, output one JSON object per line (newline-delimited JSON,
no wrapping array) with these fields:

{{"keyword": "<exact keyword>", "fit": <1|2|3>, "reason": "<one short sentence>"}}

FIT SCORING:
- 3 = Strong fit. Directly aligned with Cielo's niche positioning, a service they offer,
      or an identified Battle Plan priority (blue ocean, LGBTQ+, Indigenous, ADHD, young adult).
      Worth pursuing.
- 2 = Neutral fit. Relevant to the cluster but generic — may be worth pursuing if volume
      justifies, or may be competitor-saturated. Team decides.
- 1 = Weak fit / skip. Off-strategy: service Cielo doesn't offer (detox, inpatient),
      irrelevant geography, wrong audience, or a keyword where established competitors own
      the territory so deeply that Cielo can't realistically compete.

Be terse in the reason. Reference the cluster, the blue ocean, a service mismatch, or
a competitive reality. Do NOT explain the scoring system.

REMEMBER: This is LOCAL SEO. Low volume (10, 20, 50/month) does not mean weak fit if the
intent matches Cielo's niche. A volume-20 keyword targeting LGBTQ+ young adults is worth
more than a volume-500 generic rehab keyword.

CANDIDATES:
{candidates_block}

Output one JSON object per line. No markdown, no preamble.
"""


async def evaluate_strategic_fit(candidates: list[dict]) -> None:
    """Annotate each candidate with fit (1-3) and reason (Claude). In-place."""
    if not candidates:
        return

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    # Build the candidates block
    lines: list[str] = []
    for c in candidates:
        vol = c.get("volume", 0)
        vol_str = f"vol={vol}" if vol > 0 else "vol=unknown (local long-tail)"
        lines.append(f"- {c['keyword']} | cluster={c['cluster']} | {vol_str} | seed={c['seed']}")
    candidates_block = "\n".join(lines)

    prompt = STRATEGIC_FIT_PROMPT.format(
        positioning=CIELO_POSITIONING,
        candidates_block=candidates_block,
    )

    resp = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip() if resp.content else ""

    # Parse newline-delimited JSON
    import json as _json
    fit_map: dict[str, dict] = {}
    for line in raw.splitlines():
        line = line.strip().lstrip("`").strip()
        if not line or not line.startswith("{"):
            continue
        try:
            row = _json.loads(line)
            fit_map[row.get("keyword", "").strip().lower()] = {
                "fit":    int(row.get("fit", 2)),
                "reason": str(row.get("reason", "")).strip(),
            }
        except (ValueError, TypeError):
            continue

    unmatched = 0
    for c in candidates:
        info = fit_map.get(c["keyword"], {})
        c["fit"]    = info.get("fit", 2)
        c["reason"] = info.get("reason", "(Claude did not return a fit assessment)")
        if not info:
            unmatched += 1

    if unmatched:
        print(f"  ⚠ {unmatched}/{len(candidates)} candidates unmatched by Claude annotation")


# ── Notion writer ─────────────────────────────────────────────────────────────

def _rt(text: str, limit: int = 1900) -> dict:
    return {"rich_text": [{"text": {"content": (text or "")[:limit]}}]}


def _title(text: str) -> dict:
    return {"title": [{"text": {"content": (text or "")[:200]}}]}


def _select(name: str) -> dict:
    return {"select": {"name": name}} if name else {"select": None}


async def write_candidates_to_notion(
    candidates: list[dict],
    keywords_db_id: str,
    dry_run: bool,
) -> int:
    notion = NotionClient(settings.notion_api_key)

    entries = await notion.query_database(database_id=keywords_db_id)
    existing_titles: set[str] = set()
    for e in entries:
        items = e["properties"].get("Keyword", {}).get("title", [])
        existing_titles.add(
            "".join(p.get("text", {}).get("content", "") for p in items).strip().lower()
        )

    written = 0
    for c in candidates:
        if c["keyword"] in existing_titles:
            print(f"  ↳ skip (exists): {c['keyword']}")
            continue
        fit = c.get("fit", 2)
        reason = c.get("reason", "")
        fit_label = {3: "STRONG", 2: "NEUTRAL", 1: "WEAK"}.get(fit, "NEUTRAL")
        vol = c.get("volume", 0)
        vol_str = str(vol) if vol > 0 else "unknown (local long-tail)"
        notes = (
            f"Pass A — long-tail expansion 2026-04-22. "
            f"Seed: '{c['seed']}'. Volume: {vol_str}. Competition: {c.get('competition', '')}. "
            f"CPC: ${c.get('cpc', 0)}. Strategic fit: {fit_label}. Reason: {reason} "
            f"Promote to Priority=High to pursue."
        )
        if dry_run:
            print(f"  [DRY] fit={fit} vol={vol:>5} {c['keyword']:55s} — {reason[:60]}")
            written += 1
            continue
        await notion.create_database_entry(
            database_id=keywords_db_id,
            properties={
                "Keyword":                _title(c["keyword"]),
                "Cluster":                _rt(c["cluster"]),
                "Monthly Search Volume":  _rt(vol_str),
                "Our Position":           _rt("-"),
                "Competitor Positions":   _rt(f"(long-tail expansion of seed: {c['seed']})"),
                "Priority":               _select("Medium"),
                "Gap Type":               _select("Create"),
                "Status":                 _select("Proposed"),
                "Notes":                  _rt(notes),
            },
        )
        print(f"  ✓ fit={fit} vol={vol:>5} {c['keyword']:55s} — {reason[:60]}")
        written += 1
    return written


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(per_seed: int, min_volume: int, min_words: int, dry_run: bool) -> None:
    from config.clients import CLIENTS
    cfg = CLIENTS[CLIENT_KEY]
    keywords_db_id = cfg["keywords_db_id"]
    seo_mode = cfg.get("seo_mode", "local")  # default local for legacy clients

    seeds = [row["keyword"] for row in ANDREAS_KEYWORDS]
    print(f"\n── Pass A — Long-tail expansion for Cielo ── {'[DRY RUN]' if dry_run else ''}")
    print(f"  seo_mode={seo_mode} | seeds={len(seeds)} | per-seed={per_seed} | min-volume={min_volume} | min-words={min_words}\n")

    print("[1/4] Fetching related keywords from DataForSEO...")
    candidates = await fetch_related_keywords(seeds, per_seed)
    print(f"  → {len(candidates)} raw candidates\n")

    print("[2/4] Filtering (dedup, word count, exclusion substrings)...")
    filtered = filter_candidates(candidates, min_volume=min_volume, min_words=min_words)
    print(f"  → {len(filtered)} candidates after filters\n")

    if not filtered:
        print("No candidates passed filters.")
        return

    print("[3/4] Strategic fit evaluation (Claude) against Cielo's Battle Plan...")
    await evaluate_strategic_fit(filtered)
    strong = sum(1 for c in filtered if c.get("fit") == 3)
    neutral = sum(1 for c in filtered if c.get("fit") == 2)
    weak = sum(1 for c in filtered if c.get("fit") == 1)
    print(f"  → fit: strong={strong}, neutral={neutral}, weak={weak}\n")

    # Break down by cluster
    by_cluster: dict[str, dict[str, int]] = {}
    for c in filtered:
        cl = by_cluster.setdefault(c["cluster"], {"total": 0, "strong": 0})
        cl["total"] += 1
        if c.get("fit") == 3:
            cl["strong"] += 1
    print("  Candidates by cluster (strong-fit / total):")
    for cluster, counts in sorted(by_cluster.items(), key=lambda x: -x[1]["strong"]):
        print(f"    {counts['strong']:>3} / {counts['total']:>3}  {cluster}")
    print()

    # Sort so strong-fit candidates get written first (easier Andrea review)
    filtered.sort(key=lambda c: (-c.get("fit", 0), -(c.get("volume") or 0)))

    print(f"[4/4] Writing to Cielo's Keywords DB (Priority=Medium, Notes contain strategic fit)...")
    written = await write_candidates_to_notion(filtered, keywords_db_id, dry_run)

    print(f"\n── Summary ──")
    print(f"  Candidates written: {written}")
    print(f"  Strong fit (=3): {strong} — recommended for Andrea to promote to Priority=High first")
    print(f"  Neutral (=2): {neutral} — cluster-relevant but generic/saturated; case-by-case")
    print(f"  Weak (=1): {weak} — off-strategy; review to confirm skip")
    print(f"  All sit at Priority=Medium alongside Andrea's Priority=High set.")
    print(f"  Filter Keywords DB → Priority=Medium in Notion to review.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Long-tail keyword expansion from Andrea's seeds")
    parser.add_argument("--per-seed",    type=int, default=DEFAULT_PER_SEED)
    parser.add_argument("--min-volume",  type=int, default=DEFAULT_MIN_VOLUME)
    parser.add_argument("--min-words",   type=int, default=DEFAULT_MIN_KEYWORD_WORDS)
    parser.add_argument("--dry-run",     action="store_true")
    args = parser.parse_args()
    asyncio.run(main(
        per_seed=args.per_seed,
        min_volume=args.min_volume,
        min_words=args.min_words,
        dry_run=args.dry_run,
    ))
