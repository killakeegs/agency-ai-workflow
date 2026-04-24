"""
client_readiness.py — Unified readiness check across all four client sources.

Reads Clients DB + clients.json + Client Info DB + Brand Guidelines DB +
Business Profile page. For each source, evaluates required + nice-to-have
fields against the spec in config/client_readiness.py. For Business Profile,
runs both the mechanical gate (min section chars) AND a Layer 2 Claude
content-completeness pass that verifies each required section contains the
specific facts downstream agents need.

Returns a structured report + writes:
  - Consolidated "🚨 Client Readiness" callout at top of the Business Profile
    page (extends the existing gap callout to cover all four sources)
  - One Flag DB entry per gap (dedup'd via existing write_flags_to_db helper)

Overall status levels:
  🚨 BLOCKED  — one or more required fields empty. Downstream agents refuse
                to run unless FORCE=1.
  ⚠️ PARTIAL  — required fields present but nice-to-haves missing, OR Claude
                Layer 2 flagged sections as thin. Downstream runs with warn.
  ✓ READY    — everything fills, Layer 2 clean.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

import anthropic

from config.business_profile_requirements import (
    MIN_SECTION_CHARS, required_sections_for,
)
from config.client_readiness import (
    BRAND_GUIDELINES_NICE_TO_HAVE,
    BRAND_GUIDELINES_REQUIRED,
    BUSINESS_PROFILE_FACTS_NEEDED,
    CLIENT_INFO_NICE_TO_HAVE,
    CLIENT_INFO_REQUIRED,
    CLIENTS_DB_NICE_TO_HAVE,
    CLIENTS_DB_REQUIRED,
    CLIENTS_JSON_REQUIRED,
    effective_required,
)
from src.config import settings
from src.integrations.business_profile import _fetch_all_blocks, _rt_to_text
from src.integrations.notion import NotionClient
from src.services.business_profile_populator import _section_index


READINESS_CALLOUT_MARKER = "🚨 Client Readiness"


# ── Notion field populated check ──────────────────────────────────────────────

def _notion_is_populated(props: dict, name: str, empty_check: str) -> tuple[bool, str]:
    """Return (is_populated, current_value_preview)."""
    v = props.get(name)
    if not v:
        return False, ""
    t = v.get("type", "")
    if empty_check == "title":
        txt = "".join(p.get("text", {}).get("content", "") for p in v.get("title", []))
        return bool(txt.strip()), txt[:80]
    if empty_check == "rich_text":
        txt = "".join(p.get("text", {}).get("content", "") for p in v.get("rich_text", []))
        return bool(txt.strip()), txt[:80]
    if empty_check == "select":
        s = v.get("select")
        return bool(s and s.get("name")), s.get("name", "") if s else ""
    if empty_check == "multi_select":
        ms = v.get("multi_select", []) or []
        names = [m.get("name", "") for m in ms]
        return bool(names), ", ".join(names)
    if empty_check == "email":
        e = v.get("email")
        return bool(e), str(e or "")
    if empty_check == "phone_number":
        p = v.get("phone_number")
        return bool(p), str(p or "")
    if empty_check == "url":
        u = v.get("url")
        return bool(u), str(u or "")
    if empty_check == "number":
        n = v.get("number")
        return n is not None, str(n) if n is not None else ""
    if empty_check == "date":
        d = v.get("date")
        return bool(d and d.get("start")), d.get("start", "") if d else ""
    if empty_check == "files":
        files = v.get("files", [])
        return len(files) > 0, f"{len(files)} file(s)"
    return bool(v), "?"


# ── clients.json field populated check ───────────────────────────────────────

def _json_is_populated(value, empty_check: str) -> bool:
    if empty_check == "str":
        return bool(value and str(value).strip())
    if empty_check == "list":
        return bool(value and isinstance(value, list) and len(value) > 0)
    if empty_check == "dict":
        return bool(value and isinstance(value, dict) and len(value) > 0)
    return bool(value)


# ── Brand Guidelines schema self-heal ────────────────────────────────────────

async def _ensure_bg_schema(notion: NotionClient, bg_db_id: str) -> list[str]:
    """Add any missing required Brand Guidelines fields to the DB schema.
    Returns list of added field names. Safe to call repeatedly."""
    db = await notion._client.request(path=f"databases/{bg_db_id}", method="GET")
    existing = set(db.get("properties", {}).keys())
    added: list[str] = []
    for spec in BRAND_GUIDELINES_REQUIRED:
        name = spec["name"]
        if name in existing:
            continue
        etype = spec["empty_check"]
        # Only self-heal the common types; others need manual setup
        if etype == "rich_text":
            await notion._client.request(
                path=f"databases/{bg_db_id}", method="PATCH",
                body={"properties": {name: {"rich_text": {}}}},
            )
            added.append(name)
    return added


# ── Per-source check functions ───────────────────────────────────────────────

async def check_clients_db(
    notion: NotionClient, client_key: str, services, seo_mode: str,
) -> list[dict]:
    """Find this client's row in the Clients DB and evaluate required fields."""
    clients_db_id = os.environ.get("NOTION_CLIENTS_DB_ID", "").strip()
    if not clients_db_id:
        return [{
            "source": "Clients DB", "field": "(env var)",
            "severity": "blocked",
            "description": "NOTION_CLIENTS_DB_ID not set in .env — cannot check "
                           "top-level Clients DB row.",
        }]
    # Query by Client Name
    rows = await notion._client.request(
        path=f"databases/{clients_db_id}/query",
        method="POST",
        body={"page_size": 50, "filter": {
            "property": "Client Name", "title": {"contains": client_key.replace("_", " ")},
        }},
    )
    # Fall back to checking all rows if no title match (client_key may differ from name)
    results = rows.get("results", [])
    if not results:
        all_rows = await notion._client.request(
            path=f"databases/{clients_db_id}/query",
            method="POST", body={"page_size": 200},
        )
        results = all_rows.get("results", [])

    # Find best match by comparing title to client_key loosely
    row = None
    key_norm = client_key.replace("_", "").replace("-", "").lower()
    for r in results:
        title_items = r.get("properties", {}).get("Client Name", {}).get("title", []) or []
        title = "".join(p.get("text", {}).get("content", "") for p in title_items)
        title_norm = title.replace(" ", "").replace("_", "").replace("-", "").lower()
        if key_norm in title_norm or title_norm in key_norm:
            row = r
            break

    if not row:
        return [{
            "source": "Clients DB", "field": "(row)",
            "severity": "blocked",
            "description": f"No row found in Clients DB for client_key '{client_key}'. "
                           "Add the client to the Clients DB.",
        }]

    props = row.get("properties", {})
    gaps: list[dict] = []
    for spec in effective_required(CLIENTS_DB_REQUIRED, services, seo_mode):
        ok, _ = _notion_is_populated(props, spec["name"], spec["empty_check"])
        if not ok:
            gaps.append({
                "source": "Clients DB", "field": spec["name"],
                "severity": "blocked",
                "description": f"Clients DB field '{spec['name']}' is empty (required).",
            })
    for spec in effective_required(CLIENTS_DB_NICE_TO_HAVE, services, seo_mode):
        ok, _ = _notion_is_populated(props, spec["name"], spec["empty_check"])
        if not ok:
            gaps.append({
                "source": "Clients DB", "field": spec["name"],
                "severity": "partial",
                "description": f"Clients DB field '{spec['name']}' is empty (nice-to-have).",
            })
    return gaps


def check_clients_json(cfg: dict) -> list[dict]:
    services = cfg.get("services", {})
    seo_mode = cfg.get("seo_mode", "")
    gaps: list[dict] = []
    for spec in effective_required(CLIENTS_JSON_REQUIRED, services, seo_mode):
        v = cfg.get(spec["name"])
        if not _json_is_populated(v, spec["empty_check"]):
            gaps.append({
                "source": "clients.json", "field": spec["name"],
                "severity": "blocked",
                "description": f"clients.json field '{spec['name']}' missing or empty (required).",
            })
    return gaps


async def check_client_info(notion: NotionClient, cfg: dict) -> list[dict]:
    db_id = cfg.get("client_info_db_id", "")
    if not db_id:
        return [{"source": "Client Info DB", "field": "(db)", "severity": "blocked",
                 "description": "No client_info_db_id in clients.json — DB missing."}]
    rows = await notion.query_database(database_id=db_id)
    if not rows:
        return [{"source": "Client Info DB", "field": "(row)", "severity": "blocked",
                 "description": "Client Info DB has no rows — add one."}]
    props = rows[0].get("properties", {})
    services = cfg.get("services", {})
    seo_mode = cfg.get("seo_mode", "")
    gaps: list[dict] = []
    for spec in effective_required(CLIENT_INFO_REQUIRED, services, seo_mode):
        ok, _ = _notion_is_populated(props, spec["name"], spec["empty_check"])
        if not ok:
            gaps.append({
                "source": "Client Info DB", "field": spec["name"],
                "severity": "blocked",
                "description": f"Client Info field '{spec['name']}' is empty (required).",
            })
    for spec in effective_required(CLIENT_INFO_NICE_TO_HAVE, services, seo_mode):
        ok, _ = _notion_is_populated(props, spec["name"], spec["empty_check"])
        if not ok:
            gaps.append({
                "source": "Client Info DB", "field": spec["name"],
                "severity": "partial",
                "description": f"Client Info field '{spec['name']}' is empty (nice-to-have).",
            })
    return gaps


async def check_brand_guidelines(notion: NotionClient, cfg: dict) -> list[dict]:
    db_id = cfg.get("brand_guidelines_db_id", "")
    if not db_id:
        return [{"source": "Brand Guidelines", "field": "(db)", "severity": "blocked",
                 "description": "No brand_guidelines_db_id in clients.json."}]
    # Self-heal schema before reading
    added = await _ensure_bg_schema(notion, db_id)
    rows = await notion.query_database(database_id=db_id)
    if not rows:
        gaps = [{"source": "Brand Guidelines", "field": "(row)", "severity": "blocked",
                 "description": "Brand Guidelines DB has no rows — add one."}]
        if added:
            gaps.append({"source": "Brand Guidelines", "field": "(schema)",
                         "severity": "info",
                         "description": f"Self-healed schema: added fields {added}"})
        return gaps
    props = rows[0].get("properties", {})
    gaps: list[dict] = []
    if added:
        gaps.append({"source": "Brand Guidelines", "field": "(schema)",
                     "severity": "info",
                     "description": f"Self-healed schema: added fields {added}"})
    for spec in BRAND_GUIDELINES_REQUIRED:
        ok, _ = _notion_is_populated(props, spec["name"], spec["empty_check"])
        if not ok:
            gaps.append({
                "source": "Brand Guidelines", "field": spec["name"],
                "severity": "blocked",
                "description": f"Brand Guidelines field '{spec['name']}' is empty (required).",
            })
    for spec in BRAND_GUIDELINES_NICE_TO_HAVE:
        ok, _ = _notion_is_populated(props, spec["name"], spec["empty_check"])
        if not ok:
            gaps.append({
                "source": "Brand Guidelines", "field": spec["name"],
                "severity": "partial",
                "description": f"Brand Guidelines field '{spec['name']}' is empty (nice-to-have).",
            })
    return gaps


# ── Business Profile: mechanical + Layer 2 Claude content check ─────────────

async def check_business_profile(notion: NotionClient, cfg: dict) -> list[dict]:
    page_id = cfg.get("business_profile_page_id", "")
    if not page_id:
        return [{"source": "Business Profile", "field": "(page)", "severity": "blocked",
                 "description": "No business_profile_page_id in clients.json."}]
    verticals = cfg.get("vertical") or []
    if isinstance(verticals, str):
        verticals = [verticals]

    _, section_content = await _section_index(notion, page_id)
    required_names = required_sections_for(verticals)

    # Layer 1 — mechanical gate
    gaps: list[dict] = []
    for name in required_names:
        lines = section_content.get(name, [])
        joined = "\n".join(lines).strip()
        if not joined:
            gaps.append({
                "source": "Business Profile", "field": name,
                "severity": "blocked",
                "description": f"Business Profile section '{name}' is empty (required).",
            })
        elif len(joined) < MIN_SECTION_CHARS:
            gaps.append({
                "source": "Business Profile", "field": name,
                "severity": "partial",
                "description": f"Business Profile section '{name}' is thin "
                               f"({len(joined)} chars < {MIN_SECTION_CHARS}).",
            })

    # Layer 2 — Claude content-completeness pass for sections that ARE populated
    layer2_gaps = await _layer2_claude_check(section_content, verticals)
    gaps.extend(layer2_gaps)
    return gaps


_LAYER2_SYSTEM = """\
You are evaluating whether each Business Profile section contains the specific
facts downstream SEO / content agents need. For each section, you're given:
  - the section's current content
  - the specific facts a downstream agent needs to find there

Your job: for each section, return a verdict — "covered" or "missing" — and
if missing, a short description of what's missing.

Rules:
- A fact counts as "covered" if the content STATES it (or states its negation
  — e.g. "does NOT offer residential" counts as covering the residential fact).
- "Pending" / "TBD" / "to be confirmed" placeholder text does NOT count as
  covered — that's still missing.
- Be strict. If content is vague, mark missing.

Return ONLY a JSON object:
{
  "sections": {
    "Section Name": {
      "verdict": "covered" | "missing",
      "missing_facts": ["fact1", "fact2"]    // only if verdict=missing
    }
  }
}
"""


async def _layer2_claude_check(
    section_content: dict[str, list[str]], verticals: list[str],
) -> list[dict]:
    """Send populated sections to Claude with the per-vertical 'facts needed'
    spec; return gaps for any section where expected facts are missing."""
    # Flatten vertical-specific facts_needed specs
    facts_needed: dict[str, list[str]] = {}
    for v in verticals:
        for section, facts in BUSINESS_PROFILE_FACTS_NEEDED.get(v, {}).items():
            facts_needed.setdefault(section, []).extend(facts)

    # Build prompt payload — only include sections that (a) we have facts_needed
    # for and (b) have content (if empty, Layer 1 already flagged them)
    sections_to_check: dict[str, list[str]] = {}
    for name, facts in facts_needed.items():
        content = "\n".join(section_content.get(name, [])).strip()
        if content:
            sections_to_check[name] = facts

    if not sections_to_check:
        return []

    prompt_parts: list[str] = []
    for name, facts in sections_to_check.items():
        content = "\n".join(section_content.get(name, [])).strip()[:3000]
        fact_list = "\n".join(f"  - {f}" for f in facts)
        prompt_parts.append(
            f"=== SECTION: {name} ===\nFACTS NEEDED:\n{fact_list}\n\n"
            f"CURRENT CONTENT:\n{content}\n"
        )
    prompt = "\n\n".join(prompt_parts)

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    resp = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=3000,
        system=_LAYER2_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = (resp.content[0].text or "").strip()
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []

    gaps: list[dict] = []
    for section_name, result in (data.get("sections", {}) or {}).items():
        if result.get("verdict") == "missing":
            missing = result.get("missing_facts") or []
            gaps.append({
                "source": "Business Profile", "field": section_name,
                "severity": "partial",
                "description": (
                    f"Business Profile section '{section_name}' exists but "
                    f"is missing specific facts: {'; '.join(missing)[:500]}"
                ),
            })
    return gaps


# ── Orchestration ────────────────────────────────────────────────────────────

async def run_readiness_check(
    notion: NotionClient, cfg: dict, client_key: str,
) -> dict:
    """Run all four readiness checks, aggregate, return structured result."""
    services = cfg.get("services", {})
    seo_mode = cfg.get("seo_mode", "")

    all_gaps: list[dict] = []
    all_gaps.extend(check_clients_json(cfg))
    all_gaps.extend(await check_clients_db(notion, client_key, services, seo_mode))
    all_gaps.extend(await check_client_info(notion, cfg))
    all_gaps.extend(await check_brand_guidelines(notion, cfg))
    all_gaps.extend(await check_business_profile(notion, cfg))

    blocked = [g for g in all_gaps if g["severity"] == "blocked"]
    partial = [g for g in all_gaps if g["severity"] == "partial"]
    info    = [g for g in all_gaps if g["severity"] == "info"]

    if blocked:
        status = "blocked"
    elif partial:
        status = "partial"
    else:
        status = "ready"

    return {
        "status":  status,
        "blocked": blocked,
        "partial": partial,
        "info":    info,
        "all":     all_gaps,
    }


# ── Callout writer ──────────────────────────────────────────────────────────

async def _find_readiness_callout_id(
    notion: NotionClient, page_id: str,
) -> str | None:
    blocks = await _fetch_all_blocks(notion, page_id)
    for b in blocks:
        if b.get("type") != "callout":
            continue
        txt = _rt_to_text(b.get("callout", {}).get("rich_text", []))
        if READINESS_CALLOUT_MARKER in txt:
            return b["id"]
    return None


async def write_readiness_callout(
    notion: NotionClient, page_id: str, report: dict,
) -> str:
    """Write / update / clear the consolidated readiness callout at the top
    of the Business Profile page."""
    existing_id = await _find_readiness_callout_id(notion, page_id)
    blocked = report["blocked"]
    partial = report["partial"]

    if not blocked and not partial:
        if existing_id:
            await notion._client.request(path=f"blocks/{existing_id}", method="DELETE")
            return "cleared"
        return "unchanged"

    status_icon = "🚨" if blocked else "⚠️"
    lines: list[str] = [
        f"{READINESS_CALLOUT_MARKER} — "
        f"{len(blocked)} blocked, {len(partial)} partial"
    ]
    if blocked:
        lines.append("")
        lines.append(f"🚨 BLOCKED ({len(blocked)}) — downstream agents refuse to run:")
        by_source: dict[str, list[dict]] = {}
        for g in blocked:
            by_source.setdefault(g["source"], []).append(g)
        for src, items in by_source.items():
            lines.append(f"  {src}:")
            for g in items:
                lines.append(f"    • {g['field']}")
    if partial:
        lines.append("")
        lines.append(f"⚠️ PARTIAL ({len(partial)}) — fill when possible:")
        by_source = {}
        for g in partial:
            by_source.setdefault(g["source"], []).append(g)
        for src, items in by_source.items():
            lines.append(f"  {src}:")
            for g in items[:10]:  # cap display
                lines.append(f"    • {g['field']}")
            if len(items) > 10:
                lines.append(f"    … and {len(items) - 10} more")
    lines.append("")
    lines.append("Run `make check-client-readiness CLIENT=<key>` to refresh.")
    text = "\n".join(lines)

    callout = {
        "object": "block", "type": "callout",
        "callout": {
            "rich_text": [{"type": "text", "text": {"content": text[:1990]}}],
            "icon":  {"type": "emoji", "emoji": status_icon},
            "color": "red_background" if blocked else "yellow_background",
        },
    }

    if existing_id:
        await notion._client.request(path=f"blocks/{existing_id}", method="DELETE")

    # Insert after the first existing callout (the intro callout on the BP page)
    blocks = await _fetch_all_blocks(notion, page_id)
    anchor_id = next((b["id"] for b in blocks if b.get("type") == "callout"), None)
    body = {"children": [callout]}
    if anchor_id:
        body["after"] = anchor_id
    await notion._client.request(
        path=f"blocks/{page_id}/children", method="PATCH", body=body,
    )
    return "updated" if existing_id else "created"


# ── Flags DB writer ─────────────────────────────────────────────────────────

async def write_readiness_flags(
    notion: NotionClient, flags_db_id: str,
    client_name: str, client_key: str, gaps: list[dict],
) -> list[dict]:
    if not flags_db_id or not gaps:
        return []
    from src.services.email_enrichment import write_flags_to_db
    today = datetime.now().strftime("%Y-%m-%d")
    # Only flag blocked + partial, not info
    actionable = [g for g in gaps if g["severity"] in ("blocked", "partial")]
    flag_dicts = [{
        "type":        "readiness_gap",
        "description": f"[{g['source']} / {g['field']}] {g['description']}",
        "source_date": today,
    } for g in actionable]
    return await write_flags_to_db(
        notion, flags_db_id, client_name, client_key,
        flag_dicts, source="Readiness",
    )
