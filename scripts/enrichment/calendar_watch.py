#!/usr/bin/env python3
"""
calendar_watch.py — Alert teammates when they're added to a new calendar event.

Polls Keegan's primary Google Calendar for events in the next 7 days, compares
against a "Calendar Watch State" DB in Notion, and DMs any TEAM member who
appears as a new attendee since the last tick. Catches the "someone added me
to a meeting but I was heads-down and didn't see the email" case.

Rules (locked with Keegan on 2026-04-23):
- Lookahead: 7 days
- Skip organizer of the event (they made it)
- Skip declined attendees (responseStatus == 'declined')
- Skip personal events (classified by gcal.classify_meeting)
- Skip past events
- Alert once per series (recurring meetings collapse on recurringEventId)
- Alert fires on BOTH new events AND attendees newly added to existing events
- No removal alerts
- No "rush" louder alert for <30min-away meetings — single format
- Quiet hours: weekends all day, weekdays <7am PT and ≥6pm PT. In quiet hours
  the tick exits early without updating state, so anything added during those
  windows is detected as new on the next business-hours tick.

DM format:
    📅 New meeting on your calendar
    {title}
    {day, time PT}
    Organizer: {email}
    <htmlLink|Open in Calendar>

State DB schema (auto-created on first run):
  - Key           (title)       — series_id (recurringEventId or event.id)
  - Attendees     (rich_text)   — comma-separated TEAM emails currently on it
  - Alerted       (rich_text)   — comma-separated TEAM emails already DM'd
  - Last Seen     (date)        — for GC of stale rows
  - Event Title   (rich_text)   — for human inspection

Usage:
    python3 scripts/enrichment/calendar_watch.py            # one tick
    python3 scripts/enrichment/calendar_watch.py --dry      # preview, no DMs/state
    python3 scripts/enrichment/calendar_watch.py --force    # ignore quiet hours

Railway cron (every 15 min):
    */15 * * * *   python3 scripts/enrichment/calendar_watch.py
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent.parent / ".env")

from config.clients import CLIENTS
from src.config import settings
from src.integrations import google_calendar as gcal
from src.integrations.notion import NotionClient
from scripts.enrichment.morning_briefing import TEAM  # source of truth for teammates

SLACK_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "").strip()
STATE_DB_NAME = "Calendar Watch State"
PACIFIC_TZ = ZoneInfo("America/Los_Angeles")
LOOKAHEAD_DAYS = 7
QUIET_HOUR_START = 18   # 6pm PT
QUIET_HOUR_END = 7      # 7am PT


# ── Quiet hours ───────────────────────────────────────────────────────────────

def _in_quiet_hours(now_utc: datetime) -> bool:
    pt = now_utc.astimezone(PACIFIC_TZ)
    if pt.weekday() >= 5:  # Sat=5, Sun=6
        return True
    return pt.hour < QUIET_HOUR_END or pt.hour >= QUIET_HOUR_START


# ── Slack helpers ─────────────────────────────────────────────────────────────

async def _lookup_slack_ids(http: httpx.AsyncClient) -> dict[str, str]:
    """email → slack user ID for everyone in TEAM."""
    ids: dict[str, str] = {}
    cursor = None
    while True:
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        r = await http.get(
            "https://slack.com/api/users.list",
            headers={"Authorization": f"Bearer {SLACK_TOKEN}"},
            params=params, timeout=15,
        )
        data = r.json()
        if not data.get("ok"):
            break
        for u in data.get("members", []):
            email = (u.get("profile", {}).get("email") or "").lower()
            if email in TEAM:
                ids[email] = u.get("id", "")
        cursor = data.get("response_metadata", {}).get("next_cursor", "")
        if not cursor:
            break
    return ids


async def _dm(http: httpx.AsyncClient, slack_id: str, text: str) -> None:
    open_r = await http.post(
        "https://slack.com/api/conversations.open",
        headers={"Authorization": f"Bearer {SLACK_TOKEN}"},
        json={"users": slack_id}, timeout=15,
    )
    data = open_r.json()
    if not data.get("ok"):
        return
    await http.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_TOKEN}"},
        json={"channel": data["channel"]["id"], "text": text, "unfurl_links": False},
        timeout=15,
    )


# ── Calendar fetch ────────────────────────────────────────────────────────────

async def _fetch_upcoming(http: httpx.AsyncClient, token: str, days: int) -> list[dict]:
    """Fetch events from now to `days` ahead. Handles pagination."""
    now = datetime.now(timezone.utc)
    time_min = now.isoformat()
    time_max = (now + timedelta(days=days)).isoformat()

    events: list[dict] = []
    page_token: str | None = None
    while True:
        params = {
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": 250,
        }
        if page_token:
            params["pageToken"] = page_token
        r = await http.get(
            f"{gcal.BASE}/calendars/primary/events",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )
        r.raise_for_status()
        data = r.json()
        events.extend(data.get("items", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return events


def _series_id(event: dict) -> str:
    """Collapse recurring instances to one key; single events use their own id."""
    return event.get("recurringEventId") or event.get("id", "")


def _team_attendees_not_declined(event: dict) -> set[str]:
    """Return TEAM emails on this event who haven't declined."""
    out: set[str] = set()
    for a in event.get("attendees", []) or []:
        email = (a.get("email") or "").lower()
        if email not in TEAM:
            continue
        if (a.get("responseStatus") or "").lower() == "declined":
            continue
        out.add(email)
    return out


def _organizer_email(event: dict) -> str:
    return (event.get("organizer", {}).get("email") or "").lower()


# ── State DB ──────────────────────────────────────────────────────────────────

async def _get_or_create_state_db(notion: NotionClient) -> str:
    root = os.environ.get("NOTION_WORKSPACE_ROOT_PAGE_ID", "").strip()
    if not root:
        raise ValueError("NOTION_WORKSPACE_ROOT_PAGE_ID not set")

    r = await notion._client.request(
        path="search", method="POST",
        body={"query": STATE_DB_NAME, "filter": {"value": "database", "property": "object"}},
    )
    for row in r.get("results", []):
        title = "".join(p.get("text", {}).get("content", "") for p in row.get("title", []))
        if title == STATE_DB_NAME:
            return row["id"]

    created = await notion._client.request(
        path="databases", method="POST",
        body={
            "parent": {"type": "page_id", "page_id": root},
            "title": [{"type": "text", "text": {"content": STATE_DB_NAME}}],
            "properties": {
                "Key":         {"title": {}},
                "Event Title": {"rich_text": {}},
                "Attendees":   {"rich_text": {}},
                "Alerted":     {"rich_text": {}},
                "Last Seen":   {"date": {}},
            },
        },
    )
    print(f"  Created {STATE_DB_NAME} DB: {created['id']}")
    return created["id"]


async def _load_state(notion: NotionClient, db_id: str) -> dict[str, dict]:
    """Returns {series_id: {"page_id", "attendees": set[str], "alerted": set[str]}}."""
    out: dict[str, dict] = {}
    cursor = None
    while True:
        body: dict = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        r = await notion._client.request(
            path=f"databases/{db_id}/query", method="POST", body=body,
        )
        for row in r.get("results", []):
            props = row.get("properties", {})
            key = "".join(p.get("plain_text", "") for p in props.get("Key", {}).get("title", []))
            if not key:
                continue
            attendees_raw = "".join(p.get("plain_text", "") for p in props.get("Attendees", {}).get("rich_text", []))
            alerted_raw = "".join(p.get("plain_text", "") for p in props.get("Alerted", {}).get("rich_text", []))
            out[key] = {
                "page_id": row["id"],
                "attendees": {e.strip().lower() for e in attendees_raw.split(",") if e.strip()},
                "alerted": {e.strip().lower() for e in alerted_raw.split(",") if e.strip()},
            }
        if not r.get("has_more"):
            break
        cursor = r.get("next_cursor")
    return out


async def _upsert_state_row(
    notion: NotionClient, db_id: str, series_id: str, title: str,
    attendees: set[str], alerted: set[str], page_id: str | None,
) -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    props = {
        "Attendees":   {"rich_text": [{"text": {"content": ",".join(sorted(attendees))[:1900]}}]},
        "Alerted":     {"rich_text": [{"text": {"content": ",".join(sorted(alerted))[:1900]}}]},
        "Last Seen":   {"date": {"start": today}},
        "Event Title": {"rich_text": [{"text": {"content": title[:200]}}]},
    }
    if page_id:
        await notion._client.request(
            path=f"pages/{page_id}", method="PATCH", body={"properties": props},
        )
    else:
        props["Key"] = {"title": [{"text": {"content": series_id}}]}
        await notion._client.request(
            path="pages", method="POST",
            body={"parent": {"database_id": db_id}, "properties": props},
        )


async def _gc_stale_rows(notion: NotionClient, db_id: str, cutoff_days: int = 14) -> int:
    """Archive state rows whose Last Seen is older than cutoff — keeps DB bounded."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=cutoff_days)).date().isoformat()
    r = await notion._client.request(
        path=f"databases/{db_id}/query", method="POST",
        body={
            "filter": {"property": "Last Seen", "date": {"before": cutoff}},
            "page_size": 100,
        },
    )
    n = 0
    for row in r.get("results", []):
        await notion._client.request(
            path=f"pages/{row['id']}", method="PATCH",
            body={"in_trash": True},
        )
        n += 1
    return n


# ── DM formatting ─────────────────────────────────────────────────────────────

def _format_dm(event: dict) -> str:
    title = event.get("summary", "(no title)")
    dt, _time_str = gcal.extract_event_datetime(event)
    if dt:
        pt = dt.astimezone(PACIFIC_TZ)
        when = pt.strftime("%A, %b %-d at %-I:%M %p PT")
    else:
        when = "Time TBD"
    organizer = _organizer_email(event) or "(unknown)"
    html_link = event.get("htmlLink", "")

    lines = [
        "📅 *New meeting on your calendar*",
        f"*{title}*",
        f"_{when}_",
        f"Organizer: {organizer}",
    ]
    if html_link:
        lines.append(f"<{html_link}|Open in Calendar>")
    return "\n".join(lines)


# ── Main tick ─────────────────────────────────────────────────────────────────

async def tick(dry_run: bool = False, force: bool = False) -> None:
    now = datetime.now(timezone.utc)
    print(f"\n{'='*50}")
    print(f"  Calendar Watch — {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*50}\n")

    if not force and _in_quiet_hours(now):
        pt = now.astimezone(PACIFIC_TZ)
        print(f"  Quiet hours ({pt.strftime('%a %H:%M PT')}) — skipping tick. "
              f"State not updated so pending changes detect on next business-hours tick.")
        return

    if not SLACK_TOKEN:
        print("⚠ SLACK_BOT_TOKEN not set")
        return

    notion = NotionClient(api_key=settings.notion_api_key)
    state_db = await _get_or_create_state_db(notion)
    state = await _load_state(notion, state_db)
    bootstrap = len(state) == 0
    print(f"  Loaded state: {len(state)} tracked event series"
          f"{' (empty → bootstrap run, no DMs will fire)' if bootstrap else ''}")

    async with httpx.AsyncClient() as http:
        token = await gcal.get_access_token()
        events = await _fetch_upcoming(http, token, LOOKAHEAD_DAYS)
        print(f"  Fetched {len(events)} upcoming events (next {LOOKAHEAD_DAYS}d)")

        slack_ids = await _lookup_slack_ids(http)
        print(f"  Slack IDs resolved: {len(slack_ids)}/{len(TEAM)}")

        # Collapse recurring-instance → series
        series: dict[str, dict] = {}
        for ev in events:
            sid = _series_id(ev)
            if not sid:
                continue
            # Skip past occurrences (shouldn't happen with timeMin=now, but defensive)
            dt, _ = gcal.extract_event_datetime(ev)
            if dt and dt < now:
                continue
            # Skip personal
            mtype, _client_key = gcal.classify_meeting(ev, CLIENTS)
            if mtype == "personal":
                continue
            # Keep first-seen instance per series
            if sid not in series:
                series[sid] = ev

        alerts_sent = 0
        for sid, ev in series.items():
            current_team_attendees = _team_attendees_not_declined(ev)
            organizer = _organizer_email(ev)
            # Organizer isn't considered "new" — they made the invite
            eligible = current_team_attendees - {organizer}

            prior = state.get(sid, {})
            prior_attendees = prior.get("attendees", set())
            already_alerted = prior.get("alerted", set())

            # "New" = in current TEAM attendees, wasn't in prior attendees,
            # and hasn't been alerted before
            newly_added = eligible - prior_attendees - already_alerted

            if not current_team_attendees and not prior:
                # Meeting with no TEAM attendees — don't even create state for it
                continue

            title = ev.get("summary", "(no title)")
            sent_this_tick: set[str] = set()
            for email in sorted(newly_added):
                slack_id = slack_ids.get(email, "")
                if not slack_id:
                    print(f"    ⚠ no Slack ID for {email} (team member: {TEAM[email]['name']})")
                    continue
                if bootstrap:
                    # First-run: record attendees into state silently so the *next*
                    # tick only DMs on genuine changes, not on the entire backlog.
                    sent_this_tick.add(email)
                    continue
                msg = _format_dm(ev)
                print(f"  📅 → {TEAM[email]['name']} re: {title[:60]}")
                if not dry_run:
                    await _dm(http, slack_id, msg)
                sent_this_tick.add(email)
                alerts_sent += 1

            # Update state: attendees = current (so removal is tracked implicitly,
            # even though we don't alert on it), alerted = prior ∪ newly-alerted
            new_alerted = already_alerted | sent_this_tick
            if not dry_run:
                await _upsert_state_row(
                    notion, state_db, sid, title, current_team_attendees,
                    new_alerted, prior.get("page_id"),
                )

        # Garbage collect stale rows
        if not dry_run:
            gc = await _gc_stale_rows(notion, state_db, cutoff_days=14)
            if gc:
                print(f"  🗑  Archived {gc} stale state rows")

    print(f"\n  Done. {alerts_sent} DM(s) sent ({'dry run' if dry_run else 'live'}).")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="Preview only, don't DM or write state")
    ap.add_argument("--force", action="store_true", help="Ignore quiet hours")
    args = ap.parse_args()
    asyncio.run(tick(dry_run=args.dry, force=args.force))


if __name__ == "__main__":
    main()
