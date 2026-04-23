#!/usr/bin/env python3
"""
morning_briefing.py — Daily 7am PST briefing.

Posts:
  1. Agency-wide pulse to #general channel
  2. Individual DMs to each team member with their overdue ClickUp tasks
     and recently flagged emails they own

Scheduled via Railway cron at 7am PST (15:00 UTC).

Usage:
    python3 scripts/enrichment/morning_briefing.py          # Full run
    python3 scripts/enrichment/morning_briefing.py --dry    # Preview, don't post
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
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
from scripts.enrichment.meeting_prep import run as run_meeting_prep

SLACK_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "").strip()
TEAM_CHANNEL = "general"  # #general in Rx Media workspace
CLICKUP_API_KEY = os.environ.get("CLICKUP_API_KEY", "").strip()
CLICKUP_WORKSPACE_ID = os.environ.get("CLICKUP_WORKSPACE_ID", "").strip()

# Team: email → ClickUp user ID. Slack ID is looked up fresh each run.
TEAM = {
    "keegan@rxmedia.io":      {"name": "Keegan",  "clickup_id": 3852174},
    "content@rxmedia.io":     {"name": "Henna",   "clickup_id": 5847731},
    "systems@rxmedia.io":     {"name": "Justin",  "clickup_id": 54703919},
    "karla@rxmedia.io":       {"name": "Karla",   "clickup_id": 107627361},
    "accounting@rxmedia.io":  {"name": "Mari",    "clickup_id": 95680055},
    "andrea@rxmedia.io":      {"name": "Andrea",  "clickup_id": 78185522},
}


# ── Slack helpers ──────────────────────────────────────────────────────────────

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


async def _post_channel(http: httpx.AsyncClient, channel: str, text: str) -> dict:
    r = await http.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_TOKEN}"},
        json={"channel": channel, "text": text, "unfurl_links": False},
        timeout=15,
    )
    return r.json()


async def _dm_user(http: httpx.AsyncClient, slack_id: str, text: str) -> dict:
    open_r = await http.post(
        "https://slack.com/api/conversations.open",
        headers={"Authorization": f"Bearer {SLACK_TOKEN}"},
        json={"users": slack_id}, timeout=15,
    )
    data = open_r.json()
    if not data.get("ok"):
        return data
    channel_id = data["channel"]["id"]
    return await _post_channel(http, channel_id, text)


# ── ClickUp: overdue tasks per assignee ────────────────────────────────────────

async def _fetch_clickup_tasks(http: httpx.AsyncClient) -> list[dict]:
    """Fetch overdue ClickUp tasks that are actually still open.

    Only looks at tasks overdue by 30 days or less (older cruft is ignored).
    Filters out done/complete/approved statuses that ClickUp's include_closed misses.
    """
    now_ms = int(time.time() * 1000)
    cutoff_ms = int((datetime.now() - timedelta(days=30)).timestamp() * 1000)

    all_tasks: list[dict] = []
    for page in range(5):  # up to 500 tasks max
        params = {
            "include_closed": "false",
            "order_by": "due_date",
            "reverse": "true",  # most recent overdue first
            "subtasks": "true",
            "due_date_gt": str(cutoff_ms),
            "due_date_lt": str(now_ms),
            "page": str(page),
        }
        r = await http.get(
            f"https://api.clickup.com/api/v2/team/{CLICKUP_WORKSPACE_ID}/task",
            headers={"Authorization": CLICKUP_API_KEY},
            params=params, timeout=15,
        )
        if r.status_code != 200:
            break
        batch = r.json().get("tasks", [])
        if not batch:
            break
        all_tasks.extend(batch)
        if len(batch) < 100:
            break

    # Filter out done-like statuses that ClickUp doesn't treat as "closed"
    INACTIVE_STATUSES = {
        "complete", "completed", "done", "100% done", "closed",
        "archived", "cancelled", "approved",
    }
    active = []
    for t in all_tasks:
        status = t.get("status", {})
        status_type = (status.get("type") or "").lower()
        status_name = (status.get("status") or "").lower()

        if status_type in ("closed", "done"):
            continue
        if status_name in INACTIVE_STATUSES:
            continue
        if any(w in status_name for w in ("complete", "done")):
            continue
        if t.get("date_closed"):
            continue

        active.append(t)

    return active


def _group_tasks_by_assignee(tasks: list[dict]) -> dict[int, list[dict]]:
    grouped: dict[int, list[dict]] = {}
    for t in tasks:
        for a in t.get("assignees", []):
            uid = a.get("id")
            if uid:
                grouped.setdefault(uid, []).append(t)
    return grouped


def _format_task_line(t: dict) -> str:
    due_ms = t.get("due_date")
    due_str = ""
    if due_ms:
        due_dt = datetime.fromtimestamp(int(due_ms) / 1000)
        days_overdue = (datetime.now() - due_dt).days
        if days_overdue > 0:
            due_str = f" *({days_overdue}d overdue)*"
        else:
            due_str = f" (due {due_dt.strftime('%b %d')})"
    url = t.get("url", "")
    return f"• <{url}|{t.get('name', '')[:80]}>{due_str}"


# ── Notion: recent flags + today's meetings ────────────────────────────────────

FLAGS_DB_ID = os.environ.get("NOTION_FLAGS_DB_ID", "").strip()


async def _fetch_flagged_profiles(notion: NotionClient) -> dict:
    """Query the Flags DB for all Open + In Progress flags.
    Returns dict: {"blockers": [(client, text)], "open_actions": [(client, text)], ...}
    """
    if not FLAGS_DB_ID:
        return {"blockers": [], "open_actions": [], "total_before_dedupe": 0, "total_after_dedupe": 0}

    rows_all: list[dict] = []
    cursor: str | None = None
    try:
        while True:
            body: dict = {
                "page_size": 100,
                "filter": {
                    "and": [
                        {"property": "Status", "select": {"does_not_equal": "Resolved"}},
                        {"property": "Status", "select": {"does_not_equal": "Won't Fix"}},
                    ]
                },
                "sorts": [{"property": "Source Date", "direction": "descending"}],
            }
            if cursor:
                body["start_cursor"] = cursor
            r = await notion._client.request(
                path=f"databases/{FLAGS_DB_ID}/query", method="POST", body=body,
            )
            rows_all.extend(r.get("results", []))
            if not r.get("has_more"):
                break
            cursor = r.get("next_cursor")
    except Exception as e:
        print(f"  ⚠ Failed to query Flags DB: {e}")
        return {"blockers": [], "open_actions": [], "total_before_dedupe": 0, "total_after_dedupe": 0}

    blockers: list[tuple[str, str]] = []
    open_actions: list[tuple[str, str]] = []

    for row in rows_all:
        props = row.get("properties", {})
        client_parts = props.get("Client", {}).get("rich_text", [])
        client_name = "".join(p.get("text", {}).get("content", "") for p in client_parts)
        desc_parts = props.get("Description", {}).get("rich_text", [])
        desc = "".join(p.get("text", {}).get("content", "") for p in desc_parts)
        type_sel = (props.get("Type", {}).get("select") or {}).get("name", "")
        date_obj = props.get("Source Date", {}).get("date")
        source_date = date_obj.get("start", "") if date_obj else ""

        display = f"[{type_sel}] ({source_date}) {desc}"[:250]
        if type_sel == "BLOCKER":
            blockers.append((client_name, display))
        elif type_sel == "OPEN_ACTION":
            open_actions.append((client_name, display))

    return {
        "blockers": blockers,
        "open_actions": open_actions,
        "total_before_dedupe": len(rows_all),
        "total_after_dedupe": len(blockers) + len(open_actions),
    }


# ── Compose briefings ──────────────────────────────────────────────────────────

def _agency_pulse(
    total_overdue: int,
    flags_data: dict,
    total_meetings_today: int,
) -> str:
    today = datetime.now().strftime("%A, %b %d")
    blockers = flags_data["blockers"]
    open_actions = flags_data["open_actions"]

    lines = [
        f"🌅 *Good Morning RxMedia — {today}*",
        "",
        f"• *{total_meetings_today}* meetings on the schedule today",
        f"• *{total_overdue}* open ClickUp tasks across the team",
    ]
    if blockers:
        lines.append(f"• 🚨 *{len(blockers)}* active blockers — see client logs")
    lines.append(f"• *{len(open_actions)}* open action items (past 7 days)")
    return "\n".join(lines)


def _personal_briefing(
    name: str,
    meetings: list[dict],
    overdue_tasks: list[dict],
    flags_data: dict | None,
    show_pulse: bool = False,
) -> str:
    today = datetime.now().strftime("%A, %b %d")
    lines = [f"🌅 *Good Morning {name} — {today}*", ""]

    # 1. Today's Meetings (top priority)
    if meetings:
        lines.append(f"📅 *Today's Meetings ({len(meetings)})*")
        for m in meetings:
            time_str = m.get("time", "").strip()
            title = m.get("original_title") or m.get("title", "")
            url = m.get("url", "")
            mtype = m.get("type", "")
            host = m.get("host", "")
            if url and url != "(dry-run)":
                lines.append(f"• `{time_str}` *{title[:60]}* — <{url}|prep doc>")
            elif mtype == "external_hosted":
                host_label = f"hosted by {host}" if host and host != "external" else "external host"
                lines.append(f"• `{time_str}` *{title[:60]}* _(no prep — {host_label})_")
            else:
                lines.append(f"• `{time_str}` *{title[:60]}*")
        lines.append("")
    else:
        lines.append("📅 *No meetings today.*")
        lines.append("")

    # 2. Needs Attention — blockers + any urgent open actions
    needs_attention = []
    if flags_data:
        import re as _re
        for client_name, flag_text in flags_data["blockers"][:5]:
            clean = _re.sub(r"^\[BLOCKER\]\s*", "", flag_text)
            needs_attention.append(f"🚨 *{client_name}* — {clean[:150]}")
    if needs_attention:
        lines.append("*🚨 Needs Attention*")
        for item in needs_attention:
            lines.append(f"• {item}")
        lines.append("")

    # 3. Pulse — compact stats (shown to Keegan only)
    if show_pulse and flags_data:
        total_blockers = len(flags_data.get("blockers", []))
        total_actions = len(flags_data.get("open_actions", []))
        pulse_parts = []
        if overdue_tasks:
            pulse_parts.append(f"*{len(overdue_tasks)}* open tasks in your queue")
        if total_actions:
            pulse_parts.append(f"*{total_actions}* open action items across clients")
        if total_blockers:
            pulse_parts.append(f"*{total_blockers}* active blockers")
        if pulse_parts:
            lines.append("📊 *Pulse*")
            lines.append(" · ".join(pulse_parts))
            lines.append("")

    # 4. Personal tasks — only if the user has a meaningful number
    if overdue_tasks and not show_pulse:
        lines.append(f"*Your {len(overdue_tasks)} Open ClickUp Tasks:*")
        for t in overdue_tasks[:5]:
            lines.append(_format_task_line(t))
        if len(overdue_tasks) > 5:
            lines.append(f"_+{len(overdue_tasks) - 5} more in ClickUp_")
        lines.append("")

    # 5. Footer
    if flags_data and flags_data.get("open_actions"):
        lines.append("_DM Rex: \"show all open flags for [client]\" to dig deeper._")
    lines.append("_Reply to Rex: \"create task for...\", \"mark [task] complete\", \"what's on my plate?\"_")
    return "\n".join(lines)


# ── Main ───────────────────────────────────────────────────────────────────────

async def run(dry_run: bool = False, only_email: str = "", skip_channel: bool = False) -> None:
    if not SLACK_TOKEN:
        print("⚠ SLACK_BOT_TOKEN not set")
        sys.exit(1)

    print(f"\n{'='*50}")
    print(f"  Morning Briefing — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    if dry_run:
        print(f"  [DRY RUN]")
    print(f"{'='*50}\n")

    notion = NotionClient(api_key=settings.notion_api_key)

    # 0. Meeting prep — generate Notion prep docs for today's calendar events
    # (only for Keegan's calendar for now — expand to team later)
    print("\n  Generating meeting prep docs...")
    meeting_prep_error: str | None = None
    try:
        meeting_prep_index = await run_meeting_prep(dry=dry_run)
    except Exception as e:
        import traceback
        err_text = str(e)[:200]
        print(f"  ⚠ Meeting prep failed: {err_text}")
        traceback.print_exc()
        meeting_prep_index = []
        meeting_prep_error = err_text

    async with httpx.AsyncClient() as http:
        # 1. Slack IDs
        slack_ids = await _lookup_slack_ids(http)
        print(f"\n  Slack IDs resolved: {len(slack_ids)}/{len(TEAM)}")
        for email, info in TEAM.items():
            status = "✓" if email in slack_ids else "✗ (skip DM)"
            print(f"    {info['name']:10s} {email:30s} {status}")

        # 2. ClickUp overdue tasks
        print("\n  Fetching ClickUp tasks...")
        tasks = await _fetch_clickup_tasks(http)
        grouped = _group_tasks_by_assignee(tasks)
        print(f"  Overdue tasks: {len(tasks)} total, assigned across {len(grouped)} people")

        # 3. Notion flags
        print("\n  Scanning Notion Business Profiles for recent flags...")
        flags_data = await _fetch_flagged_profiles(notion)
        print(f"  Flags: {flags_data['total_before_dedupe']} raw → {flags_data['total_after_dedupe']} after dedupe")
        print(f"         {len(flags_data['blockers'])} blockers, {len(flags_data['open_actions'])} open actions")

        # 4. Agency pulse → #general
        pulse = _agency_pulse(len(tasks), flags_data, len(meeting_prep_index))
        print("\n--- AGENCY PULSE (→ #general) ---")
        print(pulse)
        if not dry_run and not skip_channel:
            result = await _post_channel(http, TEAM_CHANNEL, pulse)
            if result.get("ok"):
                print("  ✓ Posted to #general")
            else:
                print(f"  ⚠ Failed: {result.get('error')}")
        elif skip_channel:
            print("  (skipped — test mode)")

        # 5. Individual DMs
        print("\n--- PERSONAL BRIEFINGS ---")
        for email, info in TEAM.items():
            if only_email and email != only_email:
                continue
            name = info["name"]
            clickup_id = info["clickup_id"]
            slack_id = slack_ids.get(email, "")

            overdue_tasks = grouped.get(clickup_id, [])
            # Keegan sees agency flags + meetings + pulse; others see only their overdue
            is_keegan = email == "keegan@rxmedia.io"
            flags_for_user = flags_data if is_keegan else None
            meetings_for_user = meeting_prep_index if is_keegan else []

            has_flags = flags_for_user and (flags_for_user["blockers"] or flags_for_user["open_actions"])
            if not overdue_tasks and not has_flags and not meetings_for_user:
                print(f"  {name}: nothing to send")
                continue

            msg = _personal_briefing(name, meetings_for_user, overdue_tasks, flags_for_user, show_pulse=is_keegan)
            print(f"\n  --- DM to {name} ---")
            print(msg)

            if not slack_id:
                print(f"  ⚠ No Slack ID for {email} — skipping DM")
                continue

            if not dry_run:
                result = await _dm_user(http, slack_id, msg)
                if result.get("ok"):
                    print(f"  ✓ DM sent to {name}")
                else:
                    print(f"  ⚠ Failed to DM {name}: {result.get('error')}")

    print(f"\n{'='*50}")
    print(f"  Done.")
    print(f"{'='*50}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Morning briefing")
    parser.add_argument("--dry", action="store_true", help="Preview, don't post")
    parser.add_argument("--only", default="", help="Only DM this email (for testing)")
    parser.add_argument("--skip-channel", action="store_true", help="Don't post the #general pulse")
    args = parser.parse_args()
    try:
        asyncio.run(run(dry_run=args.dry, only_email=args.only, skip_channel=args.skip_channel))
    except Exception as e:
        import traceback
        error = traceback.format_exc()
        print(f"\n🚨 Morning Briefing crashed:\n{error}")
        # Best-effort alert
        try:
            import httpx
            with httpx.Client() as http:
                http.post(
                    "https://slack.com/api/chat.postMessage",
                    headers={"Authorization": f"Bearer {SLACK_TOKEN}"},
                    json={"channel": "agency-pipeline", "text": f"🚨 *Morning Briefing Failed*\n```{error[:500]}```"},
                    timeout=10,
                )
        except Exception:
            pass


if __name__ == "__main__":
    main()
