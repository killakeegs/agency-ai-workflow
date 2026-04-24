"""
keyword_discovery.py — Step 1 of the SEO keyword pipeline.

Reads a client's Business Profile + Client Info + metadata, asks Claude to
generate a comprehensive pool of core keyword candidates spanning every
service × substance × population × insurance × program-length × terminology
combination the client actually offers, validates each with DataForSEO for
search volume, runs the same deterministic filters as Pass A (vertical
mismatch / geo allowlist / branded competitors), scores strategic fit with
Claude, and writes proposals to the client's Keywords DB at
Priority=Medium / Status=Proposed.

Team then reviews, promotes keepers to Priority=High / Status=Target, and
Step 2 (expand-longtail) runs from those approved seeds to find long-tail
variants.

Fundamental difference from expand-longtail:
  expand-longtail takes EXISTING seeds and expands via DataForSEO's
    semantic suggestions — output stays within the seed's semantic family.
  discover-keywords reads the business's actual facts and generates
    core terms SYSTEMATICALLY across every relevant category, so no
    semantic family gets missed.

Both are needed. discover-keywords runs once per client onboarding (and
re-runs when BP changes materially). expand-longtail runs every time we
want long-tail coverage of newly-approved seeds.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
from datetime import datetime

import anthropic
import httpx

from config.keyword_taxonomy import taxonomy_for
from src.config import settings
from src.integrations.business_profile import load_business_profile
from src.integrations.notion import NotionClient

# Reuse filtering + Claude-fit + writer infrastructure from expand-longtail so
# we don't drift. expand_longtail has already solved these problems cleanly.
from scripts.seo.expand_longtail import (
    CTX,
    _derive_state_allowlist,
    _load_competitor_brand_roots,
    evaluate_strategic_fit,
    filter_candidates,
)


DATAFORSEO_BASE  = "https://api.dataforseo.com/v3"
LOCATION_CODE_US = 2840
LANGUAGE_CODE    = "en"

DEFAULT_TARGET_CANDIDATES = 150   # Claude is asked to produce ~this many


# ── Client context assembly ──────────────────────────────────────────────────

def _build_positioning(cfg: dict, bp_text: str, taxonomy: dict) -> str:
    """Rich positioning string fed to Claude. Includes BP + taxonomy cues."""
    verticals = cfg.get("vertical") or []
    if isinstance(verticals, str):
        verticals = [verticals]
    seo_mode = (cfg.get("seo_mode") or "local").lower()

    parts = [
        f"CLIENT: {cfg.get('name', cfg.get('client_id', '?'))}",
        f"Vertical(s): {', '.join(verticals) or 'unspecified'}",
        f"SEO mode: {seo_mode}",
        f"Canonical address: {cfg.get('canonical_address') or 'unspecified'}",
        f"Website: {cfg.get('website') or cfg.get('gsc_site_url') or '?'}",
    ]

    if bp_text.strip():
        parts.append("\nBUSINESS PROFILE (authoritative source of fact):")
        parts.append(bp_text[:30_000])
    else:
        parts.append("\n⚠ Business Profile is empty. Keywords will be based on "
                     "clients.json metadata alone, which is thin — expect lower-"
                     "quality output and re-run after populating BP.")

    # Taxonomy hints — the menu Claude maps against
    parts.append("\nTAXONOMY (vertical menu — map the client's actual offerings to these):")
    for key, items in taxonomy.items():
        if items:
            parts.append(f"  {key}: {', '.join(items)}")

    return "\n".join(parts)


# ── Claude keyword generation ────────────────────────────────────────────────

_DISCOVERY_SYSTEM = """\
You are a senior SEO strategist generating the FOUNDATIONAL keyword strategy
for a healthcare practice. Your output becomes Step 1 of the client's SEO
pipeline — the pool of core keyword candidates the team will review and
approve before long-tail expansion runs.

Your job: read the Business Profile carefully, identify exactly what this
practice actually offers (services, substances treated, populations served,
insurance accepted, program durations, accreditations), then generate a
comprehensive pool of realistic search keywords spanning every applicable
combination.

Think coverage, not cleverness: the team needs to see the full landscape of
keyword opportunities for this client, organized by bucket, so they can make
informed promotion decisions.

BUCKETS TO COVER (generate candidates for each the client actually serves):

  service_geo          - {service term} + {geo modifier}
                         e.g. "IOP south carolina", "PHP florence sc"
  substance_geo        - {substance} + {rehab|treatment|detox} + {geo}
                         e.g. "opioid addiction treatment south carolina"
  population_geo       - {population} + {rehab|treatment} + {geo}
                         e.g. "veterans addiction treatment south carolina"
  insurance_geo        - {insurance} + {rehab|treatment} + {geo}
                         e.g. "bcbs rehab south carolina"
  duration_geo         - {length} + {rehab|treatment} + {geo}
                         e.g. "30 day rehab south carolina"
  accreditation_geo    - {accrediting body} + {service} + {geo}
                         e.g. "joint commission accredited rehab south carolina"
  terminology_variant  - same intent, different vocabulary
                         e.g. "rehab sc" vs "addiction treatment sc" vs
                         "substance abuse treatment sc" vs "recovery center sc"
  intent_modifier      - modifiers that clarify intent
                         e.g. "best {service} {geo}", "affordable {service} {geo}",
                         "near me" variants, "find {service} {geo}"

GENERATION RULES:

  1. ONLY generate keywords for services/populations/substances/insurance the
     client ACTUALLY offers per the Business Profile. If the BP says "PHP and
     IOP only — no detox, no residential", do NOT generate detox or
     residential keywords.

  2. Keywords should be 2–6 words, lowercase, no punctuation or quotes.

  3. Cover every relevant terminology variant for each concept. If the client
     treats alcohol addiction, generate ALL of: "alcohol rehab {geo}",
     "alcohol addiction treatment {geo}", "alcoholism treatment {geo}",
     "alcohol detox {geo}" (only if they offer detox).

  4. For geo modifiers, use: full state name ("south carolina"), state
     abbrev ("sc"), primary city ("florence sc"), and "near me" variants.
     NEVER generate other state names or cities outside the client's market.

  5. Exclude branded competitor queries. Don't write "encompass health rehab"
     or any competitor's brand name.

  6. Exclude wrong-vertical "rehab" — cardiac rehab, pediatric rehab,
     physical therapy rehab, etc. are different medical verticals.

  7. Aim for approximately {target_count} candidates. Prioritize breadth of
     coverage over depth of any one bucket — the team will review and
     down-select, and long-tail expansion happens in a separate pass.

  8. Do not duplicate candidates. If a keyword fits multiple buckets, assign
     it to the most specific one and emit it once.

OUTPUT FORMAT — return ONLY a JSON object:

{
  "keywords": [
    {"keyword": "iop south carolina", "bucket": "service_geo"},
    {"keyword": "opioid addiction treatment sc", "bucket": "substance_geo"},
    ...
  ]
}
"""


async def _generate_candidates(
    positioning: str, target_count: int,
) -> list[dict]:
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    system = _DISCOVERY_SYSTEM.replace("{target_count}", str(target_count))
    resp = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=8000,
        system=system,
        messages=[{"role": "user", "content": positioning}],
    )
    raw = (resp.content[0].text or "").strip()
    # Extract JSON
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    rows = data.get("keywords", []) or []
    out: list[dict] = []
    seen: set[str] = set()
    for r in rows:
        kw = (r.get("keyword") or "").strip().lower()
        bucket = (r.get("bucket") or "other").strip()
        if not kw or kw in seen or len(kw.split()) < 2:
            continue
        seen.add(kw)
        out.append({"keyword": kw, "bucket": bucket, "seed": bucket})
    return out


# ── DataForSEO volume enrichment ─────────────────────────────────────────────

def _dfs_headers() -> dict:
    tok = base64.b64encode(
        f"{settings.dataforseo_login}:{settings.dataforseo_password}".encode()
    ).decode()
    return {"Authorization": f"Basic {tok}", "Content-Type": "application/json"}


async def _enrich_volumes(candidates: list[dict]) -> None:
    """In-place: populate volume / competition / cpc via search_volume/live."""
    if not candidates:
        return
    headers = _dfs_headers()
    unique_kws: list[str] = []
    seen: set[str] = set()
    for c in candidates:
        k = c["keyword"]
        if k in seen or len(k.split()) > 10:
            continue
        seen.add(k)
        unique_kws.append(k)

    vol_map: dict[str, dict] = {}
    for i in range(0, len(unique_kws), 1000):
        batch = unique_kws[i:i + 1000]
        payload = [{
            "keywords":      batch,
            "location_code": LOCATION_CODE_US,
            "language_code": LANGUAGE_CODE,
        }]
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{DATAFORSEO_BASE}/keywords_data/google_ads/search_volume/live",
                headers=headers, json=payload,
            )
        if resp.status_code != 200:
            print(f"  ⚠ search_volume HTTP {resp.status_code}: {resp.text[:200]}")
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


# ── Notion writer ────────────────────────────────────────────────────────────

def _rt(text: str, limit: int = 1900) -> dict:
    return {"rich_text": [{"text": {"content": (text or "")[:limit]}}]}


def _title(text: str) -> dict:
    return {"title": [{"text": {"content": (text or "")[:200]}}]}


def _select(name: str) -> dict:
    return {"select": {"name": name}} if name else {"select": None}


async def _write_to_keywords_db(
    candidates: list[dict],
    keywords_db_id: str,
    dry_run: bool,
) -> tuple[int, int]:
    """Write candidates at Priority=Medium (fit=3 strong) or Low (fit=2 neutral).
    Drop fit=1 (weak). Returns (written, dropped_weak)."""
    notion = NotionClient(settings.notion_api_key)
    existing = await notion.query_database(database_id=keywords_db_id)
    existing_titles: set[str] = set()
    for e in existing:
        items = e["properties"].get("Keyword", {}).get("title", [])
        existing_titles.add(
            "".join(p.get("text", {}).get("content", "") for p in items).strip().lower()
        )

    written = 0
    dropped_weak = 0
    for c in candidates:
        fit = c.get("fit", 2)
        if fit <= 1:
            dropped_weak += 1
            continue
        if c["keyword"] in existing_titles:
            print(f"  ↳ skip (exists): {c['keyword']}")
            continue
        reason = c.get("reason", "")
        fit_label = {3: "STRONG", 2: "NEUTRAL"}[fit]
        priority = "Medium" if fit == 3 else "Low"
        vol = c.get("volume", 0)
        vol_str = str(vol) if vol > 0 else "unknown (local long-tail)"
        bucket = c.get("bucket", "other")
        notes = (
            f"discover-keywords (Step 1). Bucket: {bucket}. "
            f"Volume: {vol_str}. Competition: {c.get('competition', '')}. "
            f"CPC: ${c.get('cpc', 0)}. Strategic fit: {fit_label}. Reason: {reason} "
            f"Promote to Priority=High + Status=Target to seed Step 2 (expand-longtail)."
        )
        if dry_run:
            print(f"  [DRY] fit={fit} pri={priority:<6} vol={vol:>5} [{bucket:<20}] {c['keyword']:55s} — {reason[:50]}")
            written += 1
            continue
        await notion.create_database_entry(
            database_id=keywords_db_id,
            properties={
                "Keyword":                _title(c["keyword"]),
                "Cluster":                _rt(bucket),
                "Monthly Search Volume":  _rt(vol_str),
                "Our Position":           _rt("-"),
                "Competitor Positions":   _rt(f"(discover-keywords candidate: {bucket})"),
                "Priority":               _select(priority),
                "Gap Type":               _select("Create"),
                "Status":                 _select("Proposed"),
                "Notes":                  _rt(notes),
            },
        )
        print(f"  ✓ fit={fit} pri={priority:<6} vol={vol:>5} [{bucket:<20}] {c['keyword']:55s} — {reason[:50]}")
        written += 1
    return written, dropped_weak


# ── Public entry point ───────────────────────────────────────────────────────

async def discover_keywords(
    cfg: dict,
    target_count: int = DEFAULT_TARGET_CANDIDATES,
    dry_run: bool = False,
) -> dict:
    client_key = cfg.get("client_id") or cfg.get("client_key") or ""
    keywords_db_id    = cfg.get("keywords_db_id", "")
    competitors_db_id = cfg.get("competitors_db_id", "")
    verticals = cfg.get("vertical") or []
    if isinstance(verticals, str):
        verticals = [verticals]

    if not keywords_db_id:
        return {"status": "skipped", "reason": "no keywords_db_id"}

    # 1. Initialize CTX used by expand_longtail's shared filter/fit utilities
    CTX["client_key"]      = client_key
    CTX["client_name"]     = cfg.get("name", client_key)
    CTX["seo_mode"]        = (cfg.get("seo_mode") or "local").lower()
    CTX["verticals"]       = verticals
    CTX["state_allowlist"] = _derive_state_allowlist(cfg)
    CTX["seeds"]           = []   # discover-keywords doesn't use pre-existing seeds
    CTX["positioning"]     = ""   # filled below
    notion = NotionClient(settings.notion_api_key)
    CTX["brand_roots"]     = await _load_competitor_brand_roots(notion, competitors_db_id)

    print(f"\n── discover-keywords for {cfg.get('name', client_key)} "
          f"{'[DRY RUN]' if dry_run else ''} ──")
    print(f"  seo_mode={CTX['seo_mode']} | verticals={CTX['verticals']} | "
          f"state_allowlist={sorted(CTX['state_allowlist']) or 'national'}")
    print(f"  target candidates={target_count} | "
          f"competitor brands blocked={len(CTX['brand_roots'])}")

    # 2. Read Business Profile
    print("\n[1/6] Loading Business Profile ...")
    bp_text = await load_business_profile(notion, cfg)
    print(f"  → BP text: {len(bp_text):,} chars "
          f"{'(thin — populate first for better output)' if len(bp_text) < 1500 else ''}")

    # 3. Assemble positioning + taxonomy
    taxonomy = taxonomy_for(verticals)
    positioning = _build_positioning(cfg, bp_text, taxonomy)
    CTX["positioning"] = positioning

    # 4. Claude generates candidates
    print(f"\n[2/6] Claude generating ~{target_count} candidate keywords from BP + taxonomy ...")
    candidates = await _generate_candidates(positioning, target_count=target_count)
    print(f"  → Claude returned {len(candidates)} unique candidates")

    if not candidates:
        return {"status": "failed", "reason": "Claude returned no candidates"}

    # Count by bucket for visibility
    by_bucket: dict[str, int] = {}
    for c in candidates:
        by_bucket[c["bucket"]] = by_bucket.get(c["bucket"], 0) + 1
    print("  Candidates by bucket:")
    for bucket, count in sorted(by_bucket.items(), key=lambda x: -x[1]):
        print(f"    {count:>3}  {bucket}")

    # 5. DataForSEO volume enrichment
    print("\n[3/6] Enriching volumes via DataForSEO search_volume/live ...")
    await _enrich_volumes(candidates)
    with_vol = sum(1 for c in candidates if c.get("volume", 0) > 0)
    print(f"  → {with_vol} / {len(candidates)} have volume > 0 "
          f"(local long-tail often 0-volume, kept anyway)")

    # 6. Deterministic filters (same as Pass A)
    print("\n[4/6] Deterministic filters (vertical / geo / branded / junk) ...")
    filtered, drops = filter_candidates(candidates, min_volume=0, min_words=2)
    drop_summary = ", ".join(f"{k}={v}" for k, v in drops.items() if v) or "none"
    print(f"  → kept {len(filtered)} | dropped: {drop_summary}")

    if not filtered:
        return {"status": "failed", "reason": "all candidates filtered out"}

    # 7. Claude strategic fit scoring
    print(f"\n[5/6] Claude strategic fit scoring against {cfg.get('name', client_key)}'s positioning ...")
    await evaluate_strategic_fit(filtered)
    strong  = sum(1 for c in filtered if c.get("fit") == 3)
    neutral = sum(1 for c in filtered if c.get("fit") == 2)
    weak    = sum(1 for c in filtered if c.get("fit") == 1)
    print(f"  → fit: strong={strong}, neutral={neutral}, weak={weak} "
          f"(weak will be dropped)")

    filtered.sort(key=lambda c: (-c.get("fit", 0), -(c.get("volume") or 0)))

    # 8. Write to Keywords DB
    print(f"\n[6/6] Writing to Keywords DB (fit=3 → Medium, fit=2 → Low, fit=1 dropped) ...")
    written, dropped_weak = await _write_to_keywords_db(
        filtered, keywords_db_id, dry_run,
    )

    print(f"\n── Summary ──")
    print(f"  Generated:           {len(candidates)}")
    print(f"  After det. filters:  {len(filtered)}")
    print(f"  Dropped (weak fit):  {dropped_weak}")
    print(f"  Written:             {written}")
    print(f"  Review Keywords DB → Status=Proposed. Promote keepers to")
    print(f"  Priority=High + Status=Target. Then run expand-longtail for")
    print(f"  long-tail variants of approved seeds.")

    return {
        "status":            "ok" if not dry_run else "dry_run",
        "generated":         len(candidates),
        "after_filters":     len(filtered),
        "dropped_weak":      dropped_weak,
        "written":           written,
        "by_bucket":         by_bucket,
    }
