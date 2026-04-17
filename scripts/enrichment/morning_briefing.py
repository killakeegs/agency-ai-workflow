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

async def _fetch_flagged_profiles(notion: NotionClient) -> dict:
    """Scan each client's Business Profile for recent Email Enrichment flags.
    Returns dict with deduped + categorized flags:
      {"blockers": [(client, text)], "open_actions": [(client, text)], "total_before_dedupe": N}
    """
    all_raw_flags: list[tuple[str, str]] = []
    cutoff_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

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
                if text and ("OPEN_ACTION" in text or "BLOCKER" in text):
                    all_raw_flags.append((cfg.get("name", client_key), text[:250]))

    # Dedupe by (client, description core text — ignoring date prefix)
    seen: set[tuple[str, str]] = set()
    deduped: list[tuple[str, str]] = []
    for client_name, flag_text in all_raw_flags:
        # Extract core description (strip [TYPE] and (date) prefixes)
        import re as _re
        core = _re.sub(r"^\[[^\]]+\]\s*(\(\d{4}-\d{2}-\d{2}\)\s*)?", "", flag_text).strip().lower()
        key = (client_name, core[:120])
        if key in seen:
            continue
        seen.add(key)
        deduped.append((client_name, flag_text))

    blockers = [f for f in deduped if "BLOCKER" in f[1]]
    open_actions = [f for f in deduped if "OPEN_ACTION" in f[1]]

    # Sort open actions by date (most recent first)
    def _extract_date(flag_text: str) -> str:
        import re as _re
        m = _re.search(r"\((\d{4}-\d{2}-\d{2})\)", flag_text)
        return m.group(1) if m else ""
    open_actions.sort(key=lambda f: _extract_date(f[1]), reverse=True)
    blockers.sort(key=lambda f: _extract_date(f[1]), reverse=True)

    return {
        "blockers": blockers,
        "open_actions": open_actions,
        "total_before_dedupe": len(all_raw_flags),
        "total_after_dedupe": len(deduped),
    }


# ── Compose briefings ──────────────────────────────────────────────────────────

def _agency_pulse(
    total_overdue: int,
    flags_data: dict,
    enriched_clients: set[str],
) -> str:
    today = datetime.now().strftime("%A, %b %d")
    blockers = flags_data["blockers"]
    open_actions = flags_data["open_actions"]

    lines = [
        f"🌅 *Good Morning RxMedia — {today}*",
        "",
        f"*Pulse:*",
        f"• *{total_overdue}* overdue ClickUp tasks across the team",
    ]
    if blockers:
        lines.append(f"• 🚨 *{len(blockers)}* active BLOCKERs")
    lines.append(f"• *{len(open_actions)}* open action items in last 7 days")
    if enriched_clients:
        lines.append(f"• Active clients: {', '.join(sorted(list(enriched_clients))[:8])}")
    lines.extend([
        "",
        "_Keegan receives a DM with top flags. DM Rex anytime for full lists._",
    ])
    return "\n".join(lines)


def _personal_briefing(
    name: str,
    overdue_tasks: list[dict],
    flags_data: dict | None,
) -> str:
    today = datetime.now().strftime("%A, %b %d")
    lines = [f"🌅 *Good Morning {name} — {today}*", ""]

    if overdue_tasks:
        lines.append(f"*Your {len(overdue_tasks)} Overdue ClickUp Tasks:*")
        for t in overdue_tasks[:8]:
            lines.append(_format_task_line(t))
        if len(overdue_tasks) > 8:
            lines.append(f"_+ {len(overdue_tasks) - 8} more — DM Rex for the full list_")
        lines.append("")
    else:
        lines.append("✓ No overdue tasks. Nice.")
        lines.append("")

    if flags_data:
        blockers = flags_data["blockers"]
        open_actions = flags_data["open_actions"]

        if blockers:
            lines.append(f"🚨 *Active BLOCKERs ({len(blockers)}):*")
            for client_name, flag_text in blockers[:5]:
                import re as _re
                clean = _re.sub(r"^\[BLOCKER\]\s*", "", flag_text)
                lines.append(f"• *{client_name}* — {clean[:180]}")
            if len(blockers) > 5:
                lines.append(f"_+ {len(blockers) - 5} more blockers_")
            lines.append("")

        if open_actions:
            lines.append(f"*Top 5 Most Recent Open Actions ({len(open_actions)} total):*")
            for client_name, flag_text in open_actions[:5]:
                import re as _re
                clean = _re.sub(r"^\[OPEN_ACTION\]\s*", "", flag_text)
                lines.append(f"• *{client_name}* — {clean[:180]}")
            lines.append("")
            lines.append("_DM Rex: \"show all open flags for [client]\" to dig deeper._")

    lines.append("")
    lines.append("_Tell Rex when tasks are done: \"mark [task] as complete\" or \"close the COI task\"._")
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
        flags_data = await _fetch_flagged_profiles(notion)
        all_clients_with_flags = {c for c, _ in flags_data["blockers"]} | {c for c, _ in flags_data["open_actions"]}
        print(f"  Flags: {flags_data['total_before_dedupe']} raw → {flags_data['total_after_dedupe']} after dedupe")
        print(f"         {len(flags_data['blockers'])} blockers, {len(flags_data['open_actions'])} open actions")

        # 4. Agency pulse → #general
        enriched_clients = all_clients_with_flags
        pulse = _agency_pulse(len(tasks), flags_data, enriched_clients)
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
            # Keegan sees agency flags; others see only their overdue
            flags_for_user = flags_data if email == "keegan@rxmedia.io" else None

            has_flags = flags_for_user and (flags_for_user["blockers"] or flags_for_user["open_actions"])
            if not overdue_tasks and not has_flags:
                print(f"  {name}: nothing to send (no overdue, no flags)")
                continue

            msg = _personal_briefing(name, overdue_tasks, flags_for_user)
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
