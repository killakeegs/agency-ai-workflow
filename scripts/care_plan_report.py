#!/usr/bin/env python3
"""
care_plan_report.py — Monthly website care plan report

For each care plan client:
  1. Runs Google PageSpeed Insights (mobile + desktop)
  2. Writes a new monthly entry to the client's Care Plan Notion DB
  3. Posts a Slack summary

Usage:
    make care-plan                          # run report for all care plan clients
    make care-plan CLIENT=summit_therapy    # run for a specific client
    python scripts/care_plan_report.py --init --client summit_therapy
                                            # create Care Plan DB for existing client

Runs automatically on the 1st of each month via Railway cron.
PageSpeed Insights API is free — no API key required.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

from config.clients import CLIENTS
from src.config import settings
from src.integrations.notion import NotionClient

logger = logging.getLogger(__name__)

PAGESPEED_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
CLIENTS_JSON_PATH = Path(__file__).parent.parent / "config" / "clients.json"


# ── PageSpeed helpers ─────────────────────────────────────────────────────────

def _rating(score: int) -> str:
    if score >= 90:
        return "Good (90–100)"
    if score >= 50:
        return "Needs Improvement (50–89)"
    return "Poor (0–49)"


async def _run_pagespeed(url: str, strategy: str) -> tuple[int, str]:
    """
    Run PageSpeed Insights for a URL and strategy ('mobile' or 'desktop').
    Returns (score 0-100, top_opportunity description).
    """
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.get(
            PAGESPEED_URL,
            params={"url": url, "strategy": strategy},
        )

    if r.status_code != 200:
        logger.warning(f"PageSpeed API error {r.status_code} for {url} ({strategy})")
        return 0, "API error — could not retrieve results"

    data = r.json()
    lighthouse = data.get("lighthouseResult", {})

    # Score (0.0–1.0 → 0–100)
    perf = lighthouse.get("categories", {}).get("performance", {})
    score = round((perf.get("score") or 0) * 100)

    # Top opportunity — highest potential savings
    audits = lighthouse.get("audits", {})
    opportunities = []
    for audit_id, audit in audits.items():
        details = audit.get("details", {})
        if details.get("type") == "opportunity":
            savings = audit.get("numericValue", 0)
            title = audit.get("title", "")
            if title and savings > 0:
                opportunities.append((savings, title))

    opportunities.sort(reverse=True)
    top = opportunities[0][1] if opportunities else "No major opportunities found"

    return score, top


# ── Notion site URL helper ────────────────────────────────────────────────────

async def _get_site_url(notion: NotionClient, client_key: str) -> str:
    """Read the Website field from Client Info DB."""
    cfg = CLIENTS[client_key]
    entries = await notion.query_database(cfg["client_info_db_id"])
    if not entries:
        return ""
    prop = entries[0]["properties"].get("Website", {})
    return prop.get("url", "") or ""


# ── Init: create Care Plan DB for an existing client ─────────────────────────

async def init_care_plan(client_key: str) -> None:
    """
    Create the Care Plan Notion DB for a client that was onboarded before
    care plan support was added. Updates clients.json with the new DB ID.
    """
    if client_key not in CLIENTS:
        print(f"Unknown client '{client_key}'. Available: {', '.join(CLIENTS)}")
        return

    cfg = CLIENTS[client_key]
    if cfg.get("care_plan_db_id"):
        print(f"{client_key} already has a Care Plan DB: {cfg['care_plan_db_id']}")
        return

    notion = NotionClient(settings.notion_api_key)

    # Find the parent page by looking at the parent of an existing DB
    info_db = cfg["client_info_db_id"]
    db_obj = await notion._client.request(path=f"databases/{info_db}", method="GET")
    parent = db_obj.get("parent", {})
    if parent.get("type") != "page_id":
        print(f"Could not determine parent page for {client_key}")
        return
    parent_page_id = parent["page_id"]

    # Import schema
    from scripts.setup_notion import care_plan_schema
    db_id = await notion.create_database(
        parent_page_id=parent_page_id,
        title="Care Plan",
        properties_schema=care_plan_schema(),
    )
    print(f"  ✓ Care Plan DB created: {db_id}")

    # Update clients.json
    existing: dict = {}
    if CLIENTS_JSON_PATH.exists():
        try:
            existing = json.loads(CLIENTS_JSON_PATH.read_text()) or {}
        except json.JSONDecodeError:
            existing = {}

    if client_key not in existing:
        existing[client_key] = dict(cfg)
    existing[client_key]["care_plan_db_id"] = db_id
    CLIENTS_JSON_PATH.write_text(json.dumps(existing, indent=2))
    print(f"  ✓ clients.json updated with care_plan_db_id")
    print(f"\nNow run: make care-plan CLIENT={client_key}")


# ── Report: run PageSpeed + write to Notion ───────────────────────────────────

async def run_report(client_key: str | None = None) -> None:
    notion = NotionClient(settings.notion_api_key)
    today = date.today()
    month_label = today.strftime("%B %Y")   # e.g. "April 2026"
    date_iso = today.isoformat()            # e.g. "2026-04-09"

    # Determine which clients to run
    targets = {}
    for key, cfg in CLIENTS.items():
        if client_key and key != client_key:
            continue
        if cfg.get("care_plan_db_id"):
            targets[key] = cfg

    if not targets:
        if client_key:
            print(f"No care_plan_db_id found for '{client_key}'.")
            print(f"Run: python scripts/care_plan_report.py --init --client {client_key}")
        else:
            print("No clients with care plan configured. Run with --init --client <key> first.")
        return

    print(f"\nCare Plan Report — {month_label}")
    print("=" * 60)

    slack_lines = [f"*Care Plan Report — {month_label}*\n"]
    all_ok = True

    for key, cfg in targets.items():
        client_name = cfg.get("name", key)
        care_plan_db_id = cfg["care_plan_db_id"]

        print(f"\n{client_name}")
        print("-" * 40)

        # Get site URL from Client Info DB
        site_url = await _get_site_url(notion, key)
        if not site_url:
            print(f"  ⚠ No site URL found in Client Info DB — skipping")
            slack_lines.append(f"• *{client_name}* — ⚠ No site URL configured")
            all_ok = False
            continue

        print(f"  URL: {site_url}")

        # Run PageSpeed for both strategies
        print("  Running PageSpeed... ", end="", flush=True)
        mobile_score, mobile_top = await _run_pagespeed(site_url, "mobile")
        desktop_score, desktop_top = await _run_pagespeed(site_url, "desktop")
        print(f"mobile {mobile_score} / desktop {desktop_score}")

        # Write to Notion Care Plan DB
        entry_name = f"{client_name} — {month_label}"
        props = {
            "Name":            notion.title_property(entry_name),
            "Report Date":     {"date": {"start": date_iso}},
            "Site URL":        {"url": site_url},
            "Mobile Score":    {"number": mobile_score},
            "Desktop Score":   {"number": desktop_score},
            "Mobile Rating":   {"select": {"name": _rating(mobile_score)}},
            "Desktop Rating":  {"select": {"name": _rating(desktop_score)}},
            "Top Opportunity": notion.text_property(mobile_top[:2000]),
        }
        entry_id = await notion.create_database_entry(care_plan_db_id, props)
        print(f"  ✓ Written to Notion: {entry_id}")

        # Slack emoji for score
        def _emoji(s: int) -> str:
            return "🟢" if s >= 90 else ("🟡" if s >= 50 else "🔴")

        slack_lines.append(
            f"• *{client_name}* — "
            f"Mobile {_emoji(mobile_score)} {mobile_score}  |  "
            f"Desktop {_emoji(desktop_score)} {desktop_score}\n"
            f"  _{mobile_top}_"
        )

        if mobile_score < 50 or desktop_score < 50:
            all_ok = False

    # Post to Slack
    slack_token = settings.slack_bot_token if hasattr(settings, "slack_bot_token") else ""
    slack_channel = "#agency-pipeline"

    if slack_token:
        summary = "\n".join(slack_lines)
        if all_ok:
            summary += "\n\n✅ All sites looking healthy."
        else:
            summary += "\n\n⚠ One or more sites need attention."

        async with httpx.AsyncClient(timeout=10) as http:
            await http.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {slack_token}"},
                json={"channel": slack_channel, "text": summary},
            )
        print(f"\n✓ Slack summary posted to {slack_channel}")
    else:
        print("\n(SLACK_BOT_TOKEN not set — skipping Slack notification)")

    print(f"\n{'='*60}")
    print(f"Done — {len(targets)} client(s) reported.")


# ── CLI ───────────────────────────────────────────────────────────────────────

async def main() -> None:
    logging.basicConfig(level=logging.WARNING)

    parser = argparse.ArgumentParser(description="Website care plan monthly report")
    parser.add_argument("--client", default="", help="Run for a specific client key only")
    parser.add_argument(
        "--init", action="store_true",
        help="Create Care Plan DB for an existing client (requires --client)",
    )
    args = parser.parse_args()

    if args.init:
        if not args.client:
            print("--init requires --client <client_key>")
            sys.exit(1)
        await init_care_plan(args.client)
    else:
        await run_report(args.client or None)


if __name__ == "__main__":
    asyncio.run(main())
