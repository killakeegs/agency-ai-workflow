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

EXISTING OPEN FLAGS — FROM THE THREADS BELOW (numbered; for EACH decide: still active or resolved?):
{flags_in_batch}

EXISTING OPEN FLAGS — FROM OTHER THREADS (for dedup context only; do not re-emit, do not evaluate):
{flags_other}

For each flag in "FROM THE THREADS BELOW":
- If it's still active based on the current thread state, do NOT re-emit it (already tracked).
- If the current thread state shows it is RESOLVED (promised thing delivered, blocker cleared, action completed, scope confirmed, question answered), include its flag_index in the resolved_flags output with a brief reason.

CRITICAL: Only close a flag when resolution is EXPLICIT in a later message of the threads shown below. If you have any doubt, keep it open — false closures erode trust more than keeping a flag open an extra cycle.

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
      "source_thread_id": "Gmail thread ID the flag came from (REQUIRED — use the thread_id from the email thread block above)",
      "brand_field": "(for rule_set only) which Brand Guidelines field: Words to Avoid | Voice & Tone | Photography Style | Image Direction | POV Notes | CTA Style",
      "brand_value": "(for rule_set only) what to add or update"
    }}
  ],
  "resolved_flags": [
    {{
      "flag_index": N (1-based — matches the # in "EXISTING OPEN FLAGS — FROM THE THREADS BELOW" above),
      "reason": "1-sentence explanation of what in the thread resolves this"
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

## FLAG QUALITY RULES (strict — flags are for the daily briefing)

A flag is ONLY created when there is a concrete, unresolved item that someone must act on. Apply these tests before emitting any flag:

**Each type must meet its bar:**
- `open_action` — explicit unresolved ASK or COMMITMENT by a named person, with a clear deliverable. Not "we should consider X," not "evaluate whether Y."
- `blocker` — a specific deliverable is STUCK on a named dependency (person, approval, external system). Not generic "progress is slow."
- `strategic` — a non-obvious relationship/upsell/risk signal that changes how we work with this client. Not "client is happy," not "future exploration possible."
- `promise_made` — specific commitment with a deliverable or timeframe. Not generic reassurance.
- `scope_change` — services added / removed / paused / re-priced. Not "client asked about X."

**DO NOT FLAG any of the following:**
1. **Scheduling / logistics** — meeting times, calendar coordination, "send the invite," "let's meet Tuesday." These resolve in the next email.
2. **Speculative or future-planning items** — "planned for future exploration," "may want to consider," "could be evaluated later," "possible down the road." Flags require commitment or explicit ask.
3. **Verification questions** — "confirm whether X was completed," "check if Y was done," "verify status of Z." If you're not sure from the thread, that's a knowledge gap, not a flag. Note it in the log summary instead.
4. **Items completed later in the same thread** — if Person A asked for X and Person A or B said "done" later in the same thread, no flag.
5. **Items the client or team closed** — "no action needed," "we'll handle this," "disregard."
6. **Pleasantries / social chatter** — thank-yous, acknowledgements, "sounds good."
7. **RxMedia-internal coordination** with no client-facing impact.

**Thread-level dedup (critical):**
- If multiple emails in the same thread touch on the same underlying item, emit ONE flag capturing the MOST RECENT state.
- Example: Thread evolves from "Amanda proposed 3 times" → "Keegan replied" → "Amanda confirmed Tuesday 9am." Emit ONE flag (or none, since this is scheduling — see rule 1).

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
    existing_flags: list[str] | None = None,
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

    # Split existing flags into "from these threads" (candidates for auto-close)
    # vs "from other threads" (dedup context only — Claude shouldn't judge them).
    thread_ids_in_batch = {t.get("thread_id") for t in threads if t.get("thread_id")}
    flags_in_batch = [
        f for f in (existing_flags or [])
        if f.get("thread_id") and f.get("thread_id") in thread_ids_in_batch
    ]
    flags_other = [
        f for f in (existing_flags or [])
        if not f.get("thread_id") or f.get("thread_id") not in thread_ids_in_batch
    ]

    flags_in_batch_str = "\n".join(
        f"  flag #{i+1}: [{f.get('type', 'OPEN_ACTION')}] {f.get('description', '')[:200]}"
        for i, f in enumerate(flags_in_batch)
    ) if flags_in_batch else "(none from these threads)"

    flags_other_str = "\n".join(
        f"  - [{f.get('type', 'OPEN_ACTION')}] {f.get('description', '')[:200]}"
        for f in flags_other
    ) if flags_other else "(none)"

    system = SYNTHESIS_SYSTEM.format(
        existing_entries=existing_entries_str,
        existing_profile=existing_profile_str,
        flags_in_batch=flags_in_batch_str,
        flags_other=flags_other_str,
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
    result = json.loads(match.group(0))

    # Enrich resolved_flags with the actual Notion page ID + description, so the
    # caller can PATCH them directly without re-looking up by index.
    resolved_raw = result.get("resolved_flags", []) or []
    resolved_enriched: list[dict] = []
    for r in resolved_raw:
        try:
            idx = int(r.get("flag_index", 0)) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(flags_in_batch):
            src = flags_in_batch[idx]
            resolved_enriched.append({
                "flag_id":          src.get("id", ""),
                "flag_description": src.get("description", ""),
                "reason":           (r.get("reason") or "").strip(),
            })
    result["resolved_flags"] = resolved_enriched
    return result


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

_STOPWORDS = {
    "the","a","an","to","of","in","on","for","and","or","is","be","by","with",
    "at","as","from","that","this","it","new","any","also","must","needs","need",
    "will","would","should","can","has","have","had","was","were","been","being",
    "are","not","no","but","if","when","where","how","what","so","we","our","you",
    "rxmedia","client","flag","update","confirm","currently","pending","some",
    "all","via","per","etc","additional","related","still","other","ensure",
}


def _keyword_set(text: str) -> set[str]:
    """Reduce a flag description to its content words for fuzzy matching."""
    # Strip [TAGS], (dates), punctuation, lowercase, split, drop stopwords + tiny words
    text = re.sub(r"\[[^\]]+\]", " ", text)
    text = re.sub(r"\(\d{4}-\d{2}-\d{2}\)", " ", text)
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if len(w) > 3 and w not in _STOPWORDS}


def _is_duplicate(new_desc: str, existing_keywords: list[set[str]], threshold: float = 0.55) -> bool:
    """True if new_desc shares >= threshold fraction of its content words with any existing flag."""
    new_kw = _keyword_set(new_desc)
    if len(new_kw) < 3:
        # Too few content words — fall back to exact-lowercased match via caller
        return False
    for exist_kw in existing_keywords:
        if not exist_kw:
            continue
        overlap = len(new_kw & exist_kw)
        smaller = min(len(new_kw), len(exist_kw))
        if smaller == 0:
            continue
        if overlap / smaller >= threshold:
            return True
    return False


async def load_open_flags(
    notion: NotionClient,
    flags_db_id: str,
    client_key: str,
) -> list[dict]:
    """Return list of open flags for a client as {type, description, thread_id} dicts.
    Used both for dedup on write AND as context fed to Claude during synthesis.
    """
    out: list[dict] = []
    if not flags_db_id or not client_key:
        return out
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
                "sorts": [{"property": "Source Date", "direction": "descending"}],
            },
        )
    except Exception:
        return out
    for row in rows.get("results", []):
        props = row.get("properties", {})
        desc_parts = props.get("Description", {}).get("rich_text", [])
        desc = "".join(p.get("text", {}).get("content", "") for p in desc_parts)
        flag_type = (props.get("Type", {}).get("select") or {}).get("name", "OPEN_ACTION")
        tid_parts = props.get("Source Thread ID", {}).get("rich_text", [])
        thread_id = "".join(p.get("text", {}).get("content", "") for p in tid_parts)
        if desc:
            out.append({
                "id": row["id"],
                "type": flag_type,
                "description": desc,
                "thread_id": thread_id,
            })
    return out


async def write_flags_to_db(
    notion: NotionClient,
    flags_db_id: str,
    client_name: str,
    client_key: str,
    flags: list[dict],
    source: str = "Email",
) -> list[dict]:
    """Write non-rule_set flags to the workspace Flags DB. Returns the flag dicts
    that were actually created (post-dedup). Callers that only need the count can
    use len() on the result.

    Dedups against existing Open/In Progress flags for the same client by description.
    rule_set flags are skipped here (they flow to Brand Guidelines via apply_rule_set_flags).
    """
    actionable = [f for f in flags if f.get("type") != "rule_set"]
    if not actionable or not flags_db_id:
        return []

    existing_flags = await load_open_flags(notion, flags_db_id, client_key)
    existing_exact = {f["description"].strip().lower()[:200] for f in existing_flags}
    # Per-thread keyword sets — used for tighter same-thread dedup
    existing_by_thread: dict[str, list[set[str]]] = {}
    existing_keywords_global: list[set[str]] = []
    for f in existing_flags:
        kw = _keyword_set(f["description"])
        existing_keywords_global.append(kw)
        tid = f.get("thread_id") or ""
        if tid:
            existing_by_thread.setdefault(tid, []).append(kw)
    created: list[dict] = []

    for f in actionable:
        description = (f.get("description") or "").strip()
        if not description:
            continue
        key = description.lower()[:200]
        if key in existing_exact:
            continue

        flag_tid = (f.get("source_thread_id") or "").strip()

        # Same-thread flags share context — apply very tight dedup (15% overlap)
        # Rationale: one email thread ≈ one conversation. Multiple flags from it
        # are overwhelmingly the same underlying task written at different thread states.
        if flag_tid and flag_tid in existing_by_thread:
            if _is_duplicate(description, existing_by_thread[flag_tid], threshold=0.15):
                continue

        # Cross-thread dedup — standard threshold (55%)
        if _is_duplicate(description, existing_keywords_global, threshold=0.55):
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
        if flag_tid:
            props["Source Thread ID"] = {"rich_text": [{"text": {"content": flag_tid}}]}

        try:
            await notion._client.request(
                path="pages", method="POST",
                body={"parent": {"database_id": flags_db_id}, "properties": props},
            )
            created.append(f)
            existing_exact.add(key)
            kw = _keyword_set(description)
            existing_keywords_global.append(kw)
            if flag_tid:
                existing_by_thread.setdefault(flag_tid, []).append(kw)
        except Exception as e:
            print(f"    ⚠ Failed to write flag '{title}': {e}")

    return created


# ── Auto-close resolved flags ──────────────────────────────────────────────────

async def auto_close_resolved_flags(
    notion: NotionClient,
    flags_db_id: str,
    resolved: list[dict],
    dry_run: bool = False,
) -> int:
    """Mark flags as Resolved when a later message in the source thread shows
    them resolved. Each item in `resolved` must carry flag_id + reason (as
    enriched by synthesize_threads). Returns count closed (or would-close in
    dry run). Appends an auditable note; never overwrites existing Notes.
    """
    if not resolved or not flags_db_id:
        return 0

    today = datetime.now().strftime("%Y-%m-%d")
    closed = 0

    for r in resolved:
        flag_id = (r.get("flag_id") or "").strip()
        reason  = (r.get("reason") or "").strip() or "resolved in thread update"
        if not flag_id:
            continue

        preview = (r.get("flag_description") or "")[:80]

        if dry_run:
            print(f"    [DRY RUN] would close: {preview} — {reason[:140]}")
            closed += 1
            continue

        try:
            existing = await notion._client.request(path=f"pages/{flag_id}", method="GET")
            existing_notes = "".join(
                p.get("text", {}).get("content", "")
                for p in existing.get("properties", {}).get("Notes", {}).get("rich_text", [])
            )
            auto_note = f"auto-closed {today}: {reason[:400]}"
            new_notes = f"{existing_notes}\n\n{auto_note}" if existing_notes.strip() else auto_note

            await notion._client.request(
                path=f"pages/{flag_id}",
                method="PATCH",
                body={"properties": {
                    "Status":        {"select": {"name": "Resolved"}},
                    "Resolved Date": {"date": {"start": today}},
                    "Notes":         {"rich_text": [{"text": {"content": new_notes[:2000]}}]},
                }},
            )
            closed += 1
        except Exception as e:
            print(f"    ⚠ Failed to auto-close flag {flag_id[:8]}: {e}")

    return closed


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
