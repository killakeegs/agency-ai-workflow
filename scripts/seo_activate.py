#!/usr/bin/env python3
"""
seo_activate.py — Activate the full SEO retainer for a client

Creates the SEO Metrics DB in Notion and updates clients.json with:
  - seo_metrics_db_id
  - gbp_location_id (optional — can be added later)
  - updates services list to include "seo"

Usage:
    make seo-activate CLIENT=summit_therapy
    make seo-activate CLIENT=summit_therapy GBP_ID="locations/123456789"
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


async def main(client_key: str, gbp_location_id: str = "") -> None:
    from config.clients import CLIENTS
    from scripts.setup_notion import seo_metrics_schema

    cfg = CLIENTS.get(client_key)
    if not cfg:
        print(f"Client '{client_key}' not found in config/clients.py")
        sys.exit(1)

    if "seo" in cfg.get("services", []):
        print(f"SEO is already active for {cfg['name']}.")
        if not gbp_location_id:
            print("Pass GBP_ID=... to update the GBP location ID.")
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
        print(f"Could not resolve client page: {e}")
        sys.exit(1)

    # Create SEO Metrics DB
    print(f"\nActivating full SEO retainer for {cfg['name']}...")
    seo_metrics_db_id = await notion.create_database(
        parent_page_id=client_page_id,
        title="SEO Metrics",
        properties_schema=seo_metrics_schema(),
    )
    print(f"  ✓ SEO Metrics DB created: {seo_metrics_db_id}")

    # Update clients.json
    existing: dict = {}
    if CLIENTS_JSON_PATH.exists():
        try:
            existing = json.loads(CLIENTS_JSON_PATH.read_text()) or {}
        except json.JSONDecodeError:
            existing = {}

    if client_key in existing:
        entry = existing[client_key]
        services = entry.get("services", [])
        if "seo" not in services:
            services.append("seo")
            entry["services"] = services
        entry["seo_metrics_db_id"] = seo_metrics_db_id
        if gbp_location_id:
            entry["gbp_location_id"] = gbp_location_id
        existing[client_key] = entry
        CLIENTS_JSON_PATH.write_text(json.dumps(existing, indent=2))
        print(f"  ✓ clients.json updated")
    else:
        print(
            f"  ⚠️  {client_key} not found in clients.json — update manually:\n"
            f'     "seo_metrics_db_id": "{seo_metrics_db_id}"'
        )

    if not gbp_location_id:
        print("\n  ⚠️  GBP location ID not provided. Add it later:")
        print(f"  make seo-activate CLIENT={client_key} GBP_ID=\"locations/YOUR_ID\"")

    print(f"\nSEO retainer active for {cfg['name']}.")
    print("Next: add competitor rows to the Competitors DB, then run `make battle-plan-init`.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Activate full SEO retainer for a client")
    parser.add_argument("--client", required=True)
    parser.add_argument("--gbp-location-id", default="", help="GBP location ID for API calls")
    args = parser.parse_args()

    asyncio.run(main(
        client_key=args.client,
        gbp_location_id=args.gbp_location_id,
    ))
