#!/usr/bin/env python3
"""
migrate_approval_workflow.py — add approval lifecycle to Cielo's DBs.

One-shot migration. Idempotent (safe to re-run).

1. Keywords DB: ensure Status select has Proposed + Dismissed options
   (existing Target / Ranking / Won preserved)
2. Competitors DB: ensure a new Status select field exists with
   Proposed / Active / Dismissed options
3. Retroactively flip Pass A's Priority=Medium keywords from
   Status=Target → Status=Proposed (system proposals awaiting review)
4. Mark all existing competitor rows as Status=Active
   (Andrea's manually-curated set IS approved)

After this runs:
  - Keyword reviewers filter Status=Proposed to see Pass A/B/C queue
  - Dropping Status=Target into a view shows only team-approved keywords
  - Same pattern on competitors — Status=Active = approved, Proposed = auto-discovered

Usage:
    python3 scripts/seo/migrate_approval_workflow.py --dry-run
    python3 scripts/seo/migrate_approval_workflow.py
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.config import settings
from src.integrations.notion import NotionClient


CLIENT_KEY = "cielo_treatment_center"

# Target schemas for Status fields
KEYWORDS_STATUS_OPTIONS = [
    {"name": "Proposed",  "color": "blue"},
    {"name": "Target",    "color": "gray"},
    {"name": "Ranking",   "color": "yellow"},
    {"name": "Won",       "color": "green"},
    {"name": "Dismissed", "color": "red"},
]

COMPETITORS_STATUS_OPTIONS = [
    {"name": "Proposed",  "color": "blue"},
    {"name": "Active",    "color": "green"},
    {"name": "Dismissed", "color": "red"},
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _rt(prop: dict) -> str:
    return "".join(p.get("text", {}).get("content", "") for p in (prop or {}).get("rich_text", []))


def _select_name(prop: dict | None) -> str:
    if not prop:
        return ""
    sel = prop.get("select")
    return sel.get("name", "") if sel else ""


# ── Step 1: Keywords DB — ensure Status options ───────────────────────────────

async def migrate_keywords_status_options(notion: NotionClient, keywords_db_id: str, dry_run: bool) -> None:
    db = await notion._client.request(path=f"databases/{keywords_db_id}", method="GET")
    status_prop = db.get("properties", {}).get("Status", {})
    current_options = status_prop.get("select", {}).get("options", [])
    current_names = {o["name"] for o in current_options}

    to_add = [o for o in KEYWORDS_STATUS_OPTIONS if o["name"] not in current_names]
    if not to_add:
        print("  ✓ Keywords DB Status already has all required options")
        return
    print(f"  → Keywords DB Status missing: {[o['name'] for o in to_add]}")
    if dry_run:
        print("  [DRY] Would PATCH Status options")
        return
    merged = list(current_options) + to_add
    await notion._client.request(
        path=f"databases/{keywords_db_id}",
        method="PATCH",
        body={"properties": {"Status": {"select": {"options": merged}}}},
    )
    print("  ✓ Keywords DB Status options patched")


# ── Step 2: Competitors DB — ensure Status field exists ───────────────────────

async def migrate_competitors_status_field(notion: NotionClient, competitors_db_id: str, dry_run: bool) -> None:
    db = await notion._client.request(path=f"databases/{competitors_db_id}", method="GET")
    existing = db.get("properties", {})
    if "Status" in existing:
        # Check if options are complete
        current_options = existing["Status"].get("select", {}).get("options", [])
        current_names = {o["name"] for o in current_options}
        to_add = [o for o in COMPETITORS_STATUS_OPTIONS if o["name"] not in current_names]
        if not to_add:
            print("  ✓ Competitors DB Status field already has all required options")
            return
        print(f"  → Competitors DB Status missing: {[o['name'] for o in to_add]}")
        if dry_run:
            print("  [DRY] Would PATCH Status options")
            return
        merged = list(current_options) + to_add
        await notion._client.request(
            path=f"databases/{competitors_db_id}",
            method="PATCH",
            body={"properties": {"Status": {"select": {"options": merged}}}},
        )
        print("  ✓ Competitors DB Status options patched")
    else:
        if dry_run:
            print("  [DRY] Would ADD Status field to Competitors DB")
            return
        await notion._client.request(
            path=f"databases/{competitors_db_id}",
            method="PATCH",
            body={"properties": {"Status": {"select": {"options": COMPETITORS_STATUS_OPTIONS}}}},
        )
        print("  ✓ Competitors DB Status field added")


# ── Step 3: Flip Pass A keywords (Priority=Medium) Target → Proposed ─────────

async def flip_pass_a_keywords(notion: NotionClient, keywords_db_id: str, dry_run: bool) -> int:
    """
    Retroactively move Priority=Medium keywords from Status=Target to
    Status=Proposed. These are the Pass A long-tail candidates; by design
    they should sit at Proposed until Andrea approves.
    """
    entries = await notion.query_database(database_id=keywords_db_id)
    flipped = 0
    for e in entries:
        props = e["properties"]
        priority = _select_name(props.get("Priority"))
        status   = _select_name(props.get("Status"))
        if priority == "Medium" and status == "Target":
            title = "".join(
                p.get("text", {}).get("content", "")
                for p in props.get("Keyword", {}).get("title", [])
            )
            if dry_run:
                print(f"  [DRY] would flip: {title}")
                flipped += 1
                continue
            await notion.update_database_entry(
                page_id=e["id"],
                properties={"Status": {"select": {"name": "Proposed"}}},
            )
            print(f"  ✓ flipped: {title}")
            flipped += 1
    print(f"  → {flipped} keyword(s) moved Target → Proposed")
    return flipped


# ── Step 4: Mark existing competitors as Active ──────────────────────────────

async def mark_competitors_active(notion: NotionClient, competitors_db_id: str, dry_run: bool) -> int:
    entries = await notion.query_database(database_id=competitors_db_id)
    updated = 0
    for e in entries:
        props = e["properties"]
        name_items = props.get("Competitor Name", {}).get("title", [])
        name = "".join(p.get("text", {}).get("content", "") for p in name_items)
        existing_status = _select_name(props.get("Status"))
        if existing_status:
            print(f"  ↳ {name}: already Status={existing_status}, leaving alone")
            continue
        if dry_run:
            print(f"  [DRY] would set Active: {name}")
            updated += 1
            continue
        await notion.update_database_entry(
            page_id=e["id"],
            properties={"Status": {"select": {"name": "Active"}}},
        )
        print(f"  ✓ Active: {name}")
        updated += 1
    return updated


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(dry_run: bool) -> None:
    from config.clients import CLIENTS
    cfg = CLIENTS[CLIENT_KEY]
    keywords_db_id    = cfg["keywords_db_id"]
    competitors_db_id = cfg["competitors_db_id"]

    notion = NotionClient(settings.notion_api_key)

    print(f"\n── Approval workflow migration {'[DRY RUN]' if dry_run else ''} ──\n")

    print("[1/4] Keywords DB — Status options")
    await migrate_keywords_status_options(notion, keywords_db_id, dry_run)

    print("\n[2/4] Competitors DB — Status field")
    await migrate_competitors_status_field(notion, competitors_db_id, dry_run)

    print("\n[3/4] Pass A keywords — flip Target → Proposed")
    flipped = await flip_pass_a_keywords(notion, keywords_db_id, dry_run)

    print("\n[4/4] Existing competitors — mark Active")
    activated = await mark_competitors_active(notion, competitors_db_id, dry_run)

    print(f"\n── Summary ──")
    print(f"  Keywords flipped Target → Proposed: {flipped}")
    print(f"  Competitors marked Active:          {activated}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run))
