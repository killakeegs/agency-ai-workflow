#!/usr/bin/env python3
"""
meeting_processor.py — Automated meeting transcript processor.

Polls Meeting Transcripts DB for Processed=False entries, reads the full
Notion AI transcript, matches to client, and runs Rex's meeting pipeline:
  1. Parse transcript with Claude → 12 structured sections
  2. Write to client's Client Log DB
  3. Create ClickUp tasks from action items
  4. Draft follow-up email (Gmail draft or Slack approval)
  5. Post summary to Slack
  6. Mark transcript as Processed

Runs as a Railway cron (every 10 min) or manually via make meeting-processor.

Usage:
    make meeting-processor                    # Process all unprocessed transcripts
    make meeting-processor CLIENT=pdx_plumber # Process only for one client
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent.parent / ".env")

from config.clients import CLIENTS
from src.config import settings
from src.integrations.notion import NotionClient

# Rex's meeting tools — already built, fully tested
from rex.tools.meeting_tools import (
    _parse_transcript,
    _write_client_log,
    _create_clickup_tasks,
    _draft_follow_up_email,
)
from rex.tools.email_tools import send_email, create_gmail_draft
from src.services.email_enrichment import write_flags_to_db

MEETING_TRANSCRIPTS_DB = os.environ.get(
    "NOTION_MEETING_TRANSCRIPTS_DB_ID",
    "343f7f45-333e-81cf-8f2f-ebfd074fc9fd",
).strip()

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "").strip()
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL_INTERNAL", "").strip()
FLAGS_DB_ID = os.environ.get("NOTION_FLAGS_DB_ID", "").strip()


def _parsed_to_flags(parsed: dict, meeting_date: str, transcript_page_id: str = "") -> list[dict]:
    """Convert parsed meeting output into Flags DB entries.

    Only flows: risk_flags, value_add_opportunities, out-of-scope client_requests.
    Action items already become ClickUp tasks — don't duplicate as flags.
    The transcript_page_id acts as the source_thread_id so same-meeting flags dedupe tightly.
    """
    flags: list[dict] = []

    for rf in parsed.get("risk_flags", []) or []:
        severity = (rf.get("severity", "") or "").lower()
        flag_type = "blocker" if severity == "high" else "strategic"
        flags.append({
            "type": flag_type,
            "description": rf.get("flag", "").strip(),
            "source_date": meeting_date,
            "source_thread_id": transcript_page_id,
        })

    for va in parsed.get("value_add_opportunities", []) or []:
        opp = va.get("opportunity", "").strip()
        potential = va.get("potential_service", "").strip()
        current = va.get("current_service", "").strip()
        if not opp:
            continue
        desc = opp
        if potential:
            desc += f" (potential: {potential}"
            if current:
                desc += f"; current: {current}"
            desc += ")"
        flags.append({
            "type": "strategic",
            "description": desc,
            "source_date": meeting_date,
            "source_thread_id": transcript_page_id,
        })

    for cr in parsed.get("client_requests", []) or []:
        if cr.get("in_scope") is False:
            req = cr.get("request", "").strip()
            if req:
                flags.append({
                    "type": "scope_change",
                    "description": f"Out-of-scope request: {req}",
                    "source_date": meeting_date,
                    "source_thread_id": transcript_page_id,
                })

    return [f for f in flags if f.get("description")]


# ── Read Notion AI transcription blocks ────────────────────────────────────────

def _rt_to_text(rich_text: list) -> str:
    return "".join(r.get("text", {}).get("content", "") for r in rich_text)


async def _read_block_children(notion: NotionClient, block_id: str) -> str:
    """Read all text content from a block's children."""
    parts: list[str] = []
    cursor = None
    while True:
        params = f"?page_size=100"
        if cursor:
            params += f"&start_cursor={cursor}"
        r = await notion._client.request(
            path=f"blocks/{block_id}/children{params}", method="GET",
        )
        for b in r.get("results", []):
            btype = b.get("type", "")
            content = b.get(btype, {})
            text = _rt_to_text(content.get("rich_text", []))
            if btype == "to_do":
                checked = content.get("checked", False)
                parts.append(f"{'[x]' if checked else '[ ]'} {text}")
            elif text.strip():
                if btype in ("heading_1", "heading_2", "heading_3"):
                    parts.append(f"\n## {text}")
                else:
                    parts.append(text)
        if not r.get("has_more"):
            break
        cursor = r.get("next_cursor")
    return "\n".join(parts)


async def _read_transcript_content(notion: NotionClient, page_id: str) -> str:
    """Read full content from a Notion AI meeting transcript page.

    Handles the special `transcription` block type which contains child
    block IDs for summary, notes, and full transcript.
    """
    blocks_resp = await notion._client.request(
        path=f"blocks/{page_id}/children", method="GET",
    )
    blocks = blocks_resp.get("results", [])

    all_text: list[str] = []

    for block in blocks:
        btype = block.get("type", "")

        if btype == "transcription":
            children = block.get("transcription", {}).get("children", {})
            summary_id = children.get("summary_block_id", "")
            notes_id = children.get("notes_block_id", "")
            transcript_id = children.get("transcript_block_id", "")

            if summary_id:
                text = await _read_block_children(notion, summary_id)
                if text.strip():
                    all_text.append(f"## SUMMARY\n{text}")
            if notes_id:
                text = await _read_block_children(notion, notes_id)
                if text.strip():
                    all_text.append(f"## NOTES\n{text}")
            if transcript_id:
                text = await _read_block_children(notion, transcript_id)
                if text.strip():
                    all_text.append(f"## TRANSCRIPT\n{text}")
        else:
            content = block.get(btype, {})
            text = _rt_to_text(content.get("rich_text", []))
            if text.strip():
                all_text.append(text)

    return "\n\n".join(all_text)


# ── Client matching ────────────────────────────────────────────────────────────

RXMEDIA_TEAM = {"keegan", "henna", "justin", "andrea", "karla", "mari"}
RXMEDIA_DOMAINS = {"rxmedia.io"}


def _build_client_email_map() -> tuple[dict[str, str], dict[str, str]]:
    """Build domain → client_key and email → client_key maps from CLIENTS."""
    domain_map: dict[str, str] = {}
    email_map: dict[str, str] = {}
    for key, cfg in CLIENTS.items():
        if cfg.get("internal"):
            continue
        for field in ("email", "primary_contact_email"):
            email = (cfg.get(field, "") or "").lower().strip()
            if not email:
                continue
            email_map[email] = key
            domain = email.split("@")[-1] if "@" in email else ""
            if domain and domain not in ("gmail.com", "rxmedia.io", "google.com", "yahoo.com", "hotmail.com"):
                domain_map[domain] = key
    return domain_map, email_map

_DOMAIN_MAP, _EMAIL_MAP = _build_client_email_map()


def _match_client(client_field: str, attendees_text: str = "") -> str | None:
    """Match to a client. Attendee email domains take precedence over Notion AI's client_field.

    Email-first ordering matters: Notion AI sometimes mis-populates the Client field on
    transcripts (e.g. tagging an internal marketing meeting as "Summit Therapy"). Attendee
    emails reflect who was actually on the call and are a harder signal.
    """
    # Email-first: match by attendee email domain
    if attendees_text:
        emails = re.findall(r"[\w\.-]+@[\w\.-]+", attendees_text.lower())
        for email in emails:
            if email in _EMAIL_MAP:
                return _EMAIL_MAP[email]
            domain = email.split("@")[-1]
            if domain in _DOMAIN_MAP:
                return _DOMAIN_MAP[domain]

    # Fallback: name field matching (Notion AI's client field — may be mis-tagged)
    if client_field:
        client_lower = client_field.strip().lower()
        for key, cfg in CLIENTS.items():
            if cfg.get("internal"):
                continue
            name = cfg.get("name", "").lower()
            if name and (name in client_lower or client_lower in name):
                return key
            if key.replace("_", " ") in client_lower:
                return key

    return None


def _detect_meeting_type(client_field: str, title: str, attendees_text: str) -> str:
    """Determine if a meeting is client, internal, or unrelated.
    Returns: 'client', 'internal', or 'skip'.

    Attendee data is evaluated BEFORE the Notion AI client_field. Notion AI sometimes
    pre-populates the Client field incorrectly (e.g. an internal marketing session tagged
    as "Summit Therapy"). Checking attendees first catches and corrects these mis-tags.
    """
    # Parse attendee emails first — they're more authoritative than Notion AI's field
    attendee_emails: list[str] = []
    if attendees_text:
        attendee_emails = re.findall(r"[\w\.-]+@[\w\.-]+", attendees_text.lower())

    if attendee_emails:
        external = [
            e for e in attendee_emails
            if not any(e.endswith(f"@{d}") for d in RXMEDIA_DOMAINS)
        ]

        # All attendees are RxMedia → internal, regardless of what Notion AI tagged
        if not external:
            return "internal"

        # External attendees — try to match by email domain
        email_client = _match_client("", " ".join(external))
        if email_client:
            # Cross-check: warn when Notion AI's client field disagrees with attendees
            name_client = _match_client(client_field, "") if client_field else None
            if name_client and name_client != email_client:
                print(f"  ⚠ Mis-tag: Notion AI tagged '{client_field}' but attendee "
                      f"emails suggest '{email_client}'. Using attendee match.")
            return "client"

    # No attendee data (or no external attendees matched) — fall back to name/title
    if _match_client(client_field, ""):
        return "client"

    # Title-based internal signals
    title_lower = (title or "").lower()
    internal_signals = ["standup", "team meeting", "internal", "rxmedia", "strategy", "planning"]
    if any(s in title_lower for s in internal_signals):
        return "internal"

    if client_field:
        cl = client_field.lower()
        if any(name in cl for name in RXMEDIA_TEAM):
            return "internal"

    return "skip"


# ── Recipient selection for follow-up emails ──────────────────────────────────

# Always CC these RxMedia internal addresses on every client follow-up email.
# Keep tight — these are the roles who read every client update by policy.
RXMEDIA_ALWAYS_CC = {"content@rxmedia.io"}

# Never put these in TO or CC (they're senders or bots, not recipients).
# Default sender is keegan@rxmedia.io — including him anywhere CC's the sender.
RXMEDIA_NEVER_CC = {"keegan@rxmedia.io"}

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")


def _determine_recipients(
    attendees_text: str,
    parsed_attendee_emails: list[str],
    cfg: dict,
) -> tuple[str, str]:
    """Return (to, cc_csv) for the follow-up email.

    Logic:
    - Collect every email visible in `attendees_text` + `parsed_attendee_emails`.
    - Filter out RxMedia internal addresses (anything @rxmedia.io).
    - If the client's primary contact email is among attendees → use as To.
    - Else first client-side attendee email → To.
    - Else fall back to cfg.primary_contact_email / cfg.email.
    - CC = remaining client attendees + RXMEDIA_ALWAYS_CC, minus To + NEVER_CC.
    - All addresses de-duped, lowercased.
    """
    primary = (cfg.get("primary_contact_email") or cfg.get("email") or "").lower().strip()

    # Collect candidate emails
    raw = set(_EMAIL_RE.findall(attendees_text or ""))
    for e in parsed_attendee_emails or []:
        if e:
            raw.add(e)
    raw = {e.lower().strip() for e in raw if e}

    # Exclude RxMedia internal addresses from the client-side attendee set
    client_attendees = [e for e in raw if not e.endswith("@rxmedia.io")]
    # Stable sort for determinism
    client_attendees.sort()

    # Pick To
    if primary and primary in client_attendees:
        to = primary
        others = [e for e in client_attendees if e != primary]
    elif client_attendees:
        to = client_attendees[0]
        others = client_attendees[1:]
    else:
        to = primary  # fallback — may be empty string
        others = []

    # Build CC
    cc_seen = {to, ""} | RXMEDIA_NEVER_CC
    cc_list: list[str] = []
    for addr in list(others) + list(RXMEDIA_ALWAYS_CC):
        if addr and addr not in cc_seen:
            cc_list.append(addr)
            cc_seen.add(addr)

    return to, ", ".join(cc_list)


# ── Meeting-flag auto-close ────────────────────────────────────────────────────

async def _evaluate_meeting_flags(
    parsed: dict,
    open_flags: list[dict],
    meeting_date: str,
) -> list[dict]:
    """Ask Claude whether this meeting's outcomes resolve any prior open flags.

    Returns list of {flag_id, flag_description, reason} for flags to close.
    Only passes flags to Claude — never auto-closes without Claude's explicit judgment.
    """
    if not open_flags:
        return []

    import anthropic as _anthropic
    client = _anthropic.Anthropic(api_key=settings.anthropic_api_key)

    flags_block = "\n".join(
        f"  flag #{i+1} [id={f['id'][:8]}]: [{f['type']}] {f['description'][:200]}"
        for i, f in enumerate(open_flags[:20])  # cap at 20 to keep prompt tight
    )
    flags_subset = open_flags[:20]

    summary = parsed.get("summary", "")[:600]
    decisions = json.dumps(parsed.get("key_decisions", []) or [], indent=2)[:1200]
    approvals = json.dumps(parsed.get("approvals_given", []) or [], indent=2)[:600]
    action_items = json.dumps(parsed.get("action_items", []) or [], indent=2)[:1200]

    prompt = f"""A meeting was processed on {meeting_date}.

SUMMARY: {summary}

KEY DECISIONS:
{decisions}

APPROVALS GIVEN:
{approvals}

ACTION ITEMS:
{action_items}

OPEN FLAGS FROM PRIOR INTERACTIONS (numbered):
{flags_block}

For each flag: does this meeting's content EXPLICITLY resolve it?
Only mark resolved when the meeting clearly closes the item — a decision made, approval given, blocker cleared, or concern addressed.
If in any doubt, leave it open. False closures are worse than leaving a flag open an extra cycle.

Return ONLY a JSON array. Empty array if nothing is resolved:
[{{"flag_index": N, "reason": "one sentence why this meeting resolves it"}}]"""

    try:
        response = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if not match:
            return []
        resolved_raw = json.loads(match.group(0))
        result: list[dict] = []
        for r in resolved_raw:
            try:
                idx = int(r.get("flag_index", 0)) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(flags_subset):
                src = flags_subset[idx]
                result.append({
                    "flag_id":          src["id"],
                    "flag_description": src["description"],
                    "reason":           (r.get("reason") or "").strip(),
                })
        return result
    except Exception as e:
        print(f"  ⚠ Flag auto-close evaluation failed: {e}")
        return []


# ── Slack ──────────────────────────────────────────────────────────────────────

async def _post_to_slack(text: str, channel: str = "") -> None:
    ch = channel or SLACK_CHANNEL
    if not SLACK_BOT_TOKEN or not ch:
        return
    try:
        async with httpx.AsyncClient() as http:
            await http.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
                json={"channel": ch, "text": text},
                timeout=10.0,
            )
    except Exception:
        pass


# ── Main processing ────────────────────────────────────────────────────────────

async def _process_one(
    notion: NotionClient,
    page_id: str,
    title: str,
    client_field: str,
    meeting_date_str: str,
    is_internal: bool = False,
    attendees_text: str = "",
    meeting_time: datetime | None = None,
) -> dict:
    """Process a single meeting transcript. Returns summary dict.

    ``meeting_time`` is the transcript page's created_time (close to when
    Notion AI joined the meeting). Used to look up the calendar event for
    recipient routing.
    """

    # Match client
    if is_internal:
        client_key = "rxmedia"
    else:
        client_key = _match_client(client_field, attendees_text)
    if not client_key:
        return {"status": "skipped", "reason": f"Could not match client '{client_field}'"}

    cfg = CLIENTS[client_key]
    client_name = cfg.get("name", client_key)
    client_log_db_id = cfg.get("client_log_db_id", "")

    if not client_log_db_id:
        return {"status": "skipped", "reason": f"No Client Log DB for {client_name}"}

    print(f"\n  Processing: {title}")
    print(f"  Client: {client_name} ({client_key})")

    # Read transcript
    transcript = await _read_transcript_content(notion, page_id)
    if len(transcript.strip()) < 100:
        return {"status": "skipped", "reason": f"Transcript too short ({len(transcript)} chars)"}
    print(f"  Transcript: {len(transcript):,} chars")

    # Determine active services
    services = cfg.get("services", {})
    active = [k for k, v in services.items() if v is True] if isinstance(services, dict) else services

    # Parse with Claude
    if not meeting_date_str:
        meeting_date_str = date.today().isoformat()
    print("  Parsing with Claude...")
    parsed = await _parse_transcript(transcript, client_name, active, meeting_date_str)

    meeting_type = parsed.get("meeting_type", "Check-in")
    attendees = parsed.get("attendees", [])
    action_items = parsed.get("action_items", [])
    print(f"  Type: {meeting_type} | Attendees: {len(attendees)} | Actions: {len(action_items)}")

    # Write to Client Log
    print("  Writing to Client Log...")
    log_entry_id = await _write_client_log(
        notion._client, client_log_db_id, client_name, parsed, meeting_date_str, page_id,
    )
    print(f"  ✓ Client Log entry: {log_entry_id}")

    # Create ClickUp tasks
    created_tasks = []
    if action_items:
        print(f"  Creating {len(action_items)} ClickUp tasks...")
        try:
            created_tasks = await _create_clickup_tasks(action_items, client_name, cfg)
            print(f"  ✓ {len(created_tasks)} tasks created")
        except Exception as e:
            print(f"  ⚠ ClickUp task creation failed: {e}")

    # Populate Business Profile sections with facts from the transcript
    # (Skip for internal RxMedia meetings — they populate RxMedia's own profile.)
    profile_page = cfg.get("business_profile_page_id", "")
    if profile_page and transcript and not is_internal:
        try:
            from src.integrations.business_profile import populate_from_meeting
            bp_result = await populate_from_meeting(
                notion, profile_page, transcript,
                meeting_date=meeting_date_str,
                meeting_type=meeting_type,
            )
            if bp_result.get("total_facts", 0) > 0:
                print(f"  ✓ Business Profile: +{bp_result['total_facts']} facts across {bp_result['sections_updated']} sections")
        except Exception as e:
            print(f"  ⚠ Business Profile population failed: {e}")

    # Write flags to Flags DB (risks, value-adds, out-of-scope requests)
    flag_dicts = _parsed_to_flags(parsed, meeting_date_str, transcript_page_id=page_id)
    if flag_dicts and FLAGS_DB_ID:
        try:
            created_flags = await write_flags_to_db(
                notion, FLAGS_DB_ID, client_name, client_key, flag_dicts, source="Meeting",
            )
            print(f"  ✓ {created_flags} flags → Flags DB (skipped {len(flag_dicts) - created_flags} dupes)")
        except Exception as e:
            print(f"  ⚠ Flags DB write failed: {e}")
    elif flag_dicts:
        print(f"  ⚠ NOTION_FLAGS_DB_ID not set — skipping {len(flag_dicts)} flag writes")

    # Auto-close prior flags that this meeting resolves
    # Same pattern as email monitor: re-evaluate open flags whenever new data arrives.
    # Skipped for internal meetings — they don't carry client resolution signals.
    if FLAGS_DB_ID and not is_internal:
        try:
            from src.services.email_enrichment import load_open_flags, auto_close_resolved_flags
            open_flags = await load_open_flags(notion, FLAGS_DB_ID, client_key)
            if open_flags:
                to_close = await _evaluate_meeting_flags(parsed, open_flags, meeting_date_str)
                if to_close:
                    closed = await auto_close_resolved_flags(notion, FLAGS_DB_ID, to_close)
                    print(f"  ✓ Auto-closed {closed} flag(s) resolved by this meeting")
                else:
                    print(f"  ✓ {len(open_flags)} open flag(s) reviewed — none resolved by this meeting")
        except Exception as e:
            print(f"  ⚠ Meeting flag auto-close failed: {e}")

    # Draft follow-up email (skip for internal meetings)
    email = {}
    if not is_internal:
        print("  Drafting follow-up email...")
        email = await _draft_follow_up_email(parsed, client_name, meeting_date_str)

        # Pull calendar invitees for this meeting — authoritative source for
        # who was actually on the call (transcript body rarely has emails).
        calendar_emails: list[str] = []
        if meeting_time is not None:
            try:
                from src.integrations.google_calendar import (
                    find_event_near_time, extract_attendee_emails as _cal_emails,
                )
                event = await find_event_near_time(client_name, meeting_time)
                if event:
                    calendar_emails = _cal_emails(event)
                    print(f"  ✓ Calendar event matched: {len(calendar_emails)} invitee(s)")
                else:
                    print(f"  ⚠ No calendar event matched for {client_name}")
            except Exception as e:
                print(f"  ⚠ Calendar lookup failed: {e}")

        # Determine recipients — calendar invitees first, then transcript-extracted
        # emails, then the configured primary contact as last resort.
        combined_emails = list(calendar_emails) + list(parsed.get("attendee_emails", []) or [])
        to_addr, cc_csv = _determine_recipients(
            attendees_text or "",
            combined_emails,
            cfg,
        )
        email["to"] = to_addr
        email["cc"] = cc_csv

        # Auto-create Gmail draft
        html_body = email.get("html_body", email.get("body", ""))
        if email.get("to") and html_body:
            try:
                draft_result = await create_gmail_draft(
                    to=email["to"],
                    subject=email.get("subject", f"{client_name} - Meeting Recap"),
                    html_body=html_body,
                    cc=email.get("cc", ""),
                )
                if draft_result.get("status") == "draft_created":
                    print(f"  ✓ Gmail draft created (check Drafts folder)")
                else:
                    print(f"  ⚠ Gmail draft failed: {draft_result.get('error', '')}")
            except Exception as e:
                print(f"  ⚠ Gmail draft failed: {e}")
    else:
        print("  Internal meeting — skipping email draft")
    print(f"  ✓ Subject: {email.get('subject', '')}")
    print(f"  ✓ To: {email.get('to', '')}")
    if email.get("cc"):
        print(f"  ✓ CC: {email.get('cc', '')}")

    # Mark as processed
    await notion._client.request(
        path=f"pages/{page_id}", method="PATCH",
        body={"properties": {
            "Processed": {"checkbox": True},
            "Processed Date": {"date": {"start": date.today().isoformat()}},
        }},
    )
    print("  ✓ Marked Processed")

    # Update Last Contact on Clients DB
    if not is_internal:
        from src.services.email_enrichment import update_last_contact
        contact_date = meeting_date_str or date.today().isoformat()
        await update_last_contact(notion, client_name, contact_date)
        print(f"  ✓ Last Contact → {contact_date}")

    # Build Slack summary
    label = "Internal Meeting" if is_internal else "Meeting Processed"
    slack_parts = [
        f"📋 *{client_name} — {label}*",
        f"{meeting_type}, {len(attendees)} attendees",
        "",
        f"Summary: {parsed.get('summary', '')[:300]}",
        "",
    ]
    if action_items:
        slack_parts.append(f"✅ {len(created_tasks)}/{len(action_items)} ClickUp tasks created")
    if parsed.get("risk_flags"):
        flags = ", ".join(r.get("flag", "")[:50] for r in parsed["risk_flags"])
        slack_parts.append(f"⚠️ Risk flags: {flags}")
    if parsed.get("value_add_opportunities"):
        slack_parts.append(f"💡 {len(parsed['value_add_opportunities'])} value-add opportunities")

    if email:
        slack_parts.extend([
            "",
            f"📧 Follow-up email draft ready:",
            f"To: {email.get('to', '')}",
            f"Subject: {email.get('subject', '')}",
            f"```{email.get('body', '')[:500]}```",
            "",
            "Reply in thread: `send` to send the email, or `edit: <instructions>` to revise.",
        ])

    slack_msg = "\n".join(slack_parts)
    client_channel = cfg.get("slack_channel", "") or SLACK_CHANNEL
    await _post_to_slack(slack_msg, channel=client_channel)
    print("  ✓ Posted to Slack")

    return {
        "status": "processed",
        "client": client_name,
        "meeting_type": meeting_type,
        "action_items": len(action_items),
        "tasks_created": len(created_tasks),
        "email": email,
        "log_entry_id": log_entry_id,
    }


async def run(client_filter: str = "", force: bool = False) -> None:
    notion = NotionClient(api_key=settings.notion_api_key)
    now = datetime.now()

    print(f"\n{'='*50}")
    print(f"  Meeting Processor — {now.strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*50}")

    # Query unprocessed transcripts
    query_body: dict = {
        "page_size": 10,
        "filter": {"property": "Processed", "checkbox": {"equals": False}},
        "sorts": [{"timestamp": "created_time", "direction": "descending"}],
    }
    rows = await notion._client.request(
        path=f"databases/{MEETING_TRANSCRIPTS_DB}/query",
        method="POST",
        body=query_body,
    )
    results = rows.get("results", [])
    print(f"\n  Unprocessed transcripts: {len(results)}")

    if not results:
        print("  Nothing to process. Done.")
        return

    processed_count = 0
    # Stability threshold: transcripts whose last edit is within this window
    # are still being written by Notion AI — wait for them to stabilize.
    STABILITY_MIN = 5
    now_utc = datetime.now(timezone.utc)

    for row in results:
        props = row.get("properties", {})
        title = "".join(
            p.get("text", {}).get("content", "")
            for p in props.get("Title", {}).get("title", [])
        )

        # Skip transcripts Notion AI is still writing. Picking these up early
        # produced partial-meeting drafts (e.g. Summit 2026-04-21 at 9:05).
        # --force bypasses for ad-hoc runs (e.g. reprocessing a flipped transcript
        # whose last_edited_time reflects our PATCH, not real Notion AI activity).
        last_edited_str = row.get("last_edited_time", "")
        if not force and last_edited_str:
            try:
                last_edited = datetime.fromisoformat(last_edited_str.replace("Z", "+00:00"))
                age_min = (now_utc - last_edited).total_seconds() / 60
                if age_min < STABILITY_MIN:
                    print(f"\n  ⏳ Skipping (transcript still updating, last edit "
                          f"{int(age_min)} min ago): {title[:60]}")
                    continue
            except ValueError:
                pass
        client_field = "".join(
            p.get("text", {}).get("content", "")
            for p in props.get("Client", {}).get("rich_text", [])
        )
        date_obj = props.get("Meeting Date", {}).get("date")
        meeting_date = date_obj.get("start", "") if date_obj else ""

        # created_time is close to when Notion AI joined the meeting — used for
        # looking up the calendar event in _process_one.
        created_str = row.get("created_time", "")
        created_dt: datetime | None = None
        if created_str:
            try:
                created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            except ValueError:
                created_dt = None

        # Smart routing: client, internal, or skip
        # Read attendees from page for detection (if available)
        attendees_text = ""
        try:
            page_data = await notion._client.request(path=f"pages/{row['id']}", method="GET")
            page_props = page_data.get("properties", {})
            for field_name in ("Attendees", "attendees"):
                if field_name in page_props:
                    attendees_text = "".join(
                        p.get("text", {}).get("content", "")
                        for p in page_props[field_name].get("rich_text", [])
                    )
                    break
        except Exception:
            pass

        meeting_route = _detect_meeting_type(client_field, title, attendees_text)

        # Optional client filter
        if client_filter:
            matched = _match_client(client_field, attendees_text)
            if matched != client_filter:
                continue

        if meeting_route == "skip":
            print(f"\n  Skipped (no client match, not internal): {title}")
            # Mark as processed so we don't re-check every tick
            await notion._client.request(
                path=f"pages/{row['id']}", method="PATCH",
                body={"properties": {"Processed": {"checkbox": True}}},
            )
            continue

        if meeting_route == "internal":
            # Route to RxMedia internal log — lighter processing (no email draft)
            rxmedia_cfg = CLIENTS.get("rxmedia", {})
            rxmedia_log_db = rxmedia_cfg.get("client_log_db_id", "")
            if not rxmedia_log_db:
                print(f"\n  Skipped (internal but no RxMedia Client Log DB): {title}")
                continue
            result = await _process_one(
                notion, row["id"], title, "RxMedia", meeting_date,
                is_internal=True, attendees_text=attendees_text,
                meeting_time=created_dt,
            )
        else:
            result = await _process_one(
                notion, row["id"], title, client_field, meeting_date,
                attendees_text=attendees_text,
                meeting_time=created_dt,
            )

        if result["status"] == "processed":
            processed_count += 1
        else:
            print(f"\n  Skipped: {title} — {result.get('reason', '')}")

    print(f"\n{'='*50}")
    print(f"  Done. {processed_count}/{len(results)} transcripts processed.")
    print(f"{'='*50}")


async def _alert_failure(error: str) -> None:
    if not SLACK_BOT_TOKEN or not SLACK_CHANNEL:
        return
    try:
        async with httpx.AsyncClient() as http:
            await http.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
                json={"channel": SLACK_CHANNEL, "text": f"🚨 *Meeting Processor Failed*\n```{error[:500]}```"},
                timeout=10.0,
            )
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Process meeting transcripts")
    parser.add_argument("--client", default="", help="Only process for this client key")
    parser.add_argument("--force", action="store_true",
                        help="Bypass the stability gate (useful for re-processing a transcript "
                             "whose last_edited_time reflects a manual flip, not Notion AI activity)")
    args = parser.parse_args()
    try:
        asyncio.run(run(client_filter=args.client, force=args.force))
    except Exception as e:
        import traceback
        error = traceback.format_exc()
        print(f"\n🚨 Meeting Processor crashed:\n{error}")
        asyncio.run(_alert_failure(error))


if __name__ == "__main__":
    main()
