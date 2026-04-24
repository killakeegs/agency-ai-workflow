"""
gap_filler.py — Interactive Q&A that walks a team member through every
open readiness gap for a client and writes their answers to the correct
Notion location.

Flow:
  1. Run check_client_readiness to get current gap list.
  2. Claude generates a contextual question + hint per gap (one batch call).
  3. Interactive loop: present question, capture answer, write to source,
     move to next gap. Supports skip / back / quit.
  4. For Business Profile gaps: Claude synthesizes the user's free-form
     answer into bulleted facts with an attribution line before appending.
  5. Final readiness re-check shows the delta.

Designed for team members (not developers) to run. All prompts are
plain-English; no JSON / ID handling exposed to the user.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

import anthropic

from src.config import settings
from src.integrations.notion import NotionClient
from src.services.business_profile_populator import (
    _append_facts_under_headings, _section_index,
)
from src.services.client_readiness import run_readiness_check


# ── Claude question generation ──────────────────────────────────────────────

_QUESTION_SYSTEM = """\
You are helping a marketing-agency team member fill out gaps in a client's
data across their Clients DB, Client Info DB, Brand Guidelines DB, and
Business Profile page.

For each gap you're given, generate:
  - A clear, CONVERSATIONAL question a team member can answer. No jargon.
    Reference the CLIENT BY NAME to make context obvious.
  - A short "hint" — examples or prompts for what kind of answer is helpful.
  - An "answer_format" string — one of:
      "short"     = expect 1-5 words (color, font, phone, email, select value)
      "sentence"  = expect one sentence
      "paragraph" = expect a paragraph or short list (voice, populations,
                    section-level BP answers)
      "list"      = expect a comma-separated list (services, contacts)

Rules:
  - Questions should be concrete and help the team member answer confidently.
  - If the gap is a Business Profile section with "missing specific facts",
    your question should name the SPECIFIC FACTS so they know exactly what
    to address.
  - For Brand Guidelines fields: give 2-3 example answers (especially for
    voice/tone, photography style, CTA style — these are subjective).
  - For DB select fields with known option sets (Pipeline Stage, Business
    Type, etc.), hint at typical options.

Return ONLY a JSON object:
{
  "questions": {
    "<gap_index>": {
      "question": "<question>",
      "hint": "<hint>",
      "answer_format": "short|sentence|paragraph|list"
    }
  }
}
Use the gap_index as the key (0, 1, 2, ...).
"""


async def generate_questions(
    gaps: list[dict], cfg: dict,
) -> dict[int, dict]:
    """Batch-generate contextual questions for every gap. Returns
    {index: {question, hint, answer_format}}."""
    if not gaps:
        return {}

    client_name = cfg.get("name", cfg.get("client_id", "the client"))
    verticals = cfg.get("vertical") or []
    if isinstance(verticals, str):
        verticals = [verticals]
    services = cfg.get("services", {}) or {}
    if isinstance(services, dict):
        active_services = [k for k, v in services.items()
                           if (isinstance(v, dict) and v.get("active")) or v is True]
    else:
        active_services = list(services)

    context = (
        f"CLIENT: {client_name}\n"
        f"Vertical(s): {', '.join(verticals) or 'unspecified'}\n"
        f"Active services: {', '.join(active_services) or 'none'}\n"
    )

    gap_lines: list[str] = []
    for i, g in enumerate(gaps):
        gap_lines.append(
            f"GAP {i}:\n"
            f"  Source: {g['source']}\n"
            f"  Field/Section: {g['field']}\n"
            f"  Why flagged: {g['description']}\n"
        )

    prompt = context + "\n\nGAPS TO ASK ABOUT:\n\n" + "\n".join(gap_lines)

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    resp = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=6000,
        system=_QUESTION_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = (resp.content[0].text or "").strip()
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    out: dict[int, dict] = {}
    for k, v in (data.get("questions", {}) or {}).items():
        try:
            idx = int(k)
        except (ValueError, TypeError):
            continue
        out[idx] = {
            "question":      str(v.get("question", "")).strip(),
            "hint":          str(v.get("hint", "")).strip(),
            "answer_format": str(v.get("answer_format", "sentence")).strip(),
        }
    return out


# ── BP answer → bulleted facts synthesis ────────────────────────────────────

_BP_SYNTH_SYSTEM = """\
Convert a team member's free-form answer about a client's business into
bulleted facts suitable for their Business Profile page. One fact per
bullet. Facts should be concise, stated plainly, and capture what the
team member said without embellishment.

Return ONLY a JSON object:
{
  "facts": ["fact 1", "fact 2", ...]
}

If the answer is a single clean fact, return a single-item list. If the
answer contains multiple distinct facts, split them.
"""


async def synthesize_bp_facts(
    answer: str, section_name: str, client_name: str,
) -> list[str]:
    if not answer.strip():
        return []
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    resp = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=1000,
        system=_BP_SYNTH_SYSTEM,
        messages=[{"role": "user", "content":
            f"Client: {client_name}\n"
            f"Business Profile section: {section_name}\n\n"
            f"Team member's answer:\n{answer}"
        }],
    )
    raw = (resp.content[0].text or "").strip()
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return [answer.strip()]
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return [answer.strip()]
    facts = data.get("facts", []) or []
    return [str(f).strip() for f in facts if str(f).strip()]


# ── Notion field writers ────────────────────────────────────────────────────

def _build_property_value(empty_check: str, answer: str):
    """Convert a string answer into a Notion property value of the right type."""
    v = answer.strip()
    if empty_check == "title":
        return {"title": [{"text": {"content": v[:200]}}]}
    if empty_check == "rich_text":
        return {"rich_text": [{"text": {"content": v[:1990]}}]}
    if empty_check == "select":
        return {"select": {"name": v[:100]}}
    if empty_check == "multi_select":
        opts = [o.strip() for o in v.split(",") if o.strip()]
        return {"multi_select": [{"name": o[:100]} for o in opts]}
    if empty_check == "email":
        return {"email": v}
    if empty_check == "phone_number":
        return {"phone_number": v}
    if empty_check == "url":
        return {"url": v}
    if empty_check == "number":
        try:
            return {"number": float(v)}
        except ValueError:
            return {"number": None}
    if empty_check == "date":
        return {"date": {"start": v}}
    return {"rich_text": [{"text": {"content": v[:1990]}}]}


def _find_spec(source: str, field: str) -> dict | None:
    """Look up the readiness spec for this source+field to get empty_check type."""
    from config.client_readiness import (
        BRAND_GUIDELINES_NICE_TO_HAVE,
        BRAND_GUIDELINES_REQUIRED,
        CLIENT_INFO_NICE_TO_HAVE,
        CLIENT_INFO_REQUIRED,
        CLIENTS_DB_NICE_TO_HAVE,
        CLIENTS_DB_REQUIRED,
    )
    pools = {
        "Clients DB":       CLIENTS_DB_REQUIRED + CLIENTS_DB_NICE_TO_HAVE,
        "Client Info DB":   CLIENT_INFO_REQUIRED + CLIENT_INFO_NICE_TO_HAVE,
        "Brand Guidelines": BRAND_GUIDELINES_REQUIRED + BRAND_GUIDELINES_NICE_TO_HAVE,
    }
    for spec in pools.get(source, []):
        if spec["name"] == field:
            return spec
    return None


async def _write_db_field(
    notion: NotionClient, db_id: str, field: str,
    empty_check: str, answer: str, row_query_filter: dict | None = None,
) -> str:
    """Query the DB, take the first row (or filter-matched row), patch the field."""
    body: dict = {"page_size": 10}
    if row_query_filter:
        body["filter"] = row_query_filter
    rows = await notion._client.request(
        path=f"databases/{db_id}/query", method="POST", body=body,
    )
    results = rows.get("results", [])
    if not results:
        return "no row"
    row_id = results[0]["id"]
    prop_value = _build_property_value(empty_check, answer)
    try:
        await notion._client.request(
            path=f"pages/{row_id}", method="PATCH",
            body={"properties": {field: prop_value}},
        )
        return "updated"
    except Exception as e:
        return f"error: {e}"


async def write_answer(
    notion: NotionClient, cfg: dict, gap: dict, answer: str,
) -> str:
    """Route a user's answer to the correct Notion location."""
    source = gap["source"]
    field  = gap["field"]

    if source == "clients.json":
        return f"skipped (clients.json field — update manually: '{field}')"

    spec = _find_spec(source, field)
    if source == "Business Profile":
        # Synthesize facts, then append under the section
        page_id = cfg.get("business_profile_page_id", "")
        if not page_id:
            return "no BP page id"
        facts = await synthesize_bp_facts(
            answer, field, cfg.get("name", cfg.get("client_id", "client")),
        )
        if not facts:
            return "no facts synthesized"
        section_headings, _ = await _section_index(notion, page_id)
        today = datetime.now().strftime("%Y-%m-%d")
        sections_updated, facts_added, _ = await _append_facts_under_headings(
            notion, page_id, section_headings,
            facts_by_section={field: facts},
            source_label="team Q&A",
            source_date=today,
        )
        return f"appended {facts_added} fact(s) to BP → {field}"

    if not spec:
        return f"no spec found for {source} / {field}"

    if source == "Clients DB":
        import os
        clients_db_id = os.environ.get("NOTION_CLIENTS_DB_ID", "").strip()
        if not clients_db_id:
            return "no NOTION_CLIENTS_DB_ID"
        client_key = cfg.get("client_id") or cfg.get("client_key", "")
        name_substring = cfg.get("name", client_key)
        flt = {"property": "Client Name", "title": {"contains": name_substring}}
        return await _write_db_field(
            notion, clients_db_id, field, spec["empty_check"], answer, flt,
        )
    if source == "Client Info DB":
        return await _write_db_field(
            notion, cfg.get("client_info_db_id", ""),
            field, spec["empty_check"], answer,
        )
    if source == "Brand Guidelines":
        return await _write_db_field(
            notion, cfg.get("brand_guidelines_db_id", ""),
            field, spec["empty_check"], answer,
        )
    return f"unknown source: {source}"


# ── Public orchestrator (used by interactive CLI) ───────────────────────────

async def load_gaps_and_questions(
    notion: NotionClient, cfg: dict, client_key: str,
) -> tuple[list[dict], dict[int, dict]]:
    """Load current readiness report → filter to actionable gaps (blocked +
    partial, skipping clients.json) → generate questions. Returns
    (actionable_gaps, questions_by_index)."""
    report = await run_readiness_check(notion, cfg, client_key)
    actionable = [
        g for g in report["all"]
        if g["severity"] in ("blocked", "partial")
        and g["source"] != "clients.json"   # clients.json edits happen manually
    ]
    questions = await generate_questions(actionable, cfg)
    return actionable, questions
