#!/usr/bin/env python3
"""
populate_business_profile.py — Auto-populate a client's Notion Business
Profile from their public website + flag gaps against per-vertical
"required for SEO" checklist.

Usage:
    make populate-business-profile CLIENT=lotus_recovery
    make populate-business-profile CLIENT=lotus_recovery DRY=1

What it does:
  1. Scrapes up to ~12 high-signal pages from the client's website
  2. Reads current Business Profile sections + facts already populated
  3. Claude extracts distinct new facts from the website and routes each
     to the correct H2 section (deduping against existing facts)
  4. Appends new facts under matching section headings (same format as
     the meeting populator: italic source line + bulleted facts)
  5. Compares populated vs required sections (per-vertical checklist in
     config/business_profile_requirements.py)
  6. Writes an inline "🚨 Information Gaps" callout at the top of the
     Business Profile page listing empty / thin required sections
  7. Writes one flag per gap to the workspace Flags DB (dedup'd — safe
     to re-run)

Safe to re-run. Dedup happens in two places:
  - Claude is given the existing profile content and told not to
    duplicate facts already captured
  - write_flags_to_db handles flag dedup (15% same-thread, 55%
    cross-thread from email_enrichment)
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.config import settings
from src.integrations.notion import NotionClient
from src.services.business_profile_populator import populate_from_website


async def main(client_key: str, dry_run: bool) -> None:
    from config.clients import CLIENTS
    cfg = CLIENTS.get(client_key)
    if not cfg:
        print(f"✗ Client '{client_key}' not found in registry")
        sys.exit(1)

    name = cfg.get("name", client_key)
    website = cfg.get("website") or cfg.get("gsc_site_url") or "(missing)"
    verticals = cfg.get("vertical") or []
    if isinstance(verticals, str):
        verticals = [verticals]

    print(f"\n── Populate Business Profile for {name} "
          f"{'[DRY RUN]' if dry_run else ''} ──")
    print(f"  Website: {website}")
    print(f"  Vertical(s): {verticals or '-'}")
    print(f"  BP page: {cfg.get('business_profile_page_id', '(missing)')}")
    print()

    notion = NotionClient(settings.notion_api_key)
    result = await populate_from_website(notion, cfg, dry_run=dry_run)

    print("\n── Summary ──")
    status = result.get("status", "unknown")
    if status == "skipped":
        print(f"  Skipped: {result.get('reason')}")
        return
    if status == "failed":
        print(f"  Failed: {result.get('reason')}")
        sys.exit(2)
    if status == "dry_run":
        print(f"  Pages scraped:         {result.get('pages_scraped')}")
        print(f"  Sections would update: {result.get('sections_would_update')}")
        print(f"  Facts would add:       {result.get('facts_would_add')}")
        gaps = result.get("gaps_preview") or []
        if gaps:
            print(f"  Pre-write gaps:        {len(gaps)}")
            for g in gaps:
                print(f"    ◯ {g['section']}  ({g['severity']})")
        return

    # status == "ok"
    print(f"  Pages scraped:    {result.get('pages_scraped')}")
    print(f"  Sections updated: {result.get('sections_updated')}")
    print(f"  Facts added:      {result.get('facts_added')}")
    print(f"  Flags written:    {result.get('flags_created')}")
    print(f"  Callout action:   {result.get('callout_action')}")
    gaps = result.get("gaps_remaining") or []
    if gaps:
        print(f"\n  🚨 {len(gaps)} gap(s) still need team input:")
        for g in gaps:
            print(f"    ◯ {g['section']}  ({g['severity']})")
    else:
        print("\n  ✓ No required sections empty — SEO pipeline unblocked.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Auto-populate Business Profile from website + flag gaps"
    )
    parser.add_argument("--client", required=True,
                        help="client_key from config/clients.py")
    parser.add_argument("--dry-run", action="store_true",
                        help="preview extracted facts without writing")
    args = parser.parse_args()
    asyncio.run(main(client_key=args.client, dry_run=args.dry_run))
