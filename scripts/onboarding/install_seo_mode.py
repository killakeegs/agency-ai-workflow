#!/usr/bin/env python3
"""
install_seo_mode.py — One-shot: add SEO Mode column to Clients DB + populate.

Two things in one script (safe to re-run, idempotent):

  1. Self-heal the top-level Clients DB — add "SEO Mode" select field
     (Local / National / Hybrid) if it doesn't already exist.

  2. Populate SEO Mode on every existing Clients DB row using defaults
     derived from the client's vertical + services in config/clients.json.
     Mirrors the value into clients.json at the same time.

SEO Mode is an SEO-specific branching directive — distinct from
Client Info → Business Type (which drives agent prompt context).
They usually align (Local Biz → Local SEO) but can legitimately
diverge (e.g. telehealth = National biz + Hybrid SEO).

Usage:
    python3 scripts/onboarding/install_seo_mode.py --dry-run    # preview
    python3 scripts/onboarding/install_seo_mode.py              # live
    python3 scripts/onboarding/install_seo_mode.py --client cielo_treatment_center  # one
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv
load_dotenv(".env")

from src.config import settings
from src.integrations.notion import NotionClient


CLIENTS_JSON_PATH = Path(__file__).parent.parent.parent / "config" / "clients.json"


# ── Default mapping: clients.json → SEO Mode ──────────────────────────────────

# Verticals that are inherently local (physical location matters, patient
# proximity drives booking).
LOCAL_VERTICALS = {
    "addiction_treatment",
    "speech_pathology",
    "occupational_therapy",
    "physical_therapy",
    "mental_health",
    "dermatology",
    # "telehealth" goes to hybrid by default (see below) — NOT here.
}

# Verticals or service mixes that are hybrid (national brand + local search).
HYBRID_VERTICALS = {
    "telehealth",
}


# Per-client overrides set by Keegan 2026-04-22 during initial install.
# These take precedence over the vertical-based default. After the initial
# install, team manages SEO Mode from the Clients DB directly and re-runs
# this script only to re-sync clients.json to the Notion truth.
CLIENT_OVERRIDES: dict[str, str] = {
    "wellwell":                           "Hybrid",    # dermatology + telehealth, multi-state
    "rxmedia":                            "National",  # the agency itself
    "wellness_works_management_partners": "National",
    "resilient_solutions":                "National",
    "team_recovery":                      "National",
}


def derive_seo_mode(cfg: dict, client_key: str = "") -> str:
    """
    Default SEO Mode from overrides → verticals → services.

    Rules (keep simple, easy to adjust in Notion UI):
      - CLIENT_OVERRIDES entry → use that
      - any HYBRID_VERTICALS entry → Hybrid
      - any LOCAL_VERTICALS entry  → Local
      - otherwise → Local (healthcare roster is local-dominant)

    Team overrides in Notion; this is just first-pass best guess.
    """
    if client_key and client_key in CLIENT_OVERRIDES:
        return CLIENT_OVERRIDES[client_key]

    verticals = cfg.get("vertical", []) or []
    if not isinstance(verticals, list):
        verticals = [verticals]
    v_set = {str(v).strip().lower() for v in verticals}

    if v_set & HYBRID_VERTICALS:
        return "Hybrid"
    if v_set & LOCAL_VERTICALS:
        return "Local"
    return "Local"  # conservative default for this agency's roster


# ── Self-heal (add SEO Mode column) ───────────────────────────────────────────

SEO_MODE_PROPERTY = {
    "select": {
        "options": [
            {"name": "Local",    "color": "blue"},
            {"name": "National", "color": "purple"},
            {"name": "Hybrid",   "color": "orange"},
        ]
    }
}


async def ensure_seo_mode_column(notion: NotionClient, clients_db_id: str, dry_run: bool) -> None:
    db = await notion._client.request(path=f"databases/{clients_db_id}", method="GET")
    if "SEO Mode" in db.get("properties", {}):
        print("  ✓ SEO Mode column already present on Clients DB")
        return
    if dry_run:
        print("  [DRY] Would PATCH Clients DB to add SEO Mode select column")
        return
    await notion._client.request(
        path=f"databases/{clients_db_id}",
        method="PATCH",
        body={"properties": {"SEO Mode": SEO_MODE_PROPERTY}},
    )
    print("  ✓ Patched Clients DB — added SEO Mode column")


# ── Populate (one row per client) ─────────────────────────────────────────────

async def populate_clients_db(
    notion: NotionClient,
    clients_db_id: str,
    only_client: str | None,
    dry_run: bool,
) -> dict[str, int]:
    """
    For every existing Clients DB row (agency roster), set SEO Mode if
    it's unset, based on the default derived from clients.json.

    Also mirrors the chosen mode into clients.json as cfg["seo_mode"]
    so runtime scripts don't need to hit the Clients DB on every call.
    """
    from config.clients import CLIENTS

    entries = await notion.query_database(database_id=clients_db_id)

    # Build lookup: Client Name (lowercased) → row
    name_to_row: dict[str, dict] = {}
    for e in entries:
        title_items = e["properties"].get("Client Name", {}).get("title", [])
        name = "".join(p.get("text", {}).get("content", "") for p in title_items).strip()
        if name:
            name_to_row[name.lower()] = e

    # Read current clients.json so we can write seo_mode into it
    json_data = json.loads(CLIENTS_JSON_PATH.read_text()) if CLIENTS_JSON_PATH.exists() else {}

    counts = {"updated": 0, "already_set": 0, "missing_in_db": 0, "json_mirrored": 0}

    for client_key, cfg in CLIENTS.items():
        if only_client and client_key != only_client:
            continue

        client_name = cfg.get("name", client_key)
        default_mode = derive_seo_mode(cfg, client_key)

        row = name_to_row.get(client_name.lower())
        if not row:
            print(f"  ⚠ {client_name}: not found in Clients DB — skipping (add the row manually first)")
            counts["missing_in_db"] += 1
            continue

        # Does the row already have a SEO Mode set?
        existing_mode_prop = row["properties"].get("SEO Mode", {})
        existing_mode = ""
        if existing_mode_prop and existing_mode_prop.get("select"):
            existing_mode = existing_mode_prop["select"].get("name", "")

        if existing_mode:
            print(f"  ↳ {client_name}: already Set ({existing_mode}) — keeping, mirroring to clients.json")
            counts["already_set"] += 1
            target_mode = existing_mode
        else:
            target_mode = default_mode
            if dry_run:
                print(f"  [DRY] {client_name}: would set SEO Mode = {target_mode}")
                counts["updated"] += 1
            else:
                await notion.update_database_entry(
                    page_id=row["id"],
                    properties={"SEO Mode": {"select": {"name": target_mode}}},
                )
                print(f"  ✓ {client_name}: set SEO Mode = {target_mode}")
                counts["updated"] += 1

        # Mirror to clients.json
        if client_key in json_data:
            current = json_data[client_key].get("seo_mode")
            mode_key = target_mode.lower()  # stored lowercase in clients.json for script use
            if current != mode_key:
                json_data[client_key]["seo_mode"] = mode_key
                counts["json_mirrored"] += 1

    if not dry_run:
        CLIENTS_JSON_PATH.write_text(json.dumps(json_data, indent=2))
        print(f"  ✓ clients.json updated: {counts['json_mirrored']} seo_mode fields synced")
    else:
        print(f"  [DRY] Would sync {counts['json_mirrored']} seo_mode fields to clients.json")

    return counts


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(only_client: str | None, dry_run: bool) -> None:
    clients_db_id = os.getenv("NOTION_CLIENTS_DB_ID") or settings.notion_clients_db_id
    if not clients_db_id:
        print("✗ NOTION_CLIENTS_DB_ID not set in .env")
        sys.exit(1)

    notion = NotionClient(settings.notion_api_key)

    print(f"\n── SEO Mode install {'[DRY RUN]' if dry_run else ''} ──")
    print(f"  Clients DB: {clients_db_id}\n")

    print("[1/2] Ensure SEO Mode column exists on Clients DB")
    await ensure_seo_mode_column(notion, clients_db_id, dry_run)

    print("\n[2/2] Populate SEO Mode on each client row")
    counts = await populate_clients_db(notion, clients_db_id, only_client, dry_run)

    print(f"\n── Summary ──")
    print(f"  Updated:         {counts['updated']}")
    print(f"  Already set:     {counts['already_set']}")
    print(f"  Missing in DB:   {counts['missing_in_db']}")
    print(f"  clients.json:    {counts['json_mirrored']} fields mirrored")
    print()
    print("Next: Andrea / Keegan open the Clients DB in Notion and adjust any")
    print("SEO Mode values where the default doesn't fit (e.g. a national SaaS,")
    print("or a hybrid telehealth). Re-run this script anytime to re-sync.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Add + populate SEO Mode on Clients DB")
    parser.add_argument("--client", help="limit to one client_key (e.g. cielo_treatment_center)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(only_client=args.client, dry_run=args.dry_run))
