#!/usr/bin/env python3
"""One-off: run a Gemini-captured transcript through the meeting pipeline.

Mirrors meeting_processor._process_one but sources the transcript from a
local text file (exported from a Gemini "Notes by Gemini" Drive doc) instead
of the Notion AI transcription block, since Notion AI transcription has been
flaking (transcription_paused state) and silently eating meetings.

Used to recover the 2026-04-22 Parkwood Clinic meeting after Notion AI paused.
Kept in-tree because the same script will be needed again while we build the
proper Gemini-first Drive polling architecture.

Usage:
    python3 scripts/enrichment/process_gemini_transcript.py \\
        --client parkwood_clinic \\
        --transcript /tmp/parkwood_gemini_2026-04-22.txt \\
        --meeting-date 2026-04-22 \\
        --title "Parkwood Clinic - Monthly Synch" \\
        --notion-page-id 34af7f45-333e-80f4-846b-dfcf6bbf7d04 \\
        --attendees-text "keegan@rxmedia.io content@rxmedia.io ashleyrose@parkwoodclinic.com magdamarks@parkwoodclinic.com"
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent.parent / ".env")

from config.clients import CLIENTS
from src.config import settings
from src.integrations.notion import NotionClient
from rex.tools.meeting_tools import (
    _parse_transcript,
    _write_client_log,
    _create_clickup_tasks,
    _draft_follow_up_email,
)
from rex.tools.email_tools import create_gmail_draft
from scripts.enrichment.meeting_processor import (
    _parsed_to_flags,
    _determine_recipients,
    _post_to_slack,
    _evaluate_meeting_flags,
    FLAGS_DB_ID,
)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True, help="Client key (e.g. parkwood_clinic)")
    ap.add_argument("--transcript", required=True, help="Path to transcript text file")
    ap.add_argument("--meeting-date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--title", required=True, help="Meeting title")
    ap.add_argument("--notion-page-id", default="",
                    help="Existing Notion Meeting Transcripts page id to patch")
    ap.add_argument("--attendees-text", default="",
                    help="Space-separated emails that were on the call")
    args = ap.parse_args()

    cfg = CLIENTS.get(args.client)
    if not cfg:
        print(f"Unknown client: {args.client}")
        return 2
    client_name = cfg.get("name", args.client)
    client_log_db_id = cfg.get("client_log_db_id", "")
    if not client_log_db_id:
        print(f"No Client Log DB configured for {args.client}")
        return 2

    transcript = Path(args.transcript).read_text()
    if len(transcript.strip()) < 100:
        print(f"Transcript too short: {len(transcript)} chars")
        return 2

    services = cfg.get("services", {})
    active = [k for k, v in services.items() if v is True] if isinstance(services, dict) else services

    notion = NotionClient(api_key=settings.notion_api_key)

    print(f"Processing: {args.title}")
    print(f"Client: {client_name} ({args.client})")
    print(f"Transcript: {len(transcript):,} chars")

    # 1. Parse with Claude
    print("Parsing with Claude...")
    parsed = await _parse_transcript(transcript, client_name, active, args.meeting_date)
    meeting_type = parsed.get("meeting_type", "Check-in")
    attendees = parsed.get("attendees", [])
    action_items = parsed.get("action_items", [])
    print(f"  Type: {meeting_type} | Attendees: {len(attendees)} | Actions: {len(action_items)}")

    # 2. Client Log
    print("Writing to Client Log...")
    log_entry_id = await _write_client_log(
        notion._client, client_log_db_id, client_name, parsed,
        args.meeting_date, args.notion_page_id,
    )
    print(f"  ✓ Client Log entry: {log_entry_id}")

    # 3. ClickUp tasks
    created_tasks = []
    if action_items:
        print(f"Creating {len(action_items)} ClickUp tasks...")
        try:
            created_tasks = await _create_clickup_tasks(action_items, client_name, cfg)
            print(f"  ✓ {len(created_tasks)} tasks created")
        except Exception as e:
            print(f"  ⚠ ClickUp task creation failed: {e}")

    # 4. Business Profile
    profile_page = cfg.get("business_profile_page_id", "")
    if profile_page:
        try:
            from src.integrations.business_profile import populate_from_meeting
            bp_result = await populate_from_meeting(
                notion, profile_page, transcript,
                meeting_date=args.meeting_date, meeting_type=meeting_type,
            )
            if bp_result.get("total_facts", 0) > 0:
                print(f"  ✓ Business Profile: +{bp_result['total_facts']} facts "
                      f"across {bp_result['sections_updated']} sections")
        except Exception as e:
            print(f"  ⚠ Business Profile population failed: {e}")

    # 5. Flags
    flag_dicts = _parsed_to_flags(parsed, args.meeting_date, transcript_page_id=args.notion_page_id)
    if flag_dicts and FLAGS_DB_ID:
        try:
            from src.services.email_enrichment import write_flags_to_db
            created_flags = len(await write_flags_to_db(
                notion, FLAGS_DB_ID, client_name, args.client, flag_dicts, source="Meeting",
            ))
            print(f"  ✓ {created_flags} flags → Flags DB (skipped {len(flag_dicts) - created_flags} dupes)")
        except Exception as e:
            print(f"  ⚠ Flags DB write failed: {e}")

    # 6. Auto-close prior flags resolved by this meeting
    if FLAGS_DB_ID:
        try:
            from src.services.email_enrichment import load_open_flags, auto_close_resolved_flags
            open_flags = await load_open_flags(notion, FLAGS_DB_ID, args.client)
            if open_flags:
                to_close = await _evaluate_meeting_flags(parsed, open_flags, args.meeting_date)
                if to_close:
                    closed = await auto_close_resolved_flags(notion, FLAGS_DB_ID, to_close)
                    print(f"  ✓ Auto-closed {closed} flag(s) resolved by this meeting")
                else:
                    print(f"  ✓ {len(open_flags)} open flag(s) reviewed — none resolved")
        except Exception as e:
            print(f"  ⚠ Meeting flag auto-close failed: {e}")

    # 7. Draft follow-up email
    print("Drafting follow-up email...")
    email = await _draft_follow_up_email(parsed, client_name, args.meeting_date)
    combined = list(parsed.get("attendee_emails", []) or [])
    to_addr, cc_csv = _determine_recipients(args.attendees_text, combined, cfg)
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
                print(f"  ✓ Gmail draft created")
            else:
                print(f"  ⚠ Gmail draft failed: {draft_result.get('error', '')}")
        except Exception as e:
            print(f"  ⚠ Gmail draft failed: {e}")

    print(f"  ✓ Subject: {email.get('subject', '')}")
    print(f"  ✓ To: {email.get('to', '')}")
    if email.get("cc"):
        print(f"  ✓ CC: {email.get('cc', '')}")

    # 8. Patch Notion transcript page with proper metadata + Processed Date
    if args.notion_page_id:
        try:
            await notion._client.request(
                path=f"pages/{args.notion_page_id}", method="PATCH",
                body={"properties": {
                    "Processed":      {"checkbox": True},
                    "Processed Date": {"date": {"start": date.today().isoformat()}},
                    "Client":         {"rich_text": [{"type": "text", "text": {"content": client_name}}]},
                    "Meeting Date":   {"date": {"start": args.meeting_date}},
                }},
            )
            print("  ✓ Notion transcript page patched (Client, Meeting Date, Processed Date)")
        except Exception as e:
            print(f"  ⚠ Notion patch failed: {e}")

    # 9. Last Contact
    try:
        from src.services.email_enrichment import update_last_contact
        await update_last_contact(notion, client_name, args.meeting_date)
        print(f"  ✓ Last Contact → {args.meeting_date}")
    except Exception as e:
        print(f"  ⚠ Last Contact update failed: {e}")

    # 10. Slack summary
    slack_parts = [
        f"📋 *{client_name} — Meeting Processed* (Gemini backup)",
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
    slack_parts.extend([
        "",
        f"📧 Follow-up email draft ready:",
        f"To: {email.get('to', '')}",
        f"Subject: {email.get('subject', '')}",
    ])

    client_channel = cfg.get("slack_channel", "") or os.environ.get("SLACK_CHANNEL_INTERNAL", "")
    await _post_to_slack("\n".join(slack_parts), channel=client_channel)
    print("  ✓ Posted to Slack")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
