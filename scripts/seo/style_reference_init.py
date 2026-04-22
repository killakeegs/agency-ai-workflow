#!/usr/bin/env python3
"""
style_reference_init.py — Create the Style Reference DB for a client.

The Style Reference DB is the agent feedback loop: every approval,
rejection, and edit logs here with the reason why. Agents prime their
next generation from recent entries so per-client voice compounds.

Run once per client, early in the SEO rollout (or as part of any
AI-driven pipeline for that client — the DB is agent-agnostic).

Usage:
    make style-reference-init CLIENT=summit_therapy
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.config import settings
from src.integrations.notion import NotionClient

CLIENTS_JSON_PATH = Path(__file__).parent.parent.parent / "config" / "clients.json"


async def main(client_key: str) -> None:
    from config.clients import CLIENTS
    from scripts.onboarding.setup_notion import style_reference_schema

    cfg = CLIENTS.get(client_key)
    if not cfg:
        print(f"Client '{client_key}' not found in config/clients.py")
        sys.exit(1)

    if cfg.get("style_reference_db_id"):
        print(f"Style Reference DB already exists for {cfg['name']}: {cfg['style_reference_db_id']}")
        print("Delete the DB in Notion and clear the field in clients.json to recreate.")
        return

    notion = NotionClient(settings.notion_api_key)

    # Resolve client root page via the Client Info DB's parent.
    client_page_id = None
    try:
        db = await notion._client.request(
            path=f"databases/{cfg['client_info_db_id']}", method="GET"
        )
        parent = db.get("parent", {})
        if parent.get("type") == "page_id":
            client_page_id = parent["page_id"]
    except Exception as e:
        print(f"Could not resolve client root page: {e}")
        sys.exit(1)

    if not client_page_id:
        print("Client Info DB has no page parent — cannot place Style Reference under client.")
        sys.exit(1)

    print(f"\nCreating Style Reference DB for {cfg['name']}...")
    style_reference_db_id = await notion.create_database(
        parent_page_id=client_page_id,
        title="Style Reference",
        properties_schema=style_reference_schema(),
    )
    print(f"  ✓ Style Reference DB created: {style_reference_db_id}")

    # Update clients.json if the client entry lives there.
    existing: dict = {}
    if CLIENTS_JSON_PATH.exists():
        try:
            existing = json.loads(CLIENTS_JSON_PATH.read_text()) or {}
        except json.JSONDecodeError:
            existing = {}

    if client_key in existing:
        existing[client_key]["style_reference_db_id"] = style_reference_db_id
        CLIENTS_JSON_PATH.write_text(json.dumps(existing, indent=2))
        print("  ✓ clients.json updated")
    else:
        print(
            f"  ⚠️  {client_key} is a _MANUAL entry in config/clients.py — add this manually:\n"
            f'     "style_reference_db_id": "{style_reference_db_id}",'
        )

    print(f"\nStyle Reference active for {cfg['name']}.")
    print("Agents will now read recent approved/edited entries to prime generation.")
    print("Log your first entry once an agent output ships to seed the loop.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Provision the Style Reference DB for a client")
    parser.add_argument("--client", required=True)
    args = parser.parse_args()
    asyncio.run(main(client_key=args.client))
