#!/usr/bin/env python3
"""
email_monitor.py — Real-time email monitor for all clients.

Runs on a schedule (Railway cron, every 10-15 min). Each tick:
  1. Reads last_checked_at from Notion monitor state
  2. ONE Gmail search for all emails since last check
  3. Matches messages to clients by sender/recipient domain
  4. Synthesizes new threads with Claude (only for clients with new mail)
  5. Writes to Notion Client Log + Business Profile + Brand Guidelines
  6. Posts flags to Slack
  7. Updates last_checked_at

State: Notion "Email Monitor State" DB (auto-created on first run)
Dedup: Gmail Thread ID on Client Log entries (from shared service)

Usage:
    python3 scripts/enrichment/email_monitor.py              # Run one tick
    python3 scripts/enrichment/email_monitor.py --setup       # Create state DB only
    python3 scripts/enrichment/email_monitor.py --lookback 60 # First run: check last 60 min
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent.parent / ".env")

from config.clients import CLIENTS
from src.config import settings
from src.integrations.notion import NotionClient
from src.integrations.business_profile import load_business_profile
from src.integrations import gmail
from src.services.email_enrichment import (
    synthesize_threads,
    load_existing_log_entries,
    write_client_log,
    append_profile_enrichments,
    apply_rule_set_flags,
    update_last_contact,
)

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "").strip()
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL_INTERNAL", "").strip()

MONITOR_DB_NAME = "Email Monitor State"


# ── Client domain registry ─────────────────────────────────────────────────────

def _build_domain_to_client_map() -> dict[str, str]:
    """Map email domains → client keys for fast lookup."""
    mapping: dict[str, str] = {}
    for key, cfg in CLIENTS.items():
        for field in ("email", "primary_contact_email"):
            email = cfg.get(field, "") or ""
            domain = gmail.extract_domain(email)
            if domain and domain not in ("gmail.com", "rxmedia.io", "google.com", "yahoo.com", "hotmail.com", "outlook.com"):
                mapping[domain] = key
    return mapping


def _match_message_to_client(message: dict, domain_map: dict[str, str], email_map: dict[str, str]) -> str | None:
    """Return client_key if any participant matches a known client."""
    headers = gmail.parse_headers(message)
    for field in ("from", "to", "cc"):
        addrs = re.findall(r"[\w\.-]+@[\w\.-]+", headers.get(field, ""))
        for addr in addrs:
            addr_lower = addr.lower()
            # Check exact email first (handles gmail.com clients like WellWell)
            if addr_lower in email_map:
                return email_map[addr_lower]
            # Then check domain
            domain = gmail.extract_domain(addr_lower)
            if domain in domain_map:
                return domain_map[domain]
    return None


def _build_email_to_client_map() -> dict[str, str]:
    """Map exact email addresses → client keys (for gmail.com clients)."""
    mapping: dict[str, str] = {}
    for key, cfg in CLIENTS.items():
        for field in ("email", "primary_contact_email"):
            email = (cfg.get(field, "") or "").lower().strip()
            if email and email != "keegan@rxmedia.io":
                mapping[email] = key
    return mapping


# ── Notion state management ────────────────────────────────────────────────────

async def _get_or_create_state_db(notion: NotionClient) -> str:
    """Find or create the Email Monitor State DB."""
    root_page_id = os.environ.get("NOTION_WORKSPACE_ROOT_PAGE_ID", "").strip()
    if not root_page_id:
        raise ValueError("NOTION_WORKSPACE_ROOT_PAGE_ID not set")

    # Search for existing
    results = await notion._client.request(
        path="search",
        method="POST",
        body={
            "query": MONITOR_DB_NAME,
            "filter": {"value": "database", "property": "object"},
        },
    )
    for r in results.get("results", []):
        title_parts = r.get("title", [])
        title = "".join(p.get("text", {}).get("content", "") for p in title_parts)
        if title == MONITOR_DB_NAME:
            return r["id"]

    # Create
    result = await notion._client.request(
        path="databases",
        method="POST",
        body={
            "parent": {"type": "page_id", "page_id": root_page_id},
            "title": [{"type": "text", "text": {"content": MONITOR_DB_NAME}}],
            "properties": {
                "Key": {"title": {}},
                "Last Checked": {"date": {}},
                "Status": {"rich_text": {}},
                "Emails Processed": {"number": {}},
                "Clients Enriched": {"rich_text": {}},
            },
        },
    )
    db_id = result["id"]
    print(f"  Created {MONITOR_DB_NAME} DB: {db_id}")
    return db_id


async def _read_state(notion: NotionClient, db_id: str) -> dict:
    """Read monitor state (last_checked_at, etc.)."""
    rows = await notion._client.request(
        path=f"databases/{db_id}/query",
        method="POST",
        body={"page_size": 1},
    )
    if rows.get("results"):
        row = rows["results"][0]
        props = row.get("properties", {})
        date_obj = props.get("Last Checked", {}).get("date")
        last_checked = date_obj.get("start", "") if date_obj else ""
        return {
            "page_id": row["id"],
            "last_checked": last_checked,
        }
    return {}


async def _write_state(
    notion: NotionClient,
    db_id: str,
    page_id: str | None,
    last_checked: str,
    emails_processed: int,
    clients_enriched: list[str],
    status: str,
) -> None:
    props = {
        "Key": {"title": [{"text": {"content": "monitor_state"}}]},
        "Last Checked": {"date": {"start": last_checked}},
        "Status": {"rich_text": [{"text": {"content": status[:2000]}}]},
        "Emails Processed": {"number": emails_processed},
        "Clients Enriched": {"rich_text": [{"text": {"content": ", ".join(clients_enriched)[:2000]}}]},
    }

    if page_id:
        await notion._client.request(
            path=f"pages/{page_id}",
            method="PATCH",
            body={"properties": props},
        )
    else:
        await notion._client.request(
            path="pages",
            method="POST",
            body={"parent": {"database_id": db_id}, "properties": props},
        )


# ── Slack alerts ───────────────────────────────────────────────────────────────

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


def _format_slack_alert(client_name: str, flags: list[dict]) -> str:
    lines = [f"📧 *Email Monitor — {client_name}*"]
    for f in flags:
        ftype = f.get("type", "flag").upper()
        desc = f.get("description", "")
        date = f.get("source_date", "")
        lines.append(f"  [{ftype}] ({date}) {desc}")
    return "\n".join(lines)


# Keyword-based urgency detection — fires even when Claude doesn't flag
URGENCY_KEYWORDS = [
    "not working", "stopped working", "broken", "down", "outage",
    "urgent", "asap", "emergency", "critical", "immediately",
    "can't access", "cannot access", "can't log in", "cannot log in",
    "not receiving", "no one is available", "not available",
    "please help", "need help", "help!",
    "issue with", "problem with", "something wrong",
]


def _detect_urgency(subject: str, body: str) -> list[str]:
    """Return list of urgency keywords matched in subject or body."""
    combined = f"{subject}\n{body[:1500]}".lower()
    return [kw for kw in URGENCY_KEYWORDS if kw in combined]


def _format_urgent_alert(client_name: str, thread: dict, matched: list[str]) -> str:
    subject = thread.get("subject", "(no subject)")
    last_date = thread.get("last_date", "")
    direction = thread.get("direction", "inbound")
    body_preview = thread.get("body", "")[:400].strip()

    lines = [
        f"🚨 *URGENT EMAIL — {client_name}*",
        f"_{direction} | {last_date}_",
        f"*Subject:* {subject}",
        f"*Keywords:* {', '.join(matched[:5])}",
        "",
        "```",
        body_preview,
        "```",
    ]
    return "\n".join(lines)


# ── Main tick ──────────────────────────────────────────────────────────────────

async def tick(lookback_minutes: int = 15) -> None:
    if not gmail.GMAIL_REFRESH_TOKEN:
        print("⚠ GOOGLE_GMAIL_REFRESH_TOKEN not set")
        sys.exit(1)

    notion = NotionClient(api_key=settings.notion_api_key)
    now = datetime.now(timezone.utc)

    print(f"\n{'='*50}")
    print(f"  Email Monitor — {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*50}\n")

    # State
    state_db_id = await _get_or_create_state_db(notion)
    state = await _read_state(notion, state_db_id)

    if state.get("last_checked"):
        since = datetime.fromisoformat(state["last_checked"].replace("Z", "+00:00"))
        # Add 1-minute overlap to avoid missing edge-case emails
        since = since - timedelta(minutes=1)
    else:
        since = now - timedelta(minutes=lookback_minutes)
        print(f"  First run — looking back {lookback_minutes} minutes")

    since_str = since.strftime("%Y/%m/%d")
    minutes_ago = int((now - since).total_seconds() / 60)
    print(f"  Checking emails since: {since.strftime('%Y-%m-%d %H:%M UTC')} ({minutes_ago} min ago)")

    # Build client lookup maps
    domain_map = _build_domain_to_client_map()
    email_map = _build_email_to_client_map()
    print(f"  Tracking {len(domain_map)} domains + {len(email_map)} exact emails across {len(CLIENTS)} clients")

    # ONE Gmail search for everything
    token = await gmail.get_access_token()
    query = f"after:{since_str}"

    async with httpx.AsyncClient() as http:
        msg_ids = await gmail.search_messages(http, token, query, max_results=500)
        print(f"  Found {len(msg_ids)} total messages")

        if not msg_ids:
            await _write_state(notion, state_db_id, state.get("page_id"),
                               now.isoformat(), 0, [], "No new emails")
            print("  No new emails. Done.")
            return

        # Fetch all messages
        messages: list[dict] = []
        for i, mid in enumerate(msg_ids):
            try:
                messages.append(await gmail.get_message(http, token, mid))
            except Exception:
                pass
            if (i + 1) % 100 == 0:
                print(f"    Fetched {i+1}/{len(msg_ids)}")

    # Match to clients
    client_messages: dict[str, list[dict]] = {}
    unmatched = 0
    for m in messages:
        client_key = _match_message_to_client(m, domain_map, email_map)
        if client_key:
            client_messages.setdefault(client_key, []).append(m)
        else:
            unmatched += 1

    matched_clients = list(client_messages.keys())
    total_matched = sum(len(msgs) for msgs in client_messages.values())
    print(f"  Matched: {total_matched} messages → {len(matched_clients)} clients | Unmatched: {unmatched}")

    if not matched_clients:
        await _write_state(notion, state_db_id, state.get("page_id"),
                           now.isoformat(), len(msg_ids), [], "No client emails")
        print("  No client emails. Done.")
        return

    # Process each client with new mail
    clients_enriched: list[str] = []
    total_log = 0
    total_enrichments = 0
    total_flags = 0

    for client_key in matched_clients:
        cfg = CLIENTS.get(client_key)
        if not cfg:
            continue
        client_name = cfg.get("name", client_key)
        log_db_id = cfg.get("client_log_db_id", "")
        profile_id = cfg.get("business_profile_page_id", "")
        brand_db_id = cfg.get("brand_guidelines_db_id", "")

        msgs = client_messages[client_key]

        # Group into threads + filter
        threads_raw = gmail.group_threads(msgs)
        summarized: list[dict] = []
        for tid, thread in threads_raw.items():
            first = thread[0]
            headers = gmail.parse_headers(first)
            subject = headers.get("subject", "")
            from_addr = headers.get("from", "")
            body = gmail.decode_body(first.get("payload", {}))

            if gmail.is_dnl(subject, body):
                continue
            if gmail.is_automated_noise(subject, from_addr, body):
                continue
            summarized.append(gmail.summarize_thread(thread))

        if not summarized:
            continue

        # Urgency keyword safety net — fires regardless of Claude's flag decisions
        for thread in summarized:
            matched = _detect_urgency(thread.get("subject", ""), thread.get("body", ""))
            if matched and SLACK_BOT_TOKEN:
                alert = _format_urgent_alert(client_name, thread, matched)
                await _post_to_slack(alert)
                print(f"  🚨 Urgent alert sent: {thread.get('subject', '')[:60]} (matched: {', '.join(matched[:3])})")

        print(f"\n  Processing {client_name}: {len(summarized)} threads from {len(msgs)} messages")

        # Dedup against existing log
        existing_summaries, thread_map = [], {}
        if log_db_id:
            existing_summaries, thread_map = await load_existing_log_entries(notion, log_db_id, 90)

        # Filter already-logged threads
        new_threads = []
        for t in summarized:
            tid = t.get("thread_id", "")
            existing = thread_map.get(tid, {})
            if not existing:
                new_threads.append(t)
            elif t.get("message_count", 1) > existing.get("msg_count", 0):
                new_threads.append(t)
        if not new_threads:
            print(f"    All threads already logged — skipping")
            continue

        # Load existing profile for dedup
        existing_profile = ""
        if profile_id:
            existing_profile = await load_business_profile(notion, cfg)

        # Synthesize
        try:
            synth = await synthesize_threads(new_threads, client_name, existing_summaries, existing_profile)
        except Exception as e:
            print(f"    ⚠ Claude synthesis failed: {e}")
            continue

        log_entries = synth.get("log_entries", []) or []
        enrichments = synth.get("profile_enrichments", []) or []
        flags = synth.get("flags", []) or []
        rule_flags = [f for f in flags if f.get("type") == "rule_set"]
        other_flags = [f for f in flags if f.get("type") != "rule_set"]

        # Write to Notion
        if log_entries and log_db_id:
            created, updated_count = await write_client_log(notion, log_db_id, client_name, log_entries, thread_map)
            total_log += created + updated_count
            print(f"    ✓ {created} new + {updated_count} updated log entries")

        if (enrichments or other_flags) and profile_id:
            await append_profile_enrichments(notion, profile_id, enrichments, flags, minutes_ago)
            total_enrichments += len(enrichments)
            print(f"    ✓ {len(enrichments)} enrichments + {len(other_flags)} flags")

        if rule_flags and brand_db_id:
            applied = await apply_rule_set_flags(notion, brand_db_id, rule_flags)
            if applied:
                print(f"    ✓ {applied} rule_set → Brand Guidelines")

        total_flags += len(flags)
        clients_enriched.append(client_name)

        # Update Last Contact on Clients DB
        if log_entries:
            latest = max(e.get("date", "") for e in log_entries if e.get("date"))
            if latest:
                await update_last_contact(notion, client_name, latest)

        # Slack alert for flags
        if flags and SLACK_BOT_TOKEN:
            alert = _format_slack_alert(client_name, flags)
            await _post_to_slack(alert)

    # Update state
    status = f"OK — {total_log} logs, {total_enrichments} enrichments, {total_flags} flags"
    await _write_state(notion, state_db_id, state.get("page_id"),
                       now.isoformat(), total_matched, clients_enriched, status)

    print(f"\n{'='*50}")
    print(f"  Done. {len(clients_enriched)} clients enriched.")
    print(f"  {total_log} log entries | {total_enrichments} enrichments | {total_flags} flags")
    print(f"{'='*50}")


async def setup_only() -> None:
    """Create the state DB without running a tick."""
    notion = NotionClient(api_key=settings.notion_api_key)
    db_id = await _get_or_create_state_db(notion)
    print(f"✓ Email Monitor State DB: {db_id}")


async def _alert_failure(error: str) -> None:
    """Post failure alert to Slack so silent crashes are caught."""
    if not SLACK_BOT_TOKEN or not SLACK_CHANNEL:
        return
    try:
        async with httpx.AsyncClient() as http:
            await http.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
                json={"channel": SLACK_CHANNEL, "text": f"🚨 *Email Monitor Failed*\n```{error[:500]}```"},
                timeout=10.0,
            )
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Real-time email monitor")
    parser.add_argument("--setup", action="store_true", help="Create state DB only")
    parser.add_argument("--lookback", type=int, default=60,
                        help="First-run lookback in minutes (default 60)")
    args = parser.parse_args()

    if args.setup:
        asyncio.run(setup_only())
    else:
        try:
            asyncio.run(tick(lookback_minutes=args.lookback))
        except Exception as e:
            import traceback
            error = traceback.format_exc()
            print(f"\n🚨 Email Monitor crashed:\n{error}")
            asyncio.run(_alert_failure(error))


if __name__ == "__main__":
    main()
