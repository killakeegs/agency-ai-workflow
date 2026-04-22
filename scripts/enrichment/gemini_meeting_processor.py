#!/usr/bin/env python3
"""
gemini_meeting_processor.py — Gemini-first meeting transcript processor.

Replaces the Notion AI–sourced meeting_processor.py. Notion AI transcription
proved unreliable (~22% of recent meetings landed in `transcription_paused`
state with no audio captured and silent `Processed=True`); Gemini's built-in
Google Meet note-taker captures every meeting reliably into a Drive folder
with a predictable filename pattern.

This processor polls that Drive folder, ingests completed Gemini docs, and
runs the same downstream pipeline (Client Log + ClickUp + Business Profile
+ Flags + Gmail draft + Slack) the old processor ran. Deduplication is by
`Gemini Doc ID` stamped on the Meeting Transcripts DB row.

Notion AI's entries still get patched when they exist (so the team's view
of the Meeting Transcripts DB stays coherent), but the transcription block
itself is never read.

Usage:
    python3 scripts/enrichment/gemini_meeting_processor.py                 # poll + process
    python3 scripts/enrichment/gemini_meeting_processor.py --dry-run       # show plan only
    python3 scripts/enrichment/gemini_meeting_processor.py --hours 48      # look back 48h
    python3 scripts/enrichment/gemini_meeting_processor.py --client CLIENT # only this client

Railway cron: replaces the old meeting_processor.py start command.
"""
from __future__ import annotations

import argparse
import asyncio
import os
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
from src.integrations import google_drive
from src.services.gemini_meeting import GeminiMeeting, build_meeting

# Reuse the full downstream pipeline from the legacy processor + rex tools.
# These modules are our shared meeting-pipeline runtime — the Gemini processor
# only owns the source (Drive) and the matching/dedup layer.
from rex.tools.meeting_tools import (
    _parse_transcript,
    _write_client_log,
    _create_clickup_tasks,
    _draft_follow_up_email,
)
from rex.tools.email_tools import create_gmail_draft
from scripts.enrichment.meeting_processor import (
    _match_client,
    _parsed_to_flags,
    _determine_recipients,
    _post_to_slack,
    _evaluate_meeting_flags,
    RXMEDIA_DOMAINS,
    FLAGS_DB_ID,
    MEETING_TRANSCRIPTS_DB,
)


GEMINI_FOLDER_ID = os.environ.get(
    "GOOGLE_MEET_RECORDINGS_FOLDER_ID",
    "1PQVipkeFakZuTrgxcKo2cHdg6RV0X3io",
).strip()


# ── Meeting Transcripts DB helpers ────────────────────────────────────────────

async def _ensure_gemini_doc_id_field(notion: NotionClient) -> None:
    """Add `Gemini Doc ID` rich_text field to the Meeting Transcripts DB if missing.

    Self-heal so the processor can be deployed without a manual Notion schema change.
    """
    try:
        db = await notion._client.request(path=f"databases/{MEETING_TRANSCRIPTS_DB}", method="GET")
        if "Gemini Doc ID" not in (db.get("properties") or {}):
            await notion._client.request(
                path=f"databases/{MEETING_TRANSCRIPTS_DB}", method="PATCH",
                body={"properties": {"Gemini Doc ID": {"rich_text": {}}}},
            )
            print("  ✓ Added `Gemini Doc ID` field to Meeting Transcripts DB")
    except Exception as e:
        print(f"  ⚠ Could not ensure Gemini Doc ID field: {e}")


async def _find_transcript_row(notion: NotionClient, meeting: GeminiMeeting) -> dict | None:
    """Find an existing Meeting Transcripts row that matches this Gemini doc.

    Priority:
      1. Existing row with matching `Gemini Doc ID` (previously processed by this script)
      2. Notion AI row with matching title + same day (so we unify the team view)
    """
    # 1. Exact Gemini Doc ID match
    r = await notion._client.request(
        path=f"databases/{MEETING_TRANSCRIPTS_DB}/query",
        method="POST",
        body={
            "filter": {"property": "Gemini Doc ID",
                       "rich_text": {"equals": meeting.doc_id}},
            "page_size": 1,
        },
    )
    if r.get("results"):
        return r["results"][0]

    # 2. Notion AI row whose title starts with the same title and was created that day
    day_start = meeting.meeting_start_utc - timedelta(hours=6)
    day_end = meeting.meeting_start_utc + timedelta(hours=18)
    r = await notion._client.request(
        path=f"databases/{MEETING_TRANSCRIPTS_DB}/query",
        method="POST",
        body={
            "filter": {"and": [
                {"property": "Title", "title": {"contains": meeting.title[:40]}},
                {"timestamp": "created_time",
                 "created_time": {"on_or_after": day_start.isoformat()}},
                {"timestamp": "created_time",
                 "created_time": {"on_or_before": day_end.isoformat()}},
            ]},
            "page_size": 3,
        },
    )
    results = r.get("results") or []
    return results[0] if results else None


async def _upsert_transcript_row(
    notion: NotionClient,
    meeting: GeminiMeeting,
    client_name: str,
) -> str:
    """Patch an existing Notion AI row or create a new one. Returns the page_id."""
    row = await _find_transcript_row(notion, meeting)
    today = date.today().isoformat()
    props = {
        "Processed":      {"checkbox": True},
        "Processed Date": {"date": {"start": today}},
        "Meeting Date":   {"date": {"start": meeting.meeting_date}},
        "Client":         {"rich_text": [{"type": "text", "text": {"content": client_name}}]},
        "Gemini Doc ID":  {"rich_text": [{"type": "text", "text": {"content": meeting.doc_id}}]},
    }
    if row:
        await notion._client.request(
            path=f"pages/{row['id']}", method="PATCH",
            body={"properties": props},
        )
        return row["id"]

    # No Notion AI row existed — create one ourselves so the team's DB view stays unified.
    props["Title"] = {"title": [{"type": "text",
                                 "text": {"content": f"{meeting.title} [Gemini]"}}]}
    created = await notion._client.request(
        path="pages", method="POST",
        body={"parent": {"database_id": MEETING_TRANSCRIPTS_DB}, "properties": props},
    )
    return created["id"]


async def _already_processed(notion: NotionClient, meeting: GeminiMeeting) -> bool:
    """True if this meeting has already been fully processed.

    Matches in priority order:
      1. A row with this exact Gemini Doc ID and Processed=True (our own dedup)
      2. A Notion AI row with matching title + same day and Processed=True
         (the old processor or the recovery script has already run) — in which
         case we stamp the Gemini Doc ID onto it so subsequent polls short-circuit.

    The second case protects against duplicate Client Log / ClickUp / email work
    during the cutover from the old Notion AI processor to this Gemini-first one.
    """
    row = await _find_transcript_row(notion, meeting)
    if not row:
        return False
    props = row.get("properties", {})
    processed = props.get("Processed", {}).get("checkbox", False)
    processed_date = (props.get("Processed Date", {}) or {}).get("date")
    # The legacy Notion AI processor's silent-skip path set Processed=True but
    # left Processed Date null (never ran the pipeline). Require BOTH to count
    # this meeting as truly done — otherwise we'd orphan every silently-skipped
    # transcript (e.g. The Manor Apr 15, Parkwood Apr 22 before recovery).
    if not processed or not processed_date:
        return False

    # Stamp Gemini Doc ID on rows that don't have it yet so future polls hit the
    # O(1) dedup path instead of the title+date query.
    has_gemini_id = bool(
        (row.get("properties", {}).get("Gemini Doc ID", {}) or {}).get("rich_text")
    )
    if not has_gemini_id:
        try:
            await notion._client.request(
                path=f"pages/{row['id']}", method="PATCH",
                body={"properties": {"Gemini Doc ID": {"rich_text": [
                    {"type": "text", "text": {"content": meeting.doc_id}}]}}},
            )
        except Exception as e:
            print(f"  ⚠ Could not stamp Gemini Doc ID on existing row: {e}")
    return True


# ── Routing ────────────────────────────────────────────────────────────────────

def _route_meeting(emails: list[str]) -> tuple[str, str | None]:
    """Return (route, client_key). route ∈ {client, internal, skip}.

    Rules:
      - All attendees @rxmedia.io → internal
      - Any external attendee matches a client domain → client
      - Otherwise → skip
    """
    emails_lc = [e.lower() for e in emails]
    external = [e for e in emails_lc
                if not any(e.endswith(f"@{d}") for d in RXMEDIA_DOMAINS)]
    if not external:
        return ("internal", "rxmedia")
    client_key = _match_client("", " ".join(external))
    if client_key:
        return ("client", client_key)
    return ("skip", None)


# ── Processing ─────────────────────────────────────────────────────────────────

async def _process_gemini_meeting(
    notion: NotionClient,
    meeting: GeminiMeeting,
    dry_run: bool = False,
) -> dict:
    route, client_key = _route_meeting(meeting.attendee_emails)

    if route == "skip":
        print(f"\n  Skipped (no client match, not internal): {meeting.title}")
        if not dry_run:
            # Still create a row so we don't re-consider this doc on every tick.
            await _upsert_transcript_row(notion, meeting, client_name="(unmatched)")
        return {"status": "skipped", "reason": "no client match"}

    is_internal = (route == "internal")
    cfg = CLIENTS[client_key]
    client_name = cfg.get("name", client_key)
    client_log_db_id = cfg.get("client_log_db_id", "")
    if not client_log_db_id:
        print(f"\n  Skipped (no Client Log DB for {client_name}): {meeting.title}")
        return {"status": "skipped", "reason": f"no client_log_db_id for {client_key}"}

    print(f"\n  Processing: {meeting.title}")
    print(f"  Route: {route} | Client: {client_name} ({client_key})")
    print(f"  Gemini doc: {meeting.doc_id} ({len(meeting.body):,} chars)")

    if dry_run:
        print("  (dry-run — no writes)")
        return {"status": "dry-run", "client": client_name}

    services = cfg.get("services", {})
    active = [k for k, v in services.items() if v is True] if isinstance(services, dict) else services

    # 1. Parse
    print("  Parsing with Claude...")
    parsed = await _parse_transcript(meeting.body, client_name, active, meeting.meeting_date)
    meeting_type = parsed.get("meeting_type", "Check-in")
    attendees = parsed.get("attendees", [])
    action_items = parsed.get("action_items", [])
    print(f"  Type: {meeting_type} | Attendees: {len(attendees)} | Actions: {len(action_items)}")

    # 2. Upsert the Meeting Transcripts row FIRST so its page_id can back the
    # flags' source_thread_id (tight same-meeting dedup).
    mt_page_id = await _upsert_transcript_row(notion, meeting, client_name)

    # 3. Client Log
    log_entry_id = await _write_client_log(
        notion._client, client_log_db_id, client_name, parsed,
        meeting.meeting_date, mt_page_id,
    )
    print(f"  ✓ Client Log entry: {log_entry_id}")

    # 4. ClickUp tasks
    created_tasks = []
    if action_items:
        try:
            created_tasks = await _create_clickup_tasks(action_items, client_name, cfg)
            print(f"  ✓ {len(created_tasks)}/{len(action_items)} ClickUp tasks created")
        except Exception as e:
            print(f"  ⚠ ClickUp task creation failed: {e}")

    # 5. Business Profile (skip internal)
    profile_page = cfg.get("business_profile_page_id", "")
    if profile_page and not is_internal:
        try:
            from src.integrations.business_profile import populate_from_meeting
            bp_result = await populate_from_meeting(
                notion, profile_page, meeting.body,
                meeting_date=meeting.meeting_date, meeting_type=meeting_type,
            )
            if bp_result.get("total_facts", 0) > 0:
                print(f"  ✓ Business Profile: +{bp_result['total_facts']} facts "
                      f"across {bp_result['sections_updated']} sections")
        except Exception as e:
            print(f"  ⚠ Business Profile population failed: {e}")

    # 6. Flags
    flag_dicts = _parsed_to_flags(parsed, meeting.meeting_date, transcript_page_id=mt_page_id)
    if flag_dicts and FLAGS_DB_ID:
        try:
            from src.services.email_enrichment import write_flags_to_db
            created_flags = len(await write_flags_to_db(
                notion, FLAGS_DB_ID, client_name, client_key, flag_dicts, source="Meeting",
            ))
            print(f"  ✓ {created_flags} flags → Flags DB (skipped {len(flag_dicts) - created_flags} dupes)")
        except Exception as e:
            print(f"  ⚠ Flags DB write failed: {e}")

    # 7. Auto-close flags resolved by this meeting
    if FLAGS_DB_ID and not is_internal:
        try:
            from src.services.email_enrichment import load_open_flags, auto_close_resolved_flags
            open_flags = await load_open_flags(notion, FLAGS_DB_ID, client_key)
            if open_flags:
                to_close = await _evaluate_meeting_flags(parsed, open_flags, meeting.meeting_date)
                if to_close:
                    closed = await auto_close_resolved_flags(notion, FLAGS_DB_ID, to_close)
                    print(f"  ✓ Auto-closed {closed} flag(s) resolved by this meeting")
        except Exception as e:
            print(f"  ⚠ Meeting flag auto-close failed: {e}")

    # 8. Follow-up email (skip internal)
    email: dict = {}
    if not is_internal:
        email = await _draft_follow_up_email(parsed, client_name, meeting.meeting_date)
        # Attendees from the Gemini doc ARE the authoritative list — the whole
        # reason we're on Gemini is that Notion AI / transcript-extracted emails
        # were lossy. Skip the calendar lookup; pass doc attendees straight in.
        attendees_text = " ".join(meeting.attendee_emails)
        combined = list(parsed.get("attendee_emails", []) or []) + list(meeting.attendee_emails)
        to_addr, cc_csv = _determine_recipients(attendees_text, combined, cfg)
        email["to"] = to_addr
        email["cc"] = cc_csv

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
                    print(f"  ✓ Gmail draft created — To: {email['to']}")
                else:
                    print(f"  ⚠ Gmail draft failed: {draft_result.get('error', '')}")
            except Exception as e:
                print(f"  ⚠ Gmail draft failed: {e}")

    # 9. Last Contact on Clients DB
    if not is_internal:
        try:
            from src.services.email_enrichment import update_last_contact
            await update_last_contact(notion, client_name, meeting.meeting_date)
        except Exception as e:
            print(f"  ⚠ Last Contact update failed: {e}")

    # 10. Slack
    label = "Internal Meeting" if is_internal else "Meeting Processed"
    slack_parts = [
        f"📋 *{client_name} — {label}* (Gemini)",
        f"{meeting_type}, {len(attendees)} attendees",
        "",
        f"Summary: {parsed.get('summary', '')[:300]}",
    ]
    if action_items:
        slack_parts.append(f"✅ {len(created_tasks)}/{len(action_items)} ClickUp tasks created")
    if parsed.get("risk_flags"):
        flags = ", ".join(r.get("flag", "")[:50] for r in parsed["risk_flags"])
        slack_parts.append(f"⚠️ Risk flags: {flags}")
    if email.get("to"):
        slack_parts.extend([
            "",
            f"📧 Follow-up email draft ready:",
            f"To: {email.get('to', '')}",
            f"Subject: {email.get('subject', '')}",
        ])
    await _post_to_slack(
        "\n".join(slack_parts),
        channel=cfg.get("slack_channel", "") or os.environ.get("SLACK_CHANNEL_INTERNAL", ""),
    )

    return {
        "status": "processed",
        "client": client_name,
        "meeting_type": meeting_type,
        "action_items": len(action_items),
        "tasks_created": len(created_tasks),
        "log_entry_id": log_entry_id,
        "mt_page_id": mt_page_id,
    }


# ── Main loop ─────────────────────────────────────────────────────────────────

async def run(hours: int, dry_run: bool, client_filter: str | None) -> int:
    if not GEMINI_FOLDER_ID:
        print("⚠ GOOGLE_MEET_RECORDINGS_FOLDER_ID not set and no default — aborting.")
        return 2

    notion = NotionClient(api_key=settings.notion_api_key)
    if not dry_run:
        await _ensure_gemini_doc_id_field(notion)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    print(f"Polling Drive folder {GEMINI_FOLDER_ID} for `Notes by Gemini` docs "
          f"modified after {cutoff.isoformat()}...")

    async with httpx.AsyncClient() as http:
        token = await google_drive.get_access_token(http)
        files = await google_drive.list_files_in_folder(
            http, token, GEMINI_FOLDER_ID,
            name_contains="Notes by Gemini",
            modified_after=cutoff,
            page_size=50,
        )
        print(f"Found {len(files)} candidate doc(s)")

        processed = skipped = waiting = errored = 0
        for f in files:
            doc_id = f["id"]
            name = f.get("name", "")

            try:
                body = await google_drive.fetch_doc_text(http, token, doc_id)
            except Exception as e:
                print(f"  ⚠ Could not fetch {name}: {e}")
                errored += 1
                continue

            meeting = build_meeting(doc_id, name, body)
            if not meeting:
                print(f"  ⚠ Filename didn't match Gemini pattern: {name}")
                errored += 1
                continue

            if not dry_run and await _already_processed(notion, meeting):
                print(f"  ⏭ Already processed: {name}")
                skipped += 1
                continue

            try:
                mtime = datetime.fromisoformat(f["modifiedTime"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                mtime = datetime.now(timezone.utc)

            if not meeting.is_ready_to_process(mtime):
                reason = ("transcription still recording"
                          if not meeting.has_end_marker
                          else "too short")
                print(f"  ⏳ Not ready ({reason}): {name}")
                waiting += 1
                continue

            if len(meeting.body.strip()) < 500:
                print(f"  ⚠ Too short ({len(meeting.body)} chars): {name}")
                errored += 1
                continue

            if client_filter:
                _, key = _route_meeting(meeting.attendee_emails)
                if key != client_filter:
                    continue

            try:
                result = await _process_gemini_meeting(notion, meeting, dry_run=dry_run)
                if result.get("status") == "processed":
                    processed += 1
                elif result.get("status") == "skipped":
                    skipped += 1
                elif result.get("status") == "dry-run":
                    processed += 1
            except Exception as e:
                print(f"  ⚠ Processing failed for {name}: {e}")
                errored += 1

    print(f"\nDone. processed={processed} skipped={skipped} "
          f"waiting={waiting} errored={errored}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24,
                    help="How far back to look for modified Gemini docs (default 24)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the plan but write nothing (no Notion/ClickUp/Gmail/Slack)")
    ap.add_argument("--client", default="",
                    help="Only process meetings that match this client key")
    args = ap.parse_args()
    return asyncio.run(run(args.hours, args.dry_run, args.client or None))


if __name__ == "__main__":
    sys.exit(main())
