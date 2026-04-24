#!/usr/bin/env python3
"""
check_client_readiness.py — Unified readiness check for a client across all
four sources: Clients DB, clients.json, Client Info DB, Brand Guidelines DB,
and Business Profile page.

Reports status as READY / PARTIAL / BLOCKED with per-source breakdown,
writes a consolidated "🚨 Client Readiness" callout at the top of the client's
Business Profile page, and logs one Flag DB entry per gap (dedup'd).

Usage:
    make check-client-readiness CLIENT=lotus_recovery
    make check-client-readiness CLIENT=lotus_recovery DRY=1       # no writes
    make check-client-readiness CLIENT=lotus_recovery QUIET=1     # compact output

Downstream agents (discover-keywords, etc.) call this check internally and
refuse to run when status=BLOCKED unless FORCE=1 is passed.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent.parent / ".env")

from src.config import settings
from src.integrations.notion import NotionClient
from src.services.client_readiness import (
    run_readiness_check,
    write_readiness_callout,
    write_readiness_flags,
)


STATUS_ICON = {"ready": "✓", "partial": "⚠️", "blocked": "🚨"}
STATUS_LABEL = {"ready": "READY", "partial": "PARTIAL", "blocked": "BLOCKED"}


def _print_report(client_name: str, report: dict, quiet: bool) -> None:
    status = report["status"]
    print(f"\n── Client Readiness: {client_name} ──")
    print(f"Status: {STATUS_ICON[status]}  {STATUS_LABEL[status]}  "
          f"({len(report['blocked'])} blocked, "
          f"{len(report['partial'])} partial, "
          f"{len(report['info'])} info)\n")

    if report["info"]:
        for g in report["info"]:
            print(f"  ℹ  {g['description']}")
        print()

    # Group by source
    by_source: dict[str, dict[str, list[dict]]] = {}
    for g in report["all"]:
        if g["severity"] == "info":
            continue
        by_source.setdefault(g["source"], {"blocked": [], "partial": []})
        by_source[g["source"]][g["severity"]].append(g)

    if not any(v["blocked"] or v["partial"] for v in by_source.values()):
        print("  ✓ No gaps in any source — this client is ready for downstream agents.")
        return

    source_order = [
        "clients.json", "Clients DB", "Client Info DB",
        "Brand Guidelines", "Business Profile",
    ]
    for src in source_order:
        if src not in by_source:
            continue
        buckets = by_source[src]
        total = len(buckets["blocked"]) + len(buckets["partial"])
        if total == 0:
            continue
        print(f"▼ {src}  ({len(buckets['blocked'])} blocked, {len(buckets['partial'])} partial)")
        for g in buckets["blocked"]:
            print(f"  🚨 {g['field']}")
            if not quiet:
                print(f"      {g['description'][:140]}")
        for g in buckets["partial"]:
            print(f"  ⚠  {g['field']}")
            if not quiet:
                print(f"      {g['description'][:140]}")
        print()


async def main(client_key: str, dry_run: bool, quiet: bool) -> int:
    from config.clients import CLIENTS
    cfg = CLIENTS.get(client_key)
    if not cfg:
        print(f"✗ Client '{client_key}' not found in registry")
        return 1

    notion = NotionClient(settings.notion_api_key)
    report = await run_readiness_check(notion, cfg, client_key)

    _print_report(cfg.get("name", client_key), report, quiet)

    if dry_run:
        print("[DRY RUN — no callout / no flags written]")
        return 0 if report["status"] != "blocked" else 2

    # Write consolidated callout on BP page
    bp_id = cfg.get("business_profile_page_id", "")
    if bp_id:
        action = await write_readiness_callout(notion, bp_id, report)
        print(f"Callout on BP page: {action}")

    # Write flags
    flags_db_id = os.environ.get("NOTION_FLAGS_DB_ID", "").strip()
    if flags_db_id:
        created = await write_readiness_flags(
            notion, flags_db_id,
            client_name=cfg.get("name", client_key),
            client_key=client_key,
            gaps=report["all"],
        )
        print(f"Flags DB: {len(created)} new gap flag(s) written")

    return 0 if report["status"] != "blocked" else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Unified client readiness check across all 4 sources",
    )
    parser.add_argument("--client", required=True,
                        help="client_key from config/clients.py")
    parser.add_argument("--dry-run", action="store_true",
                        help="report only; no callout or flag writes")
    parser.add_argument("--quiet", action="store_true",
                        help="compact output (no descriptions)")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(
        client_key=args.client, dry_run=args.dry_run, quiet=args.quiet,
    )))
