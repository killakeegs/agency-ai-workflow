#!/usr/bin/env python3
"""
backfill_flags_db.py — Migrate existing flag bullets out of Business Profile pages
into the workspace Flags DB.

Scans every non-internal client's Business Profile page for "Email Enrichment —
<date>" sections with a "Flags — Needs Attention" sub-section, parses the bullet
items (format: "[TYPE] (YYYY-MM-DD) description"), and creates one Flags DB entry
per bullet with Status=Open and Source=Email.

Idempotent: dedupes against existing Open flags for the same client by description.

Usage:
    python3 scripts/setup/backfill_flags_db.py          # dry run — prints what would be created
    python3 scripts/setup/backfill_flags_db.py COMMIT=1 # actually create flags
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent.parent / ".env")

from config.clients import CLIENTS
from src.config import settings
from src.integrations.notion import NotionClient
from src.services.email_enrichment import write_flags_to_db

FLAGS_DB_ID = os.environ.get("NOTION_FLAGS_DB_ID", "").strip()
BULLET_RE = re.compile(r"^\[([A-Z_]+)\]\s*\((\d{4}-\d{2}-\d{2})\)\s*(.+)$")


async def _read_profile_bullets(notion: NotionClient, profile_id: str) -> list[dict]:
    """Return list of {type, date, description} parsed from Email Enrichment flag bullets."""
    flags: list[dict] = []
    cursor: str | None = None
    in_enrichment = False
    in_flags = False

    while True:
        params = "?page_size=100"
        if cursor:
            params += f"&start_cursor={cursor}"
        try:
            r = await notion._client.request(
                path=f"blocks/{profile_id}/children{params}", method="GET",
            )
        except Exception:
            return flags

        for b in r.get("results", []):
            btype = b.get("type", "")
            if btype == "heading_2":
                text = "".join(p.get("text", {}).get("content", "") for p in b.get("heading_2", {}).get("rich_text", []))
                in_enrichment = "Email Enrichment" in text
                in_flags = False
            elif btype == "heading_3" and in_enrichment:
                text = "".join(p.get("text", {}).get("content", "") for p in b.get("heading_3", {}).get("rich_text", []))
                in_flags = "Flags" in text
            elif btype == "bulleted_list_item" and in_enrichment and in_flags:
                text = "".join(p.get("text", {}).get("content", "") for p in b.get("bulleted_list_item", {}).get("rich_text", []))
                m = BULLET_RE.match(text.strip())
                if not m:
                    continue
                flag_type, date_str, desc = m.group(1), m.group(2), m.group(3).strip()
                flags.append({
                    "type": flag_type.lower(),
                    "source_date": date_str,
                    "description": desc,
                })

        if not r.get("has_more"):
            break
        cursor = r.get("next_cursor")

    return flags


async def run(commit: bool) -> None:
    if not FLAGS_DB_ID:
        print("⚠ NOTION_FLAGS_DB_ID not set. Run scripts/setup/setup_flags_db.py first.")
        sys.exit(1)

    notion = NotionClient(api_key=settings.notion_api_key)

    total_found = 0
    total_created = 0

    for client_key, cfg in CLIENTS.items():
        if cfg.get("internal"):
            continue
        profile_id = cfg.get("business_profile_page_id", "")
        if not profile_id:
            continue

        client_name = cfg.get("name", client_key)
        flags = await _read_profile_bullets(notion, profile_id)
        if not flags:
            continue

        # Dedupe within the profile (same bullet repeated across enrichment runs)
        seen: set[str] = set()
        unique: list[dict] = []
        for f in flags:
            key = f["description"].strip().lower()[:200]
            if key in seen:
                continue
            seen.add(key)
            unique.append(f)

        total_found += len(unique)
        print(f"\n{client_name} ({client_key}): {len(unique)} unique flag bullets ({len(flags)} total)")
        for f in unique[:10]:
            print(f"  [{f['type'].upper()}] ({f['source_date']}) {f['description'][:100]}")
        if len(unique) > 10:
            print(f"  ... +{len(unique) - 10} more")

        if commit:
            created = await write_flags_to_db(
                notion, FLAGS_DB_ID, client_name, client_key, unique, source="Email",
            )
            total_created += created
            print(f"  → {created} created, {len(unique) - created} skipped (dupe)")

    print(f"\n{'='*60}")
    if commit:
        print(f"✓ Backfill complete — {total_created} flags created from {total_found} bullets")
    else:
        print(f"DRY RUN — would migrate {total_found} flag bullets")
        print(f"Re-run with COMMIT=1 to actually create flags.")


if __name__ == "__main__":
    commit = os.environ.get("COMMIT", "").strip() == "1"
    asyncio.run(run(commit))
