#!/usr/bin/env python3
"""
archive_meeting_prep.py — Mark old Meeting Prep entries as Archived.

Iterates every client's Meeting Prep DB and flips any entry with
Meeting Date older than 90 days to Status=Archived (unless it's already
archived). Keeps client pages showing only recent/upcoming prep docs
by default without losing history — archived rows stay queryable.

Intended to run daily as a Railway cron. Idempotent + safe to re-run.

Usage:
    python3 scripts/enrichment/archive_meeting_prep.py            # run for all
    python3 scripts/enrichment/archive_meeting_prep.py --dry      # preview only
    python3 scripts/enrichment/archive_meeting_prep.py --client summit_therapy
    python3 scripts/enrichment/archive_meeting_prep.py --days 60  # custom cutoff
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent.parent / ".env")

from config.clients import CLIENTS
from src.config import settings
from src.integrations.notion import NotionClient

DEFAULT_DAYS = 90


async def _archive_stale(
    notion: NotionClient,
    client_name: str,
    db_id: str,
    cutoff_iso: str,
    dry: bool,
) -> int:
    """Archive any entry in this DB with Meeting Date < cutoff_iso and Status != Archived.
    Returns the count of entries archived."""
    body = {
        "filter": {
            "and": [
                {"property": "Meeting Date", "date": {"before": cutoff_iso}},
                {"property": "Status", "select": {"does_not_equal": "Archived"}},
            ]
        },
        "page_size": 100,
    }

    archived = 0
    start_cursor: str | None = None
    while True:
        if start_cursor:
            body["start_cursor"] = start_cursor
        resp = await notion._client.request(
            path=f"databases/{db_id}/query", method="POST", body=body,
        )
        results = resp.get("results", [])
        for row in results:
            row_id = row["id"]
            title_parts = row.get("properties", {}).get("Title", {}).get("title", [])
            title = "".join(p.get("text", {}).get("content", "") for p in title_parts)
            date_obj = row.get("properties", {}).get("Meeting Date", {}).get("date") or {}
            meeting_date = date_obj.get("start", "?")
            if dry:
                print(f"    [DRY] {client_name}: would archive '{title}' ({meeting_date})")
            else:
                await notion._client.request(
                    path=f"pages/{row_id}", method="PATCH",
                    body={"properties": {"Status": {"select": {"name": "Archived"}}}},
                )
                print(f"    ✓ {client_name}: archived '{title}' ({meeting_date})")
            archived += 1

        if not resp.get("has_more"):
            break
        start_cursor = resp.get("next_cursor")
        if not start_cursor:
            break

    return archived


async def main(target: str | None, days: int, dry: bool) -> None:
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    notion = NotionClient(settings.notion_api_key)

    if target:
        if target not in CLIENTS:
            print(f"Unknown client: {target}")
            sys.exit(1)
        targets = [(target, CLIENTS[target])]
    else:
        targets = list(CLIENTS.items())

    print(f"\n{'[DRY RUN] ' if dry else ''}Archiving Meeting Prep entries with Meeting Date < {cutoff} ({days}-day window)...\n")

    total = 0
    for client_key, cfg in targets:
        db_id = cfg.get("meeting_prep_db_id")
        if not db_id:
            continue
        client_name = cfg.get("name", client_key)
        try:
            n = await _archive_stale(notion, client_name, db_id, cutoff, dry)
            total += n
        except Exception as e:
            print(f"  ✗ {client_name}: {e}")

    verb = "would archive" if dry else "Archived"
    print(f"\nDone. {verb} {total} entries across {len(targets)} client(s).")


def cli() -> None:
    parser = argparse.ArgumentParser(description="Archive old Meeting Prep entries")
    parser.add_argument("--client", default=None, help="Only run for this client_key")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help=f"Archive entries older than N days (default {DEFAULT_DAYS})")
    parser.add_argument("--dry", action="store_true", help="Preview without writing")
    args = parser.parse_args()
    asyncio.run(main(target=args.client, days=args.days, dry=args.dry))


if __name__ == "__main__":
    cli()
