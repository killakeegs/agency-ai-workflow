"""
Email enrichment service — shared core for backfill + real-time monitor.

Responsibilities:
  - Synthesize email threads → log entries + profile enrichments + flags
  - Dedup against existing Client Log (by thread_id or subject+date)
  - Write log entries, profile enrichments, and flags to Notion
  - rule_set flags → auto-update Brand Guidelines (Words to Avoid, etc.)
"""
from __future__ import annotations

import json
import re
from datetime import datetime

import anthropic

from src.config import settings
from src.integrations.notion import NotionClient


# ── Claude synthesis ───────────────────────────────────────────────────────────

SYNTHESIS_SYSTEM = """\
You are analyzing email history between an agency (RxMedia — Keegan Warrington, keegan@rxmedia.io) and one of its clients.

Extract THREE things:

1. CLIENT LOG ENTRIES — one per substantive thread or topic. Merge back-and-forths on the same topic into ONE rich entry. Skip pure confirmations, logistics, and social chatter.

2. BUSINESS PROFILE ENRICHMENTS — new factual information about the client's business revealed in emails (staffing changes, new services, pricing decisions, tech stack, strategic shifts, etc.).

3. FLAGS — open action items, promises, scope changes, blockers, AND client rules/constraints.

EXISTING CLIENT LOG ENTRIES (do NOT create duplicates):
{existing_entries}

EXISTING BUSINESS PROFILE (only surface genuinely NEW facts not already here):
{existing_profile}

Output ONLY a JSON object, no preamble:

{{
  "log_entries": [
    {{
      "date": "YYYY-MM-DD (date of last message in thread)",
      "direction": "inbound | outbound | mixed",
      "subject": "email subject (from first message)",
      "thread_id": "Gmail thread ID (from thread data)",
      "message_count": N,
      "attendees": "comma-separated names + emails",
      "summary": "Structured recap — see RULES below. Always attribute by speaker and tag projects.",
      "key_decisions": "What was DECIDED. Attribute by speaker. Tag the project each decision applies to.",
      "action_items": "What was COMMITTED TO. Attribute by owner. Tag the project each item applies to."
    }}
  ],
  "profile_enrichments": [
    {{
      "section": "exact section name from Business Profile (e.g. Services Overview, Staffing & Team, Tech Stack, Insurance & Payment, Common Objections & FAQs, etc.)",
      "fact": "the new fact — one concise sentence or short paragraph"
    }}
  ],
  "flags": [
    {{
      "type": "open_action | scope_change | blocker | promise_made | strategic | rule_set",
      "description": "what needs attention — attribute by speaker + tag project",
      "source_date": "YYYY-MM-DD",
      "brand_field": "(for rule_set only) which Brand Guidelines field: Words to Avoid | Voice & Tone | Photography Style | Image Direction | POV Notes | CTA Style",
      "brand_value": "(for rule_set only) what to add or update"
    }}
  ],
  "skipped_count": N
}}

## ATTRIBUTION RULES (non-negotiable)

Every decision, action item, and key point must explicitly name WHO said/suggested/agreed to it.
- Use the actual sender's name: "Keegan suggested X", "Brandon confirmed Y", "Sarah requested Z".
- Never use ambiguous phrasing like "the team agreed", "they decided", "it was confirmed". Always name the person.
- When one party proposes and the other confirms, capture BOTH: "Keegan suggested removing the loading screen; Brandon confirmed."
- For auto-replies or system messages, don't extract anything — skip them.

## PROJECT DISAMBIGUATION RULES (critical when multiple projects are discussed)

When a single thread discusses multiple distinct projects, products, or websites, each decision/action/flag MUST be tagged with the project it applies to.
- Example: if a thread covers BOTH "Growth Code 27 website" AND "WWMP website", every action item must specify which one: "[WWMP] Fix contact form" vs. "[GC27] Build new hero image".
- Never assume both projects share the same decision — they likely don't.
- If you can't tell which project an item applies to, flag it in the description as "(project unclear — confirm with sender)".
- When in doubt, split into separate log entries per project rather than merging.

## GENERAL RULES

- Use exact dates from the thread headers.
- Only include facts actually stated in the emails. Do not infer or embellish.
- If a thread is pure scheduling / acknowledgement / auto-reply, skip it — increment skipped_count.
- Do NOT duplicate any existing Client Log entry (check subjects and dates).
- Do NOT surface enrichments already present in the existing Business Profile.
- For rule_set flags: these are client-stated constraints like "don't use this word", "we don't offer X", "never describe us as Y", "use warmer imagery." Map to the Brand Guidelines field where it belongs.
- Compress aggressively. Quality over quantity. One rich consolidated entry beats five fragmented ones.
"""


async def synthesize_threads(
    threads: list[dict],
    client_name: str,
    existing_log_entries: list[str],
    existing_profile: str,
) -> dict:
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    threads_block = "\n\n===== THREAD =====\n".join(
        f"Subject: {t['subject']}\n"
        f"Thread ID: {t['thread_id']}\n"
        f"Dates: {t['first_date']} → {t['last_date']} ({t['message_count']} msgs)\n"
        f"Participants: {', '.join(t['participants'])}\n"
        f"Last direction: {t['direction']}\n"
        f"Body:\n{t['body']}"
        for t in threads
    )

    existing_entries_str = "\n".join(
        f"  - {e}" for e in existing_log_entries
    ) if existing_log_entries else "(none — first enrichment run)"

    existing_profile_str = existing_profile[:8000] if existing_profile else "(empty)"

    system = SYNTHESIS_SYSTEM.format(
        existing_entries=existing_entries_str,
        existing_profile=existing_profile_str,
    )

    prompt = f"""Client: {client_name}

Email threads to analyze ({len(threads)} threads):

{threads_block}

Return the JSON object as specified."""

    response = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError("Could not parse Claude response as JSON")
    return json.loads(match.group(0))


# ── Dedup: load existing Client Log entries ────────────────────────────────────

async def load_existing_log_entries(
    notion: NotionClient, log_db_id: str, days: int = 365
) -> tuple[list[str], dict[str, dict]]:
    """Return (summary strings for Claude context, thread_id → {page_id, msg_count} map)."""
    summaries: list[str] = []
    thread_map: dict[str, dict] = {}

    try:
        rows = await notion._client.request(
            path=f"databases/{log_db_id}/query",
            method="POST",
            body={"page_size": 100, "sorts": [{"property": "Date", "direction": "descending"}]},
        )
    except Exception:
        return [], {}

    for row in rows.get("results", []):
        props = row.get("properties", {})

        title_parts = props.get("Title", {}).get("title", [])
        title = "".join(p.get("text", {}).get("content", "") for p in title_parts)

        date_obj = props.get("Date", {}).get("date")
        date_str = date_obj.get("start", "") if date_obj else ""

        tid_parts = props.get("Gmail Thread ID", {}).get("rich_text", [])
        tid = "".join(p.get("text", {}).get("content", "") for p in tid_parts)

        msg_count = props.get("Message Count", {}).get("number") or 0

        if tid:
            thread_map[tid] = {"page_id": row["id"], "msg_count": msg_count}

        summaries.append(f"[{date_str}] {title}")

    return summaries[:80], thread_map


# ── Notion writes ──────────────────────────────────────────────────────────────

async def write_client_log(
    notion: NotionClient,
    log_db_id: str,
    client_name: str,
    entries: list[dict],
    thread_map: dict[str, dict],
) -> tuple[int, int]:
    """Write or update Client Log entries. Returns (created, updated)."""
    created = 0
    updated = 0

    await _ensure_log_thread_field(notion, log_db_id)

    for e in entries:
        date_str = e.get("date", "")
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue

        thread_id = e.get("thread_id", "")
        msg_count = e.get("message_count", 1)

        direction = e.get("direction", "inbound")
        type_select = "Email Inbound" if direction != "outbound" else "Email Outbound"

        subject = e.get("subject", "Email")
        title = f"{client_name} — Email — {subject[:60]} — {date_str}"

        props: dict = {
            "Title":         {"title": [{"text": {"content": title}}]},
            "Date":          {"date": {"start": date_str}},
            "Type":          {"select": {"name": type_select}},
            "Attendees":     {"rich_text": [{"text": {"content": e.get("attendees", "")[:2000]}}]},
            "Summary":       {"rich_text": [{"text": {"content": e.get("summary", "")[:2000]}}]},
            "Key Decisions": {"rich_text": [{"text": {"content": e.get("key_decisions", "")[:2000]}}]},
            "Action Items":  {"rich_text": [{"text": {"content": e.get("action_items", "")[:2000]}}]},
            "Processed":     {"checkbox": True},
            "Source":        {"rich_text": [{"text": {"content": "Enriched from Gmail"}}]},
        }
        if thread_id:
            props["Gmail Thread ID"] = {"rich_text": [{"text": {"content": thread_id}}]}
        props["Message Count"] = {"number": msg_count}

        existing = thread_map.get(thread_id, {}) if thread_id else {}

        if existing and existing.get("msg_count", 0) >= msg_count:
            continue

        try:
            if existing and existing.get("page_id"):
                await notion._client.request(
                    path=f"pages/{existing['page_id']}",
                    method="PATCH",
                    body={"properties": props},
                )
                updated += 1
                thread_map[thread_id] = {"page_id": existing["page_id"], "msg_count": msg_count}
            else:
                result = await notion._client.request(
                    path="pages", method="POST",
                    body={"parent": {"database_id": log_db_id}, "properties": props},
                )
                created += 1
                if thread_id:
                    thread_map[thread_id] = {"page_id": result["id"], "msg_count": msg_count}
        except Exception as ex:
            print(f"    ⚠ Failed to write log entry for {date_str}: {ex}")

    return created, updated


async def _ensure_log_thread_field(notion: NotionClient, log_db_id: str) -> None:
    """Add Gmail Thread ID + Message Count fields to Client Log DB if missing."""
    try:
        db = await notion._client.request(path=f"databases/{log_db_id}", method="GET")
        patches: dict = {}
        if "Gmail Thread ID" not in db.get("properties", {}):
            patches["Gmail Thread ID"] = {"rich_text": {}}
        if "Message Count" not in db.get("properties", {}):
            patches["Message Count"] = {"number": {}}
        if patches:
            await notion._client.request(
                path=f"databases/{log_db_id}",
                method="PATCH",
                body={"properties": patches},
            )
    except Exception:
        pass


async def append_profile_enrichments(
    notion: NotionClient,
    profile_page_id: str,
    enrichments: list[dict],
    flags: list[dict],
    days: int,
) -> None:
    """Append new factual enrichments to Business Profile. Flags go to the Flags DB,
    not bullet lists on the profile page."""
    if not enrichments:
        return

    today = datetime.now().strftime("%Y-%m-%d")
    blocks: list[dict] = [
        {
            "object": "block",
            "type": "heading_2",
            "heading_2": {
                "rich_text": [{"type": "text", "text": {"content": f"Email Enrichment — {today}"}}],
            },
        },
        {
            "object": "block",
            "type": "callout",
            "callout": {
                "icon": {"type": "emoji", "emoji": "📧"},
                "rich_text": [{"type": "text", "text": {
                    "content": f"Synthesized from Gmail threads (last {days} days). Review and merge into appropriate sections above."
                }}],
            },
        },
        {
            "object": "block", "type": "heading_3",
            "heading_3": {"rich_text": [{"type": "text", "text": {"content": "New Facts"}}]},
        },
    ]

    for e in enrichments:
        content = f"[{e.get('section', 'General')}] {e.get('fact', '')}"
        blocks.append({
            "object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": content[:1900]}}]},
        })

    for i in range(0, len(blocks), 100):
        await notion._client.request(
            path=f"blocks/{profile_page_id}/children",
            method="PATCH",
            body={"children": blocks[i : i + 100]},
        )


# ── Flags DB writes ────────────────────────────────────────────────────────────

async def _load_open_flag_descriptions(
    notion: NotionClient,
    flags_db_id: str,
    client_key: str,
) -> set[str]:
    """Return lowercased set of existing Open flag descriptions for this client (dedup)."""
    existing: set[str] = set()
    if not flags_db_id or not client_key:
        return existing
    try:
        rows = await notion._client.request(
            path=f"databases/{flags_db_id}/query",
            method="POST",
            body={
                "page_size": 100,
                "filter": {
                    "and": [
                        {"property": "Client Key", "rich_text": {"equals": client_key}},
                        {"property": "Status", "select": {"does_not_equal": "Resolved"}},
                    ]
                },
            },
        )
    except Exception:
        return existing
    for row in rows.get("results", []):
        props = row.get("properties", {})
        desc_parts = props.get("Description", {}).get("rich_text", [])
        desc = "".join(p.get("text", {}).get("content", "") for p in desc_parts)
        if desc:
            existing.add(desc.strip().lower()[:200])
    return existing


async def write_flags_to_db(
    notion: NotionClient,
    flags_db_id: str,
    client_name: str,
    client_key: str,
    flags: list[dict],
    source: str = "Email",
) -> int:
    """Write non-rule_set flags to the workspace Flags DB. Returns count created.

    Dedups against existing Open/In Progress flags for the same client by description.
    rule_set flags are skipped here (they flow to Brand Guidelines via apply_rule_set_flags).
    """
    actionable = [f for f in flags if f.get("type") != "rule_set"]
    if not actionable or not flags_db_id:
        return 0

    existing = await _load_open_flag_descriptions(notion, flags_db_id, client_key)
    created = 0

    for f in actionable:
        description = (f.get("description") or "").strip()
        if not description:
            continue
        key = description.lower()[:200]
        if key in existing:
            continue

        flag_type = (f.get("type") or "open_action").upper()
        source_date = f.get("source_date") or datetime.now().strftime("%Y-%m-%d")
        title = description[:80]

        props: dict = {
            "Title":       {"title": [{"text": {"content": title}}]},
            "Client":      {"rich_text": [{"text": {"content": client_name}}]},
            "Client Key":  {"rich_text": [{"text": {"content": client_key}}]},
            "Type":        {"select": {"name": flag_type}},
            "Status":      {"select": {"name": "Open"}},
            "Description": {"rich_text": [{"text": {"content": description[:2000]}}]},
            "Source":      {"select": {"name": source}},
            "Source Date": {"date": {"start": source_date}},
        }

        try:
            await notion._client.request(
                path="pages", method="POST",
                body={"parent": {"database_id": flags_db_id}, "properties": props},
            )
            created += 1
            existing.add(key)
        except Exception as e:
            print(f"    ⚠ Failed to write flag '{title}': {e}")

    return created


# ── rule_set → Brand Guidelines auto-write ─────────────────────────────────────

BRAND_FIELD_MAP = {
    "Words to Avoid":     "Words to Avoid",
    "Voice & Tone":       "Voice & Tone",
    "Photography Style":  "Photography Style",
    "Image Direction":    "Image Direction",
    "POV Notes":          "POV Notes",
    "CTA Style":          "CTA Style",
}


async def apply_rule_set_flags(
    notion: NotionClient,
    brand_db_id: str,
    flags: list[dict],
) -> int:
    """Write rule_set flags to the Brand Guidelines DB."""
    rule_flags = [f for f in flags if f.get("type") == "rule_set"]
    if not rule_flags or not brand_db_id:
        return 0

    # Read existing brand row
    try:
        rows = await notion._client.request(
            path=f"databases/{brand_db_id}/query", method="POST", body={"page_size": 1},
        )
    except Exception:
        return 0

    if not rows.get("results"):
        return 0

    page_id = rows["results"][0]["id"]
    props = rows["results"][0].get("properties", {})

    updates: dict = {}
    applied = 0

    for flag in rule_flags:
        brand_field = BRAND_FIELD_MAP.get(flag.get("brand_field", ""), "")
        value = flag.get("brand_value", "").strip()
        if not brand_field or not value:
            continue

        existing_prop = props.get(brand_field, {})
        existing_text = "".join(
            p.get("text", {}).get("content", "")
            for p in existing_prop.get("rich_text", [])
        )

        # Append, don't overwrite — separate with semicolon if existing content
        if value.lower() in existing_text.lower():
            continue

        new_text = f"{existing_text}; {value}" if existing_text.strip() else value
        updates[brand_field] = {"rich_text": [{"text": {"content": new_text[:2000]}}]}
        applied += 1

    if updates:
        try:
            await notion._client.request(
                path=f"pages/{page_id}",
                method="PATCH",
                body={"properties": updates},
            )
        except Exception as e:
            print(f"    ⚠ Could not update Brand Guidelines: {e}")
            return 0

    return applied


# ── Last Contact update ────────────────────────────────────────────────────────

async def update_last_contact(
    notion: NotionClient,
    client_name: str,
    contact_date: str,
) -> None:
    """Update the Last Contact date on the top-level Clients DB row."""
    import os
    clients_db_id = os.environ.get("NOTION_CLIENTS_DB_ID", "").strip()
    if not clients_db_id:
        return

    try:
        rows = await notion._client.request(
            path=f"databases/{clients_db_id}/query",
            method="POST",
            body={
                "page_size": 100,
                "filter": {"property": "Client Name", "title": {"equals": client_name}},
            },
        )
        if rows.get("results"):
            page_id = rows["results"][0]["id"]
            await notion._client.request(
                path=f"pages/{page_id}",
                method="PATCH",
                body={"properties": {"Last Contact": {"date": {"start": contact_date}}}},
            )
    except Exception:
        pass
