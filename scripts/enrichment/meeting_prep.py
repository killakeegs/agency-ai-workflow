#!/usr/bin/env python3
"""
meeting_prep.py — Generate meeting prep docs for today's calendar events.

For each meeting on Keegan's calendar today:
  - Classify the meeting (client recurring / onboarding / sales / internal / unknown)
  - Skip internal meetings
  - Generate a Notion prep doc tailored to the meeting type
  - Output index of {meeting_time, client, prep_doc_url} that morning briefing reads

Runs at 6:50am PST (before morning briefing). Output saved to Notion "Meeting Prep Index"
page which morning_briefing.py reads to build the "Today's Meetings" section.

Usage:
    python3 scripts/enrichment/meeting_prep.py           # Generate for today
    python3 scripts/enrichment/meeting_prep.py --dry     # Preview, don't create docs
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent.parent / ".env")

from config.clients import CLIENTS
from src.config import settings
from src.integrations.notion import NotionClient
from src.integrations import google_calendar as gcal

ROOT_PAGE_ID = os.environ.get("NOTION_WORKSPACE_ROOT_PAGE_ID", "").strip()


# ── Prep doc content generation ────────────────────────────────────────────────

async def _load_recent_client_log(
    notion: NotionClient,
    log_db_id: str,
    limit: int = 5,
) -> list[dict]:
    """Fetch the client's most recent log entries."""
    try:
        rows = await notion._client.request(
            path=f"databases/{log_db_id}/query",
            method="POST",
            body={
                "page_size": limit,
                "sorts": [{"property": "Date", "direction": "descending"}],
            },
        )
    except Exception:
        return []

    entries = []
    for row in rows.get("results", []):
        props = row.get("properties", {})
        title = "".join(p.get("text", {}).get("content", "") for p in props.get("Title", {}).get("title", []))
        date_obj = props.get("Date", {}).get("date")
        date_str = date_obj.get("start", "") if date_obj else ""
        type_sel = props.get("Type", {}).get("select") or {}
        type_name = type_sel.get("name", "")
        summary = "".join(p.get("text", {}).get("content", "") for p in props.get("Summary", {}).get("rich_text", []))
        decisions = "".join(p.get("text", {}).get("content", "") for p in props.get("Key Decisions", {}).get("rich_text", []))
        actions = "".join(p.get("text", {}).get("content", "") for p in props.get("Action Items", {}).get("rich_text", []))
        entries.append({
            "title": title, "date": date_str, "type": type_name,
            "summary": summary, "decisions": decisions, "actions": actions,
        })
    return entries


async def _load_business_profile_flags(notion: NotionClient, profile_id: str) -> list[str]:
    """Pull open flags from the most recent Email Enrichment block."""
    try:
        blocks_resp = await notion._client.request(
            path=f"blocks/{profile_id}/children?page_size=100", method="GET",
        )
    except Exception:
        return []

    blocks = blocks_resp.get("results", [])
    cutoff = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
    flags = []
    in_recent = False
    in_flags = False
    for b in blocks:
        btype = b.get("type", "")
        if btype == "heading_2":
            text = "".join(p.get("text", {}).get("content", "") for p in b.get("heading_2", {}).get("rich_text", []))
            if "Email Enrichment" in text:
                parts = text.split(" — ")
                date_str = parts[-1].strip() if len(parts) > 1 else ""
                in_recent = date_str >= cutoff
                in_flags = False
            else:
                in_recent = False
        elif btype == "heading_3" and in_recent:
            text = "".join(p.get("text", {}).get("content", "") for p in b.get("heading_3", {}).get("rich_text", []))
            in_flags = "Flags" in text
        elif btype == "bulleted_list_item" and in_recent and in_flags:
            text = "".join(p.get("text", {}).get("content", "") for p in b.get("bulleted_list_item", {}).get("rich_text", []))
            if text and ("OPEN_ACTION" in text or "BLOCKER" in text):
                flags.append(text[:300])
    return flags


def _prep_block(text: str, btype: str = "paragraph") -> dict:
    """Build a Notion block."""
    return {
        "object": "block", "type": btype,
        btype: {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]},
    }


def _heading(text: str, level: int = 2) -> dict:
    htype = f"heading_{level}"
    return {
        "object": "block", "type": htype,
        htype: {"rich_text": [{"type": "text", "text": {"content": text}}]},
    }


def _bullet(text: str) -> dict:
    return _prep_block(text, "bulleted_list_item")


def _divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def build_recurring_client_prep_blocks(
    client_name: str,
    meeting_time: str,
    attendees: list[str],
    recent_entries: list[dict],
    flags: list[str],
) -> list[dict]:
    """Build Notion blocks for a recurring client meeting prep doc."""
    blocks = [
        _heading(f"{client_name} — Meeting Prep", 1),
        _prep_block(f"Time: {meeting_time}"),
        _prep_block(f"Attendees: {', '.join(attendees)}"),
        _divider(),
    ]

    if flags:
        blocks.append(_heading("🚨 Open Items / Blockers", 2))
        for f in flags[:8]:
            blocks.append(_bullet(f))

    if recent_entries:
        last = recent_entries[0]
        blocks.append(_heading(f"📝 Last Interaction — {last.get('date', '')}", 2))
        blocks.append(_prep_block(last.get("title", "")))
        if last.get("summary"):
            blocks.append(_prep_block(f"Summary: {last['summary']}"))
        if last.get("decisions"):
            blocks.append(_prep_block(f"Decisions: {last['decisions']}"))
        if last.get("actions"):
            blocks.append(_prep_block(f"Action Items: {last['actions']}"))

    if len(recent_entries) > 1:
        blocks.append(_heading("📚 Earlier Activity", 2))
        for e in recent_entries[1:5]:
            line = f"[{e.get('date', '')}] {e.get('title', '')[:80]}"
            if e.get("summary"):
                line += f" — {e['summary'][:120]}"
            blocks.append(_bullet(line))

    blocks.append(_divider())
    blocks.append(_heading("✏️ My Notes", 2))
    blocks.append(_prep_block("(Take notes during the meeting here)"))

    return blocks


def build_onboarding_prep_blocks(
    client_name: str,
    meeting_time: str,
    attendees: list[str],
    cfg: dict | None,
) -> list[dict]:
    blocks = [
        _heading(f"{client_name} — Onboarding Meeting Prep", 1),
        _prep_block(f"Time: {meeting_time}"),
        _prep_block(f"Attendees: {', '.join(attendees)}"),
        _divider(),
        _heading("📋 Standard Onboarding Agenda", 2),
        _bullet("Welcome + intros (5 min)"),
        _bullet("Walk through their business goals and target audience"),
        _bullet("Review onboarding form responses"),
        _bullet("Confirm services scope + timeline"),
        _bullet("Identify key contacts + communication cadence"),
        _bullet("Confirm next steps + homework for both sides"),
        _divider(),
    ]
    if cfg and cfg.get("business_profile_page_id"):
        blocks.append(_heading("🏢 Their Business Profile", 2))
        blocks.append(_prep_block(f"https://notion.so/{cfg['business_profile_page_id'].replace('-', '')}"))
    blocks.append(_heading("✏️ My Notes", 2))
    blocks.append(_prep_block("(Take notes during the meeting here)"))
    return blocks


def build_sales_prep_blocks(
    meeting_title: str,
    meeting_time: str,
    attendees: list[str],
) -> list[dict]:
    external = [a for a in attendees if not a.endswith("@rxmedia.io")]
    domains = list(set(a.split("@")[-1] for a in external))
    blocks = [
        _heading(f"Sales Meeting Prep — {meeting_title}", 1),
        _prep_block(f"Time: {meeting_time}"),
        _prep_block(f"Attendees: {', '.join(attendees)}"),
        _prep_block(f"Domains: {', '.join(domains) if domains else '(none detected)'}"),
        _divider(),
        _heading("🔎 Discovery Questions Checklist", 2),
        _bullet("What's prompting them to look for help now?"),
        _bullet("What's their current marketing setup? What's working / not?"),
        _bullet("What does success look like in 6 months?"),
        _bullet("Budget range / pricing expectations"),
        _bullet("Decision-making timeline + who else is involved"),
        _bullet("Any prior agency experiences (good or bad)"),
        _divider(),
        _prep_block("(Sales template is still evolving — add context below)"),
        _heading("✏️ My Notes", 2),
        _prep_block(""),
    ]
    return blocks


# ── Main flow ──────────────────────────────────────────────────────────────────

async def run(dry: bool = False) -> list[dict]:
    notion = NotionClient(api_key=settings.notion_api_key)
    today = datetime.now().strftime("%A, %b %d")
    print(f"\n{'='*60}")
    print(f"  Meeting Prep — {today}")
    print(f"{'='*60}\n")

    # Fetch calendar events (today + next ~18 hours so evening meetings are included)
    token = await gcal.get_access_token()
    async with httpx.AsyncClient() as http:
        events = await gcal.list_events_today(http, token, lookback_hours=0, lookahead_hours=18)

    print(f"  Found {len(events)} calendar events")

    prep_index = []
    for event in events:
        title = event.get("summary", "(no title)")
        dt, time_str = gcal.extract_event_datetime(event)
        attendees = gcal.extract_attendee_emails(event)
        meeting_type, client_key = gcal.classify_meeting(event, CLIENTS)

        if meeting_type in ("internal", "personal"):
            print(f"  — [{time_str}] {title}  ({meeting_type}, skipped)")
            continue

        print(f"  → [{time_str}] {title}  type={meeting_type}  client={client_key or '-'}")

        # Generate blocks based on type
        if meeting_type == "client_recurring" and client_key:
            cfg = CLIENTS[client_key]
            client_name = cfg.get("name", client_key)
            log_db = cfg.get("client_log_db_id", "")
            profile = cfg.get("business_profile_page_id", "")
            recent = await _load_recent_client_log(notion, log_db) if log_db else []
            flags = await _load_business_profile_flags(notion, profile) if profile else []
            blocks = build_recurring_client_prep_blocks(
                client_name, time_str, attendees, recent, flags,
            )
            prep_title = f"{client_name} — Meeting Prep — {dt.strftime('%Y-%m-%d')}"
        elif meeting_type == "onboarding":
            cfg = CLIENTS.get(client_key, {}) if client_key else {}
            client_name = cfg.get("name", title.split("—")[0].strip() or title)
            blocks = build_onboarding_prep_blocks(client_name, time_str, attendees, cfg)
            prep_title = f"{client_name} — Onboarding Prep — {dt.strftime('%Y-%m-%d')}"
        elif meeting_type == "sales":
            blocks = build_sales_prep_blocks(title, time_str, attendees)
            prep_title = f"Sales: {title[:40]} — {dt.strftime('%Y-%m-%d')}"
        else:  # unknown
            blocks = build_sales_prep_blocks(title, time_str, attendees)
            prep_title = f"{title[:50]} — Prep — {dt.strftime('%Y-%m-%d')}"

        if dry:
            print(f"    [DRY] would create: {prep_title}  ({len(blocks)} blocks)")
            prep_index.append({
                "time": time_str, "original_title": title, "prep_title": prep_title,
                "url": "(dry-run)", "type": meeting_type, "client": client_key,
            })
            continue

        # Create Notion page under root
        try:
            r = await notion._client.request(
                path="pages", method="POST",
                body={
                    "parent": {"type": "page_id", "page_id": ROOT_PAGE_ID},
                    "properties": {"title": {"title": [{"text": {"content": prep_title}}]}},
                    "children": blocks[:100],  # Notion caps at 100 blocks per create
                },
            )
            page_id = r["id"]
            url = f"https://notion.so/{page_id.replace('-', '')}"
            print(f"    ✓ Created: {url}")
            prep_index.append({
                "time": time_str, "original_title": title, "prep_title": prep_title,
                "url": url, "type": meeting_type, "client": client_key,
            })
        except Exception as e:
            print(f"    ⚠ Failed to create: {e}")

    print(f"\n  Created {len([p for p in prep_index if p.get('url') != '(dry-run)'])} prep docs")
    return prep_index


def main() -> None:
    parser = argparse.ArgumentParser(description="Meeting prep doc generator")
    parser.add_argument("--dry", action="store_true", help="Preview only, don't create docs")
    args = parser.parse_args()
    asyncio.run(run(dry=args.dry))


if __name__ == "__main__":
    main()
