#!/usr/bin/env python3
"""
expand_longtail.py — Pass A keyword expansion for any SEO client.

Reads the client's Priority=High + Status=Target keywords (team-approved
set) from Keywords DB, uses each as a seed for DataForSEO keyword
suggestions, Claude evaluates each candidate for strategic fit against
the client's positioning, writes net-new candidates as Priority=Medium /
Status=Proposed for team review.

For LOCAL SEO clients (default): no volume filter. Strategic fit decides
selection. Low-volume long-tail terms that match the client's niche get
through; high-volume off-strategy terms (wrong service, wrong geography,
etc.) get fit=1 (weak) and the team can Dismiss.

Usage:
    make expand-longtail CLIENT=lotus_recovery
    make expand-longtail CLIENT=lotus_recovery DRY=1 PER_SEED=5
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


DATAFORSEO_BASE    = "https://api.dataforseo.com/v3"
LOCATION_CODE_US   = 2840     # USA — priority keywords already carry their geo modifier
LANGUAGE_CODE      = "en"

DEFAULT_PER_SEED   = 10       # ideas per seed pulled from DataForSEO
DEFAULT_MIN_VOLUME = 0        # LOCAL SEO: volume is informational, not a gate
DEFAULT_MIN_KEYWORD_WORDS = 3 # genuine long-tail starts at 3 words
MAX_WRITES_PER_SEED = 5       # after Claude scoring, cap output per seed

# Module-level client context — initialized by _init_ctx() at main() start.
CTX: dict = {
    "client_key":     "",
    "client_name":    "",
    "positioning":    "",
    "seo_mode":       "local",
    "verticals":      [],      # list of vertical keys from clients.json
    "state_allowlist": set(),  # {state abbrev(s) in which client operates}
    "brand_roots":    [],      # competitor brand roots to filter out branded queries
    "seeds":          [],      # [{keyword, cluster}] from client's Keywords DB (Priority=High, Status=Target)
}

# Junk / off-topic filters — terms that almost always indicate the seed
# bled into a vertical we don't serve or into a job/career query.
EXCLUDE_SUBSTRINGS = [
    "salary", "jobs", "career", "license", "certification", "near me hiring",
    "degree", "school", "course", "training program",
    "free", "cheap",
    "veteran only",
]

# ── Per-vertical modifier blocklist ──────────────────────────────────────────
# "rehab" means five different things medically. For each vertical we serve,
# list the modifier tokens that specifically flip "rehab" to a different
# medical vertical. A candidate keyword containing any of these phrases gets
# dropped pre-Claude.
VERTICAL_MISMATCH_TOKENS: dict[str, list[str]] = {
    "addiction_treatment": [
        "cardiac", "cardio", "heart",
        "pediatric", "pediatrics",
        "physical rehab", "physical therapy", "pt rehab",
        "wildlife", "animal", "bird", "horse", "vet ", "veterinary",
        "pulmonary",
        "orthopedic", "ortho rehab", "knee rehab", "hip rehab",
        "shoulder rehab", "ankle rehab", "joint rehab", "back rehab",
        "stroke", "neurological", "neuro rehab", "brain injury",
        "vocational", "speech therapy", "occupational therapy",
        "geriatric", "elderly rehab",
        "dental rehab",
        "inpatient rehabilitation hospital",
        "acute rehabilitation",
        "skilled nursing",
        "spinal rehab", "spine rehab",
    ],
    # other verticals: add blocklists as they're onboarded
}

# ── US geography for state-allowlist filter ──────────────────────────────────
US_STATES: dict[str, str] = {
    "alabama":"AL","alaska":"AK","arizona":"AZ","arkansas":"AR","california":"CA",
    "colorado":"CO","connecticut":"CT","delaware":"DE","florida":"FL","georgia":"GA",
    "hawaii":"HI","idaho":"ID","illinois":"IL","indiana":"IN","iowa":"IA",
    "kansas":"KS","kentucky":"KY","louisiana":"LA","maine":"ME","maryland":"MD",
    "massachusetts":"MA","michigan":"MI","minnesota":"MN","mississippi":"MS","missouri":"MO",
    "montana":"MT","nebraska":"NE","nevada":"NV","new hampshire":"NH","new jersey":"NJ",
    "new mexico":"NM","new york":"NY","north carolina":"NC","north dakota":"ND","ohio":"OH",
    "oklahoma":"OK","oregon":"OR","pennsylvania":"PA","rhode island":"RI","south carolina":"SC",
    "south dakota":"SD","tennessee":"TN","texas":"TX","utah":"UT","vermont":"VT",
    "virginia":"VA","washington":"WA","west virginia":"WV","wisconsin":"WI","wyoming":"WY",
}

# Major metros → state abbrev. If metro appears in a keyword AND its state is
# not in the client's allowlist AND the keyword doesn't also name an allowed
# state, the keyword is out-of-market.
MAJOR_METROS_TO_STATE: dict[str, str] = {
    "new york city":"NY","nyc":"NY","manhattan":"NY","brooklyn":"NY","bronx":"NY","queens":"NY",
    "los angeles":"CA","san francisco":"CA","san diego":"CA","orange county":"CA","sacramento":"CA","san jose":"CA",
    "chicago":"IL","philadelphia":"PA","pittsburgh":"PA","boston":"MA",
    "miami":"FL","orlando":"FL","tampa":"FL","jacksonville":"FL","fort lauderdale":"FL",
    "atlanta":"GA","dallas":"TX","houston":"TX","austin":"TX","san antonio":"TX","fort worth":"TX",
    "phoenix":"AZ","tucson":"AZ","denver":"CO",
    "seattle":"WA","portland":"OR","las vegas":"NV","detroit":"MI","minneapolis":"MN",
    "baltimore":"MD","cleveland":"OH","cincinnati":"OH","indianapolis":"IN",
    "st. louis":"MO","st louis":"MO","kansas city":"MO","milwaukee":"WI","nashville":"TN",
    "memphis":"TN","louisville":"KY","oklahoma city":"OK","tulsa":"OK",
    "salt lake city":"UT","albuquerque":"NM","honolulu":"HI",
}


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
    """Map each team-approved seed keyword → cluster label.
    Seeds are loaded from the client's Keywords DB at main() startup and
    stashed in CTX['seeds'] as a list of {keyword, cluster} dicts."""
    return {row["keyword"].lower(): row["cluster"] for row in CTX["seeds"]}


def existing_keyword_set() -> set[str]:
    """Lowercased team-approved keywords — used to dedupe proposals."""
    return {row["keyword"].strip().lower() for row in CTX["seeds"]}


# Brand roots — generic tokens dropped when deriving a competitor's distinctive name.
_BRAND_GENERIC_STOP: set[str] = {
    "the","a","an","and","of","&","at","in","for","on","to","by","with",
    "recovery","rehab","rehabilitation","rehabs","center","centers","centre",
    "health","hospital","hospitals","treatment","medical","clinic","clinics",
    "group","services","inc","llc","co","company","corp","home","homes",
    "sc","nc","va","ga","fl","ny","ca","tx","pa","tn","ky","al","ms","wv",
    "north","south","east","west","american","america","national","usa","us",
    "llc.","inc.",
}


def _brand_root(name: str) -> str:
    """Heuristic: take the first 1–2 distinctive tokens from a competitor name.
    'Encompass Health Rehabilitation Hospital' → 'encompass'
    'Sea Grove Recovery' → 'sea grove'
    'Recovery Centers of America' → 'america' (fallback — pathological case)"""
    tokens = name.lower().replace(",", " ").replace("-", " ").replace("'", "").split()
    root: list[str] = []
    for t in tokens:
        t = t.strip(".,:")
        if not t:
            continue
        if t in _BRAND_GENERIC_STOP:
            if root:
                break
            continue
        root.append(t)
        if len(root) >= 2:
            break
    return " ".join(root).strip()


async def _load_competitor_brand_roots(
    notion: NotionClient, competitors_db_id: str,
) -> list[str]:
    """Load Status ∈ {Active, Proposed} competitor names, reduce to brand roots."""
    if not competitors_db_id:
        return []
    try:
        entries = await notion.query_database(database_id=competitors_db_id)
    except Exception as exc:
        print(f"  ⚠ could not load competitors for branded-query filter: {exc}")
        return []
    roots: set[str] = set()
    for e in entries:
        props = e["properties"]
        status_sel = (props.get("Status") or {}).get("select")
        status = status_sel.get("name", "") if status_sel else ""
        if status not in ("Active", "Proposed", ""):  # include unstated status
            continue
        name_items = props.get("Competitor Name", {}).get("title", []) or []
        name = "".join(p.get("text", {}).get("content", "") for p in name_items).strip()
        if not name:
            continue
        root = _brand_root(name)
        if len(root) >= 4:  # avoid 2-letter generic roots
            roots.add(root)
    return sorted(roots)


# State abbrevs that collide with common English words — only match these via
# their full state name, never the 2-letter code.
_AMBIGUOUS_STATE_ABBREVS: set[str] = {"ME", "IN", "OR", "OK", "HI", "LA", "AL"}


def _has_geo_outside_allowlist(keyword: str, state_allowlist: set[str]) -> bool:
    """Return True if the keyword names a US state or major metro whose state
    is NOT in the client's allowlist. Clients with an empty allowlist (national
    SEO mode) get no geo filtering."""
    if not state_allowlist:
        return False
    kw = " " + keyword.lower().replace(",", " ") + " "

    # Full state name mentions
    allowed_state_mentioned = False
    for state_name, state_abbr in US_STATES.items():
        if f" {state_name} " in kw:
            if state_abbr in state_allowlist:
                allowed_state_mentioned = True
            else:
                return True
        # State abbrev as standalone token — skip abbrevs that are common words
        if state_abbr in _AMBIGUOUS_STATE_ABBREVS:
            continue
        if f" {state_abbr.lower()} " in kw:
            if state_abbr in state_allowlist:
                allowed_state_mentioned = True
            else:
                return True

    # Major metros — only if no in-allowlist state is already present
    if allowed_state_mentioned:
        return False
    for metro, metro_state in MAJOR_METROS_TO_STATE.items():
        if f" {metro} " in kw and metro_state not in state_allowlist:
            return True
    return False


def _is_branded_competitor_query(keyword: str, brand_roots: list[str]) -> bool:
    """True if the keyword contains a competitor's brand root as a substring."""
    kw = " " + keyword.lower() + " "
    for root in brand_roots:
        if f" {root} " in kw or kw.startswith(root + " ") or kw.endswith(" " + root):
            return True
    return False


def _has_vertical_mismatch(keyword: str, verticals: list[str]) -> bool:
    """True if keyword contains any modifier that flips 'rehab' (or similar
    ambiguous stems) into a medical vertical the client doesn't serve."""
    kw = keyword.lower()
    for v in verticals:
        for token in VERTICAL_MISMATCH_TOKENS.get(v, []):
            if token in kw:
                return True
    return False


def filter_candidates(
    candidates: list[dict],
    min_volume: int,
    min_words: int,
) -> tuple[list[dict], dict[str, int]]:
    """
    Strip obvious junk + apply per-client intelligence:
      - dedup against team-approved seed set
      - minimum word count
      - junk-substring blocklist (jobs / salary / certification / etc)
      - per-vertical mismatch tokens (cardiac/pediatric/wildlife rehab, etc)
      - geography allowlist (states/metros outside client's market → drop)
      - branded-competitor substring check (keyword containing competitor brand → drop)
      - optional volume gate (off by default for local SEO)

    Returns (cleaned_list, drop_counts). drop_counts is a diagnostic dict of
    how many candidates were dropped by each reason, logged before Claude.
    """
    existing = existing_keyword_set()
    seed_map = seed_to_cluster_map()
    state_allowlist = CTX["state_allowlist"]
    brand_roots     = CTX["brand_roots"]
    verticals       = CTX["verticals"]

    cleaned: list[dict] = []
    seen: set[str] = set()
    drops = {
        "dup": 0, "word_count": 0, "volume": 0, "junk_substr": 0,
        "vertical_mismatch": 0, "geo_out_of_market": 0, "branded_competitor": 0,
    }
    for c in candidates:
        kw = c["keyword"]
        if kw in existing or kw in seen:
            drops["dup"] += 1; continue
        if len(kw.split()) < min_words:
            drops["word_count"] += 1; continue
        if min_volume > 0 and c.get("volume", 0) < min_volume:
            drops["volume"] += 1; continue
        if any(bad in kw for bad in EXCLUDE_SUBSTRINGS):
            drops["junk_substr"] += 1; continue
        if _has_vertical_mismatch(kw, verticals):
            drops["vertical_mismatch"] += 1; continue
        if _has_geo_outside_allowlist(kw, state_allowlist):
            drops["geo_out_of_market"] += 1; continue
        if _is_branded_competitor_query(kw, brand_roots):
            drops["branded_competitor"] += 1; continue
        c["cluster"] = seed_map.get(c["seed"].lower(), "Other")
        seen.add(kw)
        cleaned.append(c)
    return cleaned, drops


# ── Claude strategic-fit evaluation ───────────────────────────────────────────

STRATEGIC_FIT_PROMPT = """\
You are evaluating keyword candidates for {client_name}'s local SEO strategy.

{positioning}

For EACH candidate keyword below, output one JSON object per line (newline-delimited JSON,
no wrapping array) with these fields:

{{"keyword": "<exact keyword>", "fit": <1|2|3>, "reason": "<one short sentence>"}}

FIT SCORING:
- 3 = Strong fit. Directly aligned with {client_name}'s niche positioning, a service they offer,
      or a stated priority in their battle plan. Worth pursuing.
- 2 = Neutral fit. Relevant to the cluster but generic — may be worth pursuing if volume
      justifies, or may be competitor-saturated. Team decides.
- 1 = Weak fit / skip. Off-strategy: service {client_name} doesn't offer,
      irrelevant geography, wrong audience, or a keyword where established competitors own
      the territory so deeply the client can't realistically compete.

Be terse in the reason. Reference the cluster, a service mismatch, or a competitive reality.
Do NOT explain the scoring system.

REMEMBER: This is LOCAL SEO. Low volume (10, 20, 50/month) does not mean weak fit if the
intent matches {client_name}'s niche. A volume-20 keyword targeting the client's specific
audience is worth more than a volume-500 generic keyword outside their positioning.

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
        client_name=CTX["client_name"],
        positioning=CTX["positioning"],
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


def _apply_per_seed_cap(candidates: list[dict], cap: int) -> list[dict]:
    """After Claude scoring, keep at most `cap` candidates per seed, ranked
    by (fit desc, volume desc). Caps runaway output on high-yield seeds."""
    by_seed: dict[str, list[dict]] = {}
    for c in candidates:
        by_seed.setdefault(c["seed"], []).append(c)
    kept: list[dict] = []
    for seed, rows in by_seed.items():
        rows.sort(key=lambda r: (-r.get("fit", 0), -(r.get("volume") or 0)))
        kept.extend(rows[:cap])
    return kept


async def write_candidates_to_notion(
    candidates: list[dict],
    keywords_db_id: str,
    dry_run: bool,
) -> tuple[int, int]:
    """
    Writes only fit ∈ {2, 3}. fit=1 (weak) is dropped entirely — Claude has
    marked the candidate off-strategy, so the team shouldn't have to sift it.

    Priority mapping (drives the seed pool for the next Pass A run):
      fit=3 (strong)  → Priority=Medium  (team promotes to High to pursue)
      fit=2 (neutral) → Priority=Low

    Returns (written, dropped_weak).
    """
    notion = NotionClient(settings.notion_api_key)

    entries = await notion.query_database(database_id=keywords_db_id)
    existing_titles: set[str] = set()
    for e in entries:
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
        fit_label = {3: "STRONG", 2: "NEUTRAL"}.get(fit, "NEUTRAL")
        priority = "Medium" if fit == 3 else "Low"
        vol = c.get("volume", 0)
        vol_str = str(vol) if vol > 0 else "unknown (local long-tail)"
        notes = (
            f"Pass A long-tail expansion. "
            f"Seed: '{c['seed']}'. Volume: {vol_str}. Competition: {c.get('competition', '')}. "
            f"CPC: ${c.get('cpc', 0)}. Strategic fit: {fit_label}. Reason: {reason} "
            f"Promote to Priority=High + Status=Target to pursue."
        )
        if dry_run:
            print(f"  [DRY] fit={fit} pri={priority:<6} vol={vol:>5} {c['keyword']:55s} — {reason[:60]}")
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
                "Priority":               _select(priority),
                "Gap Type":               _select("Create"),
                "Status":                 _select("Proposed"),
                "Notes":                  _rt(notes),
            },
        )
        print(f"  ✓ fit={fit} pri={priority:<6} vol={vol:>5} {c['keyword']:55s} — {reason[:60]}")
        written += 1
    return written, dropped_weak


# ── Client context init ──────────────────────────────────────────────────────

async def _load_seeds_from_notion(notion: NotionClient, keywords_db_id: str) -> list[dict]:
    """Pull Priority=High + Status=Target keywords from the client's Keywords DB.
    Returns list of {keyword, cluster} for use as expansion seeds."""
    entries = await notion.query_database(database_id=keywords_db_id)
    out: list[dict] = []
    for e in entries:
        props = e["properties"]
        priority_sel = (props.get("Priority") or {}).get("select")
        priority = priority_sel.get("name", "") if priority_sel else ""
        status_sel = (props.get("Status") or {}).get("select")
        status = status_sel.get("name", "") if status_sel else ""
        if priority == "High" and status == "Target":
            kw = "".join(p.get("text", {}).get("content", "")
                         for p in props.get("Keyword", {}).get("title", [])).strip()
            cluster = "".join(p.get("text", {}).get("content", "")
                              for p in props.get("Cluster", {}).get("rich_text", [])).strip() or "Other"
            if kw:
                out.append({"keyword": kw, "cluster": cluster})
    return out


def _derive_state_allowlist(cfg: dict) -> set[str]:
    """For local + hybrid clients, return the set of state abbrevs the client
    operates in (parsed from canonical_address). For national clients, return
    an empty set — no geo filtering.

    Supports multi-state clients via an explicit 'market_states' list in the
    client config (e.g. ["SC","NC"] for a border client). Falls back to
    canonical_address parsing if not provided.
    """
    seo_mode = (cfg.get("seo_mode") or "local").lower()
    if seo_mode == "national":
        return set()

    explicit = cfg.get("market_states") or []
    if isinstance(explicit, str):
        explicit = [explicit]
    if explicit:
        return {s.upper() for s in explicit if s}

    # Parse state from canonical_address (e.g. "940 E Ashby Rd., Quinby, SC 29506")
    addr = (cfg.get("canonical_address") or "").upper()
    for _, abbr in US_STATES.items():
        # Match " SC " or ", SC " before a zip
        if f" {abbr} " in f" {addr} " or f",{abbr} " in addr or f", {abbr} " in addr:
            return {abbr}
    return set()


def _init_ctx_from_cfg(client_key: str, cfg: dict) -> None:
    """Populate CTX with client_name, positioning, verticals, and state allowlist.
    Seeds + brand_roots are loaded async from Notion and set later in main()."""
    verticals = cfg.get("vertical") or []
    if isinstance(verticals, str):
        verticals = [verticals]
    vertical_str = ", ".join(verticals) if verticals else "unspecified"

    address = cfg.get("canonical_address", "")
    seo_mode = (cfg.get("seo_mode") or "local").lower()
    positioning = (
        f"{cfg.get('name', client_key)} — {seo_mode} SEO client.\n"
        f"Vertical: {vertical_str}.\n"
        f"Address / market: {address or 'unspecified'}.\n"
        f"SEO mode: {seo_mode}.\n"
        f"(For richer positioning context, see the client's Business Profile page "
        f"in Notion. This summary is derived from clients.json metadata only.)"
    )

    CTX["client_key"]      = client_key
    CTX["client_name"]     = cfg.get("name", client_key)
    CTX["positioning"]     = positioning
    CTX["seo_mode"]        = seo_mode
    CTX["verticals"]       = verticals
    CTX["state_allowlist"] = _derive_state_allowlist(cfg)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(client_key: str, per_seed: int, min_volume: int, min_words: int, dry_run: bool) -> None:
    from config.clients import CLIENTS
    cfg = CLIENTS.get(client_key)
    if not cfg:
        print(f"✗ Client '{client_key}' not found")
        sys.exit(1)

    keywords_db_id = cfg.get("keywords_db_id", "")
    if not keywords_db_id:
        print(f"✗ {client_key} has no keywords_db_id")
        sys.exit(1)

    _init_ctx_from_cfg(client_key, cfg)
    competitors_db_id = cfg.get("competitors_db_id", "")

    notion = NotionClient(settings.notion_api_key)

    # Load team-approved seeds from Notion
    seed_rows = await _load_seeds_from_notion(notion, keywords_db_id)
    CTX["seeds"] = seed_rows
    seeds = [row["keyword"] for row in seed_rows]

    # Load competitor brand roots for branded-query filtering
    CTX["brand_roots"] = await _load_competitor_brand_roots(notion, competitors_db_id)

    print(f"\n── Pass A — Long-tail expansion for {CTX['client_name']} {'[DRY RUN]' if dry_run else ''} ──")
    print(
        f"  seo_mode={CTX['seo_mode']} | seeds={len(seeds)} | per-seed={per_seed} | "
        f"min-vol={min_volume} | min-words={min_words} | cap/seed={MAX_WRITES_PER_SEED}"
    )
    print(
        f"  verticals={CTX['verticals'] or '-'} | "
        f"state_allowlist={sorted(CTX['state_allowlist']) or 'national (no geo filter)'} | "
        f"competitor brands blocked={len(CTX['brand_roots'])}"
    )
    print()

    if not seeds:
        print("No Priority=High + Status=Target keywords in Keywords DB. Approve seeds first.")
        return

    print("[1/5] Fetching related keywords from DataForSEO...")
    candidates = await fetch_related_keywords(seeds, per_seed)
    print(f"  → {len(candidates)} raw candidates\n")

    print("[2/5] Deterministic filters (dedup, vertical mismatch, geo, branded competitors)...")
    filtered, drops = filter_candidates(candidates, min_volume=min_volume, min_words=min_words)
    print(f"  → kept {len(filtered)} | dropped: " + ", ".join(
        f"{k}={v}" for k, v in drops.items() if v
    ) + "\n")

    if not filtered:
        print("No candidates passed filters.")
        return

    print(f"[3/5] Strategic fit evaluation (Claude) against {CTX['client_name']}'s positioning...")
    await evaluate_strategic_fit(filtered)
    strong  = sum(1 for c in filtered if c.get("fit") == 3)
    neutral = sum(1 for c in filtered if c.get("fit") == 2)
    weak    = sum(1 for c in filtered if c.get("fit") == 1)
    print(f"  → fit: strong={strong}, neutral={neutral}, weak={weak} (weak will be dropped)\n")

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

    print(f"[4/5] Per-seed cap (max {MAX_WRITES_PER_SEED} per seed, ranked by fit × volume)...")
    capped = _apply_per_seed_cap(filtered, cap=MAX_WRITES_PER_SEED)
    print(f"  → {len(capped)} candidates after cap\n")

    capped.sort(key=lambda c: (-c.get("fit", 0), -(c.get("volume") or 0)))

    print(f"[5/5] Writing to {CTX['client_name']}'s Keywords DB (fit=3 → Medium, fit=2 → Low, fit=1 dropped)...")
    written, dropped_weak = await write_candidates_to_notion(capped, keywords_db_id, dry_run)

    print(f"\n── Summary ──")
    print(f"  Raw candidates:       {len(candidates)}")
    print(f"  After det. filters:   {len(filtered)}")
    print(f"  After per-seed cap:   {len(capped)}")
    print(f"  Dropped (weak fit):   {dropped_weak}")
    print(f"  Written:              {written}")
    print(f"  Filter Keywords DB → Status=Proposed to review. Promote strong fits to")
    print(f"  Priority=High + Status=Target to use them as seeds in the next Pass A.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pass A long-tail keyword expansion with strategic fit")
    parser.add_argument("--client",      required=True, help="client_key (e.g. lotus_recovery)")
    parser.add_argument("--per-seed",    type=int, default=DEFAULT_PER_SEED)
    parser.add_argument("--min-volume",  type=int, default=DEFAULT_MIN_VOLUME)
    parser.add_argument("--min-words",   type=int, default=DEFAULT_MIN_KEYWORD_WORDS)
    parser.add_argument("--dry-run",     action="store_true")
    args = parser.parse_args()
    asyncio.run(main(
        client_key=args.client,
        per_seed=args.per_seed,
        min_volume=args.min_volume,
        min_words=args.min_words,
        dry_run=args.dry_run,
    ))
