#!/usr/bin/env python3
"""
add_meeting_prep_dbs.py — Provision a Meeting Prep DB for every existing client.

Per-client Meeting Prep DBs — one row per scheduled meeting, auto-populated
by the morning briefing cron. Placed on the client's page alongside
Client Log / Brand Guidelines / etc.

Idempotent: skips any client that already has `meeting_prep_db_id` set in
clients.json. Writes new IDs back to clients.json.

Usage:
    python3 scripts/setup/add_meeting_prep_dbs.py           # provision all
    python3 scripts/setup/add_meeting_prep_dbs.py --dry     # preview only
    python3 scripts/setup/add_meeting_prep_dbs.py --client summit_therapy  # just one
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent.parent / ".env")

from src.config import settings
from src.integrations.notion import NotionClient
from config.clients import CLIENTS
from scripts.onboarding.setup_notion import meeting_prep_schema

CLIENTS_JSON_PATH = Path(__file__).parent.parent.parent / "config" / "clients.json"


async def _resolve_client_page_id(notion: NotionClient, cfg: dict) -> str | None:
    """
    Find the client's parent page by walking up from their Client Info DB.
    (CLIENTS config doesn't store client_page_id directly — we derive it.)
    """
    client_info_db = cfg.get("client_info_db_id")
    if not client_info_db:
        return None
    try:
        db = await notion._client.request(
            path=f"databases/{client_info_db}",
            method="GET",
        )
    except Exception:
        return None
    parent = db.get("parent", {})
    if parent.get("type") == "page_id":
        return parent["page_id"]
    return None


async def _provision_one(
    notion: NotionClient,
    client_key: str,
    cfg: dict,
    dry: bool,
) -> str | None:
    """
    Create a Meeting Prep DB for one client if they don't already have one.
    Returns the new DB ID (or existing one), or None if skipped.
    """
    client_name = cfg.get("name", client_key)

    existing = cfg.get("meeting_prep_db_id", "")
    if existing:
        print(f"  [skip] {client_name} — already has Meeting Prep DB: {existing}")
        return existing

    page_id = await _resolve_client_page_id(notion, cfg)
    if not page_id:
        print(f"  [skip] {client_name} — could not resolve client page (missing client_info_db_id?)")
        return None

    db_prefix = client_name.split()[0] if client_name else "Client"
    title = f"{db_prefix} — Meeting Prep"

    if dry:
        print(f"  [DRY] {client_name} — would create '{title}' under {page_id}")
        return None

    db_id = await notion.create_database(
        parent_page_id=page_id,
        title=title,
        properties_schema=meeting_prep_schema(),
    )
    print(f"  ✓ {client_name} — Meeting Prep DB created: {db_id}")
    return db_id


def _write_clients_json_update(updates: dict[str, str]) -> None:
    """Merge new meeting_prep_db_id values into clients.json. Manual clients in
    _MANUAL are not persisted here — they live in config/clients.py."""
    if not updates:
        return
    if not CLIENTS_JSON_PATH.exists():
        print(f"  (no clients.json at {CLIENTS_JSON_PATH} — skipping persistence)")
        return
    try:
        data = json.loads(CLIENTS_JSON_PATH.read_text())
    except json.JSONDecodeError:
        print("  (clients.json is malformed — skipping persistence)")
        return

    written = 0
    for client_key, db_id in updates.items():
        if client_key in data:
            data[client_key]["meeting_prep_db_id"] = db_id
            written += 1
    CLIENTS_JSON_PATH.write_text(json.dumps(data, indent=2))
    print(f"\n  ✓ clients.json updated ({written} entries)")

    manual_only = [k for k in updates if k not in data]
    if manual_only:
        print(f"\n  ⚠ These clients live in _MANUAL (config/clients.py) — add manually:")
        for k in manual_only:
            print(f"      {k}:  \"meeting_prep_db_id\": \"{updates[k]}\",")


async def main(target: str | None, dry: bool) -> None:
    notion = NotionClient(settings.notion_api_key)

    if target:
        if target not in CLIENTS:
            print(f"Unknown client: {target}")
            sys.exit(1)
        targets = [(target, CLIENTS[target])]
    else:
        targets = list(CLIENTS.items())

    print(f"\n{'[DRY RUN] ' if dry else ''}Provisioning Meeting Prep DBs for {len(targets)} client(s)...\n")
    updates: dict[str, str] = {}
    for client_key, cfg in targets:
        if cfg.get("internal"):
            print(f"  [skip] {cfg.get('name', client_key)} — internal client")
            continue
        try:
            db_id = await _provision_one(notion, client_key, cfg, dry)
        except Exception as e:
            print(f"  ✗ {cfg.get('name', client_key)} — failed: {e}")
            continue
        if db_id and not cfg.get("meeting_prep_db_id"):
            updates[client_key] = db_id

    if not dry:
        _write_clients_json_update(updates)

    print(f"\nDone. Provisioned {len(updates)} new Meeting Prep DB(s).")


def cli() -> None:
    parser = argparse.ArgumentParser(description="Provision Meeting Prep DBs for existing clients")
    parser.add_argument("--client", default=None, help="Only run for this client_key")
    parser.add_argument("--dry", action="store_true", help="Preview without creating anything")
    args = parser.parse_args()
    asyncio.run(main(target=args.client, dry=args.dry))


if __name__ == "__main__":
    cli()
