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
MEETING_PREP_PARENT_ID = (
    os.environ.get("NOTION_MEETING_PREP_PAGE_ID", "").strip()
    or ROOT_PAGE_ID
)


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


async def build_internal_team_sync_blocks(
    notion: NotionClient,
    meeting_title: str,
    meeting_time: str,
    attendees: list[str],
) -> list[dict]:
    """Build rich prep for the RxMedia Weekly Sync.

    Pulls:
      - Last 3 weekly syncs from RxMedia's Client Log (with summary + action items)
      - Active client blockers across all clients (last 14 days)
      - Standing agenda scaffolding
    """
    from config.clients import CLIENTS as _CLIENTS
    rxmedia_log = _CLIENTS.get("rxmedia", {}).get("client_log_db_id", "")

    blocks = [
        _heading(f"{meeting_title} Prep", 1),
        _prep_block(f"Time: {meeting_time}"),
        _prep_block(f"Attendees: {', '.join(attendees)}"),
        _divider(),
    ]

    # 1. Last 3 syncs — pull from RxMedia Client Log with full recap
    if rxmedia_log:
        try:
            rows = await notion._client.request(
                path=f"databases/{rxmedia_log}/query", method="POST",
                body={
                    "page_size": 10,
                    "sorts": [{"property": "Date", "direction": "descending"}],
                },
            )
            past_syncs = []
            for r in rows.get("results", []):
                props = r.get("properties", {})
                title = "".join(p.get("text", {}).get("content", "") for p in props.get("Title", {}).get("title", []))
                if "Weekly Synch" in title or "Team Sync" in title:
                    date_obj = props.get("Date", {}).get("date")
                    date_str = date_obj.get("start", "") if date_obj else ""
                    summary = "".join(p.get("text", {}).get("content", "") for p in props.get("Summary", {}).get("rich_text", []))
                    decisions = "".join(p.get("text", {}).get("content", "") for p in props.get("Key Decisions", {}).get("rich_text", []))
                    actions = "".join(p.get("text", {}).get("content", "") for p in props.get("Action Items", {}).get("rich_text", []))
                    past_syncs.append({
                        "date": date_str, "title": title, "summary": summary,
                        "decisions": decisions, "actions": actions,
                        "id": r["id"],
                    })
                if len(past_syncs) >= 3:
                    break

            if past_syncs:
                # Most recent gets full detail
                latest = past_syncs[0]
                blocks.append(_heading(f"📝 Last Sync — {latest['date']}", 2))
                if latest.get("summary"):
                    blocks.append(_prep_block(f"Summary: {latest['summary'][:600]}"))
                if latest.get("actions"):
                    blocks.append(_prep_block(f"Action Items from last week:"))
                    for line in latest["actions"].split("\n"):
                        line = line.strip().lstrip("-").strip()
                        if line:
                            blocks.append(_bullet(line[:200]))
                blocks.append(_prep_block(f"Full notes: https://notion.so/{latest['id'].replace('-', '')}"))

                # Previous 2 get short-form
                if len(past_syncs) > 1:
                    blocks.append(_heading("🗓️ Recent Syncs (context)", 3))
                    for s in past_syncs[1:3]:
                        line = f"[{s['date']}]: {s.get('summary', '')[:180]}"
                        blocks.append(_bullet(line))
        except Exception as e:
            print(f"    (couldn't load past syncs: {e})")

    # 2. Active blockers across all clients (reuses morning briefing logic structure)
    blocks.append(_heading("🚨 Active Blockers Across Clients", 2))
    from config.clients import CLIENTS as _CLIENTS
    cutoff_date = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
    blockers_found = 0
    for ck, cfg in _CLIENTS.items():
        if cfg.get("internal"): continue
        profile_id = cfg.get("business_profile_page_id", "")
        if not profile_id: continue
        try:
            br = await notion._client.request(
                path=f"blocks/{profile_id}/children?page_size=100", method="GET",
            )
        except Exception:
            continue
        in_recent, in_flags = False, False
        for b in br.get("results", []):
            btype = b.get("type", "")
            if btype == "heading_2":
                text = "".join(p.get("text", {}).get("content", "") for p in b.get("heading_2", {}).get("rich_text", []))
                if "Email Enrichment" in text:
                    parts = text.split(" — ")
                    date_str = parts[-1].strip() if len(parts) > 1 else ""
                    in_recent = date_str >= cutoff_date
                    in_flags = False
                else:
                    in_recent = False
            elif btype == "heading_3" and in_recent:
                text = "".join(p.get("text", {}).get("content", "") for p in b.get("heading_3", {}).get("rich_text", []))
                in_flags = "Flags" in text
            elif btype == "bulleted_list_item" and in_recent and in_flags:
                text = "".join(p.get("text", {}).get("content", "") for p in b.get("bulleted_list_item", {}).get("rich_text", []))
                if "BLOCKER" in text:
                    blocks.append(_bullet(f"**{cfg.get('name', ck)}** — {text[:220]}"))
                    blockers_found += 1
                    break  # one blocker per client max for brevity
    if blockers_found == 0:
        blocks.append(_prep_block("No active blockers in last 14 days."))

    # 3. Agenda scaffolding
    blocks.append(_divider())
    blocks.append(_heading("📋 Standing Agenda", 2))
    blocks.append(_bullet("Client pulse — any on fire? Any falling behind?"))
    blocks.append(_bullet("Projects on deck this week + deadlines"))
    blocks.append(_bullet("Team blockers — what's stuck + who needs help"))
    blocks.append(_bullet("Wins from last week"))
    blocks.append(_bullet("Upcoming client meetings this week (use prep docs)"))
    blocks.append(_divider())
    blocks.append(_heading("✏️ This Week's Notes", 2))
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

        if meeting_type == "personal":
            print(f"  — [{time_str}] {title}  (personal, skipped entirely)")
            continue

        if meeting_type == "external_hosted":
            # Show in briefing but don't generate a prep doc
            print(f"  → [{time_str}] {title}  (external-hosted, no prep doc)")
            # Try to find organizer for labeling
            org = event.get("organizer", {}) or {}
            org_email = (org.get("email") or "").lower()
            host_domain = org_email.split("@")[-1] if "@" in org_email else "external"
            prep_index.append({
                "time": time_str, "original_title": title, "prep_title": title,
                "url": "", "type": meeting_type, "host": host_domain,
                "attendees": attendees,
            })
            continue

        print(f"  → [{time_str}] {title}  type={meeting_type}  client={client_key or '-'}")

        # Internal team sync → rich team prep if it looks like RxMedia Weekly Sync
        if meeting_type == "internal":
            if any(kw in title.lower() for kw in ("rxmedia", "weekly synch", "team synch", "team sync", "weekly sync")):
                blocks = await build_internal_team_sync_blocks(notion, title, time_str, attendees)
                prep_title = f"{title[:50]} — {dt.astimezone(gcal.PACIFIC_TZ).strftime('%Y-%m-%d')}"
            else:
                # Other internal meetings — lightweight, just a note-taking surface
                blocks = [
                    _heading(f"{title} — Prep", 1),
                    _prep_block(f"Time: {time_str}"),
                    _prep_block(f"Attendees: {', '.join(attendees)}"),
                    _divider(),
                    _heading("✏️ Notes", 2),
                    _prep_block(""),
                ]
                prep_title = f"{title[:50]} — {dt.astimezone(gcal.PACIFIC_TZ).strftime('%Y-%m-%d')}"
        # Generate blocks based on type
        elif meeting_type == "client_recurring" and client_key:
            cfg = CLIENTS[client_key]
            client_name = cfg.get("name", client_key)
            log_db = cfg.get("client_log_db_id", "")
            profile = cfg.get("business_profile_page_id", "")
            recent = await _load_recent_client_log(notion, log_db) if log_db else []
            flags = await _load_business_profile_flags(notion, profile) if profile else []
            blocks = build_recurring_client_prep_blocks(
                client_name, time_str, attendees, recent, flags,
            )
            prep_title = f"{client_name} — Meeting Prep — {dt.astimezone(gcal.PACIFIC_TZ).strftime('%Y-%m-%d')}"
        elif meeting_type == "onboarding":
            cfg = CLIENTS.get(client_key, {}) if client_key else {}
            client_name = cfg.get("name", title.split("—")[0].strip() or title)
            blocks = build_onboarding_prep_blocks(client_name, time_str, attendees, cfg)
            prep_title = f"{client_name} — Onboarding Prep — {dt.astimezone(gcal.PACIFIC_TZ).strftime('%Y-%m-%d')}"
        elif meeting_type == "sales":
            blocks = build_sales_prep_blocks(title, time_str, attendees)
            prep_title = f"Sales: {title[:40]} — {dt.astimezone(gcal.PACIFIC_TZ).strftime('%Y-%m-%d')}"
        else:  # unknown
            blocks = build_sales_prep_blocks(title, time_str, attendees)
            prep_title = f"{title[:50]} — Prep — {dt.astimezone(gcal.PACIFIC_TZ).strftime('%Y-%m-%d')}"

        # Figure out where this prep doc goes. Client meetings → per-client
        # Meeting Prep DB. Everything else (sales/internal/unknown/external)
        # falls back to the master Meeting Prep Docs page as before.
        target_db_id = ""
        if client_key and meeting_type in ("client_recurring", "onboarding"):
            target_db_id = CLIENTS.get(client_key, {}).get("meeting_prep_db_id", "")
            meeting_type_label = (
                "Client Recurring" if meeting_type == "client_recurring" else "Onboarding"
            )
        elif meeting_type == "sales":
            meeting_type_label = "Sales"
        else:
            meeting_type_label = "Other"

        meeting_date_iso = dt.astimezone(gcal.PACIFIC_TZ).strftime("%Y-%m-%d")

        if dry:
            dest = f"DB {target_db_id}" if target_db_id else "master Meeting Prep page"
            print(f"    [DRY] would create: {prep_title}  ({len(blocks)} blocks) → {dest}")
            prep_index.append({
                "time": time_str, "original_title": title, "prep_title": prep_title,
                "url": "(dry-run)", "type": meeting_type, "client": client_key,
                "attendees": attendees,
            })
            continue

        # Create as DB entry if we have a per-client Meeting Prep DB,
        # otherwise fall back to a sub-page under the master Meeting Prep
        # Docs container (for sales/internal/unknown — no client DB target).
        try:
            if target_db_id:
                properties = {
                    "Title":        {"title": [{"text": {"content": prep_title}}]},
                    "Meeting Date": {"date": {"start": meeting_date_iso}},
                    "Status":       {"select": {"name": "Upcoming"}},
                    "Meeting Type": {"select": {"name": meeting_type_label}},
                    "Attendees":    {"rich_text": [{"text": {"content": ", ".join(attendees)[:1900]}}]},
                }
                r = await notion._client.request(
                    path="pages", method="POST",
                    body={
                        "parent": {"type": "database_id", "database_id": target_db_id},
                        "properties": properties,
                        "children": blocks[:100],  # Notion caps at 100 blocks per create
                    },
                )
            else:
                r = await notion._client.request(
                    path="pages", method="POST",
                    body={
                        "parent": {"type": "page_id", "page_id": MEETING_PREP_PARENT_ID},
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
                "attendees": attendees,
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
