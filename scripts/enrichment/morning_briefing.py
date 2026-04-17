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
    now_ms = int(time.time() * 1000)
    params = {
        "include_closed": "false",
        "order_by": "due_date",
        "subtasks": "true",
        "limit": "100",
        "due_date_lt": str(now_ms),
    }
    r = await http.get(
        f"https://api.clickup.com/api/v2/team/{CLICKUP_WORKSPACE_ID}/task",
        headers={"Authorization": CLICKUP_API_KEY},
        params=params, timeout=15,
    )
    if r.status_code != 200:
        return []
    return r.json().get("tasks", [])


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

async def _fetch_flagged_profiles(notion: NotionClient) -> list[tuple[str, str]]:
    """Scan each client's Business Profile for recent Email Enrichment flags.
    Returns list of (client_name, flag_text).
    """
    flags: list[tuple[str, str]] = []
    cutoff_date = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")

    for client_key, cfg in CLIENTS.items():
        if cfg.get("internal"):
            continue
        profile_id = cfg.get("business_profile_page_id", "")
        if not profile_id:
            continue
        try:
            blocks_resp = await notion._client.request(
                path=f"blocks/{profile_id}/children?page_size=100", method="GET",
            )
        except Exception:
            continue

        blocks = blocks_resp.get("results", [])
        in_recent_enrichment = False
        in_flags_section = False
        for b in blocks:
            btype = b.get("type", "")
            if btype == "heading_2":
                text = "".join(p.get("text", {}).get("content", "") for p in b.get("heading_2", {}).get("rich_text", []))
                if "Email Enrichment" in text:
                    # Parse date from heading
                    parts = text.split(" — ")
                    date_str = parts[-1].strip() if len(parts) > 1 else ""
                    in_recent_enrichment = date_str >= cutoff_date
                    in_flags_section = False
                else:
                    in_recent_enrichment = False
                    in_flags_section = False
            elif btype == "heading_3" and in_recent_enrichment:
                text = "".join(p.get("text", {}).get("content", "") for p in b.get("heading_3", {}).get("rich_text", []))
                in_flags_section = "Flags" in text
            elif btype == "bulleted_list_item" and in_recent_enrichment and in_flags_section:
                text = "".join(p.get("text", {}).get("content", "") for p in b.get("bulleted_list_item", {}).get("rich_text", []))
                if text and ("OPEN_ACTION" in text or "BLOCKER" in text or "RULE_SET" in text):
                    flags.append((cfg.get("name", client_key), text[:250]))

    return flags


# ── Compose briefings ──────────────────────────────────────────────────────────

def _agency_pulse(
    total_overdue: int,
    flags_count: int,
    enriched_clients: set[str],
) -> str:
    today = datetime.now().strftime("%A, %b %d")
    lines = [
        f"🌅 *Good Morning RxMedia — {today}*",
        "",
        f"*Pulse:*",
        f"• *{total_overdue}* overdue ClickUp tasks across the team",
        f"• *{flags_count}* flagged items in Notion from the last 48h",
    ]
    if enriched_clients:
        lines.append(f"• Recent client activity: {', '.join(sorted(list(enriched_clients))[:8])}")
    lines.extend([
        "",
        "_Each team member will receive a DM with their specific action items._",
    ])
    return "\n".join(lines)


def _personal_briefing(
    name: str,
    overdue_tasks: list[dict],
    owned_flags: list[tuple[str, str]],
) -> str:
    today = datetime.now().strftime("%A, %b %d")
    lines = [f"🌅 *Good Morning {name} — {today}*", ""]

    if overdue_tasks:
        lines.append(f"*Your {len(overdue_tasks)} Overdue ClickUp Tasks:*")
        for t in overdue_tasks[:10]:
            lines.append(_format_task_line(t))
        if len(overdue_tasks) > 10:
            lines.append(f"_+ {len(overdue_tasks) - 10} more_")
        lines.append("")
    else:
        lines.append("✓ No overdue tasks. Nice.")
        lines.append("")

    if owned_flags:
        lines.append(f"*Agency-wide flags needing attention ({len(owned_flags)}):*")
        for client_name, flag_text in owned_flags[:8]:
            lines.append(f"• [{client_name}] {flag_text[:180]}")
        if len(owned_flags) > 8:
            lines.append(f"_+ {len(owned_flags) - 8} more_")

    return "\n".join(lines)


# ── Main ───────────────────────────────────────────────────────────────────────

async def run(dry_run: bool = False) -> None:
    if not SLACK_TOKEN:
        print("⚠ SLACK_BOT_TOKEN not set")
        sys.exit(1)

    print(f"\n{'='*50}")
    print(f"  Morning Briefing — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    if dry_run:
        print(f"  [DRY RUN]")
    print(f"{'='*50}\n")

    notion = NotionClient(api_key=settings.notion_api_key)

    async with httpx.AsyncClient() as http:
        # 1. Slack IDs
        slack_ids = await _lookup_slack_ids(http)
        print(f"  Slack IDs resolved: {len(slack_ids)}/{len(TEAM)}")
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
        flags = await _fetch_flagged_profiles(notion)
        enriched_clients = {name for name, _ in flags}
        print(f"  Flags found: {len(flags)} across {len(enriched_clients)} clients")

        # 4. Agency pulse → #general
        pulse = _agency_pulse(len(tasks), len(flags), enriched_clients)
        print("\n--- AGENCY PULSE (→ #general) ---")
        print(pulse)
        if not dry_run:
            result = await _post_channel(http, TEAM_CHANNEL, pulse)
            if result.get("ok"):
                print("  ✓ Posted to #general")
            else:
                print(f"  ⚠ Failed: {result.get('error')}")

        # 5. Individual DMs
        print("\n--- PERSONAL BRIEFINGS ---")
        for email, info in TEAM.items():
            name = info["name"]
            clickup_id = info["clickup_id"]
            slack_id = slack_ids.get(email, "")

            overdue_tasks = grouped.get(clickup_id, [])
            # Keegan sees ALL flags (per user request); others see only owned
            owned_flags = flags if email == "keegan@rxmedia.io" else []

            if not overdue_tasks and not owned_flags:
                print(f"  {name}: nothing to send (no overdue, no flags)")
                continue

            msg = _personal_briefing(name, overdue_tasks, owned_flags)
            print(f"\n  --- DM to {name} ---")
            print(msg[:400] + ("..." if len(msg) > 400 else ""))

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
    args = parser.parse_args()
    try:
        asyncio.run(run(dry_run=args.dry))
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
