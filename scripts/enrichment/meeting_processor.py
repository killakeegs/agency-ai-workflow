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
from datetime import date, datetime
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
from rex.tools.email_tools import send_email

MEETING_TRANSCRIPTS_DB = os.environ.get(
    "NOTION_MEETING_TRANSCRIPTS_DB_ID",
    "343f7f45-333e-81cf-8f2f-ebfd074fc9fd",
).strip()

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "").strip()
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL_INTERNAL", "").strip()


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

def _match_client(client_field: str) -> str | None:
    """Match the Client field from Meeting Transcripts to a client key."""
    if not client_field:
        return None
    client_lower = client_field.strip().lower()
    for key, cfg in CLIENTS.items():
        name = cfg.get("name", "").lower()
        if name and (name in client_lower or client_lower in name):
            return key
        if key.replace("_", " ") in client_lower:
            return key
    return None


# ── Slack ──────────────────────────────────────────────────────────────────────

async def _post_to_slack(text: str) -> None:
    if not SLACK_BOT_TOKEN or not SLACK_CHANNEL:
        return
    try:
        async with httpx.AsyncClient() as http:
            await http.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
                json={"channel": SLACK_CHANNEL, "text": text},
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
) -> dict:
    """Process a single meeting transcript. Returns summary dict."""

    # Match client
    client_key = _match_client(client_field)
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

    # Draft follow-up email
    print("  Drafting follow-up email...")
    email = await _draft_follow_up_email(parsed, client_name, meeting_date_str)

    # Ensure To field is populated from client config
    if not email.get("to"):
        email["to"] = cfg.get("primary_contact_email") or cfg.get("email", "")
    if not email.get("cc"):
        email["cc"] = "keegan@rxmedia.io"
    print(f"  ✓ Subject: {email.get('subject', '')}")
    print(f"  ✓ To: {email.get('to', '')}")

    # Mark as processed
    await notion._client.request(
        path=f"pages/{page_id}", method="PATCH",
        body={"properties": {
            "Processed": {"checkbox": True},
            "Processed Date": {"date": {"start": date.today().isoformat()}},
        }},
    )
    print("  ✓ Marked Processed")

    # Build Slack summary
    slack_parts = [
        f"📋 *{client_name} — Meeting Processed*",
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
        f"```{email.get('body', '')[:500]}```",
        "",
        "Reply in thread: `send` to send the email, or `edit: <instructions>` to revise.",
    ])

    slack_msg = "\n".join(slack_parts)
    await _post_to_slack(slack_msg)
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


async def run(client_filter: str = "") -> None:
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
    for row in results:
        props = row.get("properties", {})
        title = "".join(
            p.get("text", {}).get("content", "")
            for p in props.get("Title", {}).get("title", [])
        )
        client_field = "".join(
            p.get("text", {}).get("content", "")
            for p in props.get("Client", {}).get("rich_text", [])
        )
        date_obj = props.get("Meeting Date", {}).get("date")
        meeting_date = date_obj.get("start", "") if date_obj else ""

        # Optional client filter
        if client_filter:
            matched = _match_client(client_field)
            if matched != client_filter:
                continue

        result = await _process_one(notion, row["id"], title, client_field, meeting_date)

        if result["status"] == "processed":
            processed_count += 1
        else:
            print(f"\n  Skipped: {title} — {result.get('reason', '')}")

    print(f"\n{'='*50}")
    print(f"  Done. {processed_count}/{len(results)} transcripts processed.")
    print(f"{'='*50}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Process meeting transcripts")
    parser.add_argument("--client", default="", help="Only process for this client key")
    args = parser.parse_args()
    asyncio.run(run(client_filter=args.client))


if __name__ == "__main__":
    main()
