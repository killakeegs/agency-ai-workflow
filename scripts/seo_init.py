#!/usr/bin/env python3
"""
seo_init.py — Create Battle Plan DBs for an existing client

Creates Competitors DB and Keywords DB in Notion for a client that was
onboarded before these schemas existed. Updates clients.json with the new
DB IDs.

New clients onboarded via `make onboard` get these DBs automatically.
This is the one-time retroactive setup for older clients.

Usage:
    make seo-init CLIENT=summit_therapy
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings
from src.integrations.notion import NotionClient

CLIENTS_JSON_PATH = Path(__file__).parent.parent / "config" / "clients.json"


async def main(client_key: str) -> None:
    from config.clients import CLIENTS
    from scripts.setup_notion import competitors_schema, keywords_schema

    cfg = CLIENTS.get(client_key)
    if not cfg:
        print(f"Client '{client_key}' not found in config/clients.py")
        sys.exit(1)

    # Check if already done
    if cfg.get("competitors_db_id") and cfg.get("keywords_db_id"):
        print(f"Competitors and Keywords DBs already exist for {cfg['name']}.")
        print(f"  competitors_db_id: {cfg['competitors_db_id']}")
        print(f"  keywords_db_id:    {cfg['keywords_db_id']}")
        return

    notion = NotionClient(settings.notion_api_key)

    # Resolve client root page from Client Info DB parent
    client_page_id = None
    try:
        db = await notion._client.request(
            path=f"databases/{cfg['client_info_db_id']}", method="GET"
        )
        parent = db.get("parent", {})
        if parent.get("type") == "page_id":
            client_page_id = parent["page_id"]
    except Exception as e:
        print(f"Could not resolve client Notion page: {e}")
        sys.exit(1)

    print(f"\nCreating Battle Plan DBs for {cfg['name']}...")

    competitors_db_id = ""
    keywords_db_id = ""

    if not cfg.get("competitors_db_id"):
        competitors_db_id = await notion.create_database(
            parent_page_id=client_page_id,
            title="Competitors",
            properties_schema=competitors_schema(),
        )
        print(f"  ✓ Competitors DB: {competitors_db_id}")
    else:
        competitors_db_id = cfg["competitors_db_id"]
        print(f"  — Competitors DB already exists: {competitors_db_id}")

    if not cfg.get("keywords_db_id"):
        keywords_db_id = await notion.create_database(
            parent_page_id=client_page_id,
            title="Keywords",
            properties_schema=keywords_schema(),
        )
        print(f"  ✓ Keywords DB:    {keywords_db_id}")
    else:
        keywords_db_id = cfg["keywords_db_id"]
        print(f"  — Keywords DB already exists: {keywords_db_id}")

    # Update clients.json
    existing: dict = {}
    if CLIENTS_JSON_PATH.exists():
        try:
            existing = json.loads(CLIENTS_JSON_PATH.read_text()) or {}
        except json.JSONDecodeError:
            existing = {}

    if client_key in existing:
        existing[client_key]["competitors_db_id"] = competitors_db_id
        existing[client_key]["keywords_db_id"] = keywords_db_id
        CLIENTS_JSON_PATH.write_text(json.dumps(existing, indent=2))
        print(f"  ✓ clients.json updated")
    else:
        print(
            f"\n  ⚠️  {client_key} not in clients.json — add these manually:\n"
            f'     "competitors_db_id": "{competitors_db_id}",\n'
            f'     "keywords_db_id": "{keywords_db_id}"'
        )

    print(f"\nDone. Next steps:")
    print(f"  1. make battle-plan-init CLIENT={client_key}  — create input checklist in Notion")
    print(f"  2. Fill in Competitors DB and Keywords DB rows in Notion")
    print(f"  3. make battle-plan CLIENT={client_key}       — generate the battle plan")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Create Battle Plan DBs for an existing client"
    )
    parser.add_argument("--client", required=True, help="Client key (e.g. summit_therapy)")
    args = parser.parse_args()

    asyncio.run(main(client_key=args.client))
