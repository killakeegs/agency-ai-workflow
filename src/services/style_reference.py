"""
Style Reference service — the agent feedback loop.

Every approval, rejection, or edit of an agent output gets logged here with
the *reason why*. At the start of a run, agents call get_recent_examples()
to pull approved/edited outputs for their agent+asset type on this client,
then prime generation from those examples.

This is how per-client voice compounds over time instead of drifting back
to generic LLM output each run.

Two entry points:
  - log_feedback(): append one decision to the Style Reference DB
  - get_recent_examples(): fetch recent entries to prime an agent prompt

See CLAUDE.md § Agent Design Principles and the AI-First SEO plan
(§ Style Reference + Feedback Loop) for architectural context.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.integrations.notion import NotionClient


# ── Constants ─────────────────────────────────────────────────────────────────

# Decisions that carry useful signal. Rejected entries are logged for the
# reason (what NOT to do) but aren't primed as positive examples.
POSITIVE_DECISIONS = ("Approved", "Approved with Edits")

# Cap text fields to keep Notion writes under rich_text limits (2000 chars per
# block). For longer outputs we truncate and append a marker — the goal is
# priming, not archival.
MAX_TEXT_LEN = 1800


# ── Write path ────────────────────────────────────────────────────────────────

async def log_feedback(
    notion: NotionClient,
    style_reference_db_id: str,
    agent: str,
    asset_type: str,
    decision: str,
    reason: str,
    original_output: str,
    final_output: str = "",
    target: str = "",
    reviewer: str = "",
    title: str | None = None,
) -> str:
    """
    Append one feedback entry to the Style Reference DB.

    Call this whenever a human approves, rejects, or edits agent output.
    Can be invoked from a Notion webhook, a Slack approval button, a
    Railway cron that sweeps "Approved" status changes, or a one-off
    `make style-log` command.

    Returns the new entry's Notion page ID.
    """
    if not title:
        # Default title includes the agent, asset, and decision so the DB
        # reads well in the Notion UI without opening each row.
        snippet = _first_line(original_output)[:60]
        title = f"{agent} · {asset_type} · {decision}: {snippet}"

    properties: dict[str, Any] = {
        "Title":           {"title": [{"text": {"content": title[:200]}}]},
        "Agent":           {"select": {"name": agent}},
        "Asset Type":      {"select": {"name": asset_type}},
        "Decision":        {"select": {"name": decision}},
        "Reason":          _rich_text(reason),
        "Original Output": _rich_text(original_output),
        "Logged Date":     {"date": {"start": datetime.now(timezone.utc).date().isoformat()}},
    }

    if final_output:
        properties["Final Output"] = _rich_text(final_output)
    if target:
        properties["Target"] = _rich_text(target)
    if reviewer:
        properties["Reviewer"] = _rich_text(reviewer)

    return await notion.create_database_entry(
        database_id=style_reference_db_id,
        properties=properties,
    )


# ── Read path ─────────────────────────────────────────────────────────────────

async def get_recent_examples(
    notion: NotionClient,
    style_reference_db_id: str,
    agent: str,
    asset_type: str | None = None,
    decisions: tuple[str, ...] = POSITIVE_DECISIONS,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """
    Fetch recent positive examples for an agent+asset combo.

    Returns a list of dicts with keys: title, decision, reason, original,
    final, target, reviewer, logged_date. The `final` field reflects what
    actually shipped after edits — prime prompts from this field when
    present, fall back to `original` when not.

    Agents should call this at the start of run() and pass the examples
    into the generation prompt as "here's what the team approved before,
    with the reasons why — match this style."
    """
    filters: list[dict[str, Any]] = [
        {"property": "Agent", "select": {"equals": agent}},
    ]
    if asset_type:
        filters.append({"property": "Asset Type", "select": {"equals": asset_type}})
    if decisions:
        if len(decisions) == 1:
            filters.append({"property": "Decision", "select": {"equals": decisions[0]}})
        else:
            filters.append({
                "or": [
                    {"property": "Decision", "select": {"equals": d}}
                    for d in decisions
                ]
            })

    filter_payload: dict[str, Any]
    if len(filters) == 1:
        filter_payload = filters[0]
    else:
        filter_payload = {"and": filters}

    pages = await notion.query_database(
        database_id=style_reference_db_id,
        filter_payload=filter_payload,
        sorts=[{"property": "Logged Date", "direction": "descending"}],
    )

    return [_extract_example(p) for p in pages[:limit]]


def format_examples_for_prompt(examples: list[dict[str, Any]]) -> str:
    """
    Format examples as a prompt-ready string.

    Agents can drop this directly into their system prompt. Each example
    shows the decision, reason, and final output (or original if no edit).
    """
    if not examples:
        return "(no prior approved examples for this client — generate from first principles)"

    blocks: list[str] = []
    for i, ex in enumerate(examples, 1):
        shipped = ex["final"] or ex["original"]
        target_line = f" [{ex['target']}]" if ex["target"] else ""
        blocks.append(
            f"--- Example {i} ({ex['decision']}){target_line} ---\n"
            f"WHY: {ex['reason']}\n"
            f"SHIPPED: {shipped}"
        )
    return "\n\n".join(blocks)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _rich_text(text: str) -> dict[str, Any]:
    """Build a Notion rich_text property value, truncating long input."""
    truncated = text[:MAX_TEXT_LEN]
    if len(text) > MAX_TEXT_LEN:
        truncated += " …[truncated]"
    return {"rich_text": [{"text": {"content": truncated}}]}


def _first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return text.strip()


def _extract_example(page: dict[str, Any]) -> dict[str, Any]:
    """Flatten a Notion Style Reference row into a plain dict."""
    props = page.get("properties", {})
    return {
        "page_id":     page.get("id", ""),
        "title":       _plain(props.get("Title", {}), key="title"),
        "decision":    _select(props.get("Decision")),
        "reason":      _plain(props.get("Reason", {})),
        "original":    _plain(props.get("Original Output", {})),
        "final":       _plain(props.get("Final Output", {})),
        "target":      _plain(props.get("Target", {})),
        "reviewer":    _plain(props.get("Reviewer", {})),
        "logged_date": (props.get("Logged Date", {}).get("date") or {}).get("start", ""),
    }


def _plain(prop: dict[str, Any], key: str = "rich_text") -> str:
    items = prop.get(key, []) or []
    return "".join(item.get("plain_text") or item.get("text", {}).get("content", "") for item in items)


def _select(prop: dict[str, Any] | None) -> str:
    if not prop:
        return ""
    sel = prop.get("select")
    return sel.get("name", "") if sel else ""
