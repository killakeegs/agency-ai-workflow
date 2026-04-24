#!/usr/bin/env python3
"""
discover_keywords.py — Step 1 of the SEO keyword pipeline.

Reads the client's Business Profile + Client Info, generates a comprehensive
core keyword pool covering every service × substance × population × insurance
× duration × terminology combination they actually offer, writes candidates
to the Keywords DB at Priority=Medium / Status=Proposed for team review.

Prerequisite: client's Business Profile should be populated first via
    make populate-business-profile CLIENT=x
followed by a team Q&A round to fill any gaps the website doesn't cover.
Running discover-keywords against a thin BP produces thin output.

After this script + team review, run expand-longtail to produce long-tail
variants of newly-approved Priority=High / Status=Target seeds.

Usage:
    make discover-keywords CLIENT=lotus_recovery
    make discover-keywords CLIENT=lotus_recovery DRY=1
    make discover-keywords CLIENT=lotus_recovery TARGET=200
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.services.keyword_discovery import (
    DEFAULT_TARGET_CANDIDATES, discover_keywords,
)


async def main(client_key: str, target_count: int, dry_run: bool, force: bool) -> None:
    from config.clients import CLIENTS
    cfg = CLIENTS.get(client_key)
    if not cfg:
        print(f"✗ Client '{client_key}' not found in registry")
        sys.exit(1)

    result = await discover_keywords(
        cfg, target_count=target_count, dry_run=dry_run, force=force,
    )
    if result.get("status") == "skipped":
        print(f"Skipped: {result.get('reason')}")
        sys.exit(1)
    if result.get("status") == "blocked":
        print(f"\n🚨 BLOCKED: {result.get('reason')}")
        for item in result.get("blocked", [])[:10]:
            print(f"   • {item}")
        sys.exit(3)
    if result.get("status") == "failed":
        print(f"Failed: {result.get('reason')}")
        sys.exit(2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Step 1: generate comprehensive core keyword candidates from BP",
    )
    parser.add_argument("--client", required=True,
                        help="client_key from config/clients.py")
    parser.add_argument("--target", type=int, default=DEFAULT_TARGET_CANDIDATES,
                        help=f"approximate candidate count (default {DEFAULT_TARGET_CANDIDATES})")
    parser.add_argument("--dry-run", action="store_true",
                        help="preview without writing to Notion")
    parser.add_argument("--force", action="store_true",
                        help="bypass readiness gate (not recommended)")
    args = parser.parse_args()
    asyncio.run(main(
        client_key=args.client,
        target_count=args.target,
        dry_run=args.dry_run,
        force=args.force,
    ))
