#!/usr/bin/env python3
"""
enrich_from_emails.py — Enrich a client's Notion record from Gmail history.

Searches keegan@rxmedia.io Gmail for the last N days (default 180) for emails
involving the client, synthesizes with Claude into:
  - Client Log entries (deduplicated, with Gmail thread_id tracking)
  - Business Profile enrichments (only genuinely new facts)
  - Flags: open actions, scope changes, blockers, strategic signals
  - rule_set flags → auto-written to Brand Guidelines DB

Usage:
    make enrich-emails CLIENT=wellness_works_management_partners
    make enrich-emails CLIENT=the_manor DAYS=90
    make enrich-emails CLIENT=skycloud_health DRY=1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent.parent / ".env")

from config.clients import CLIENTS
from src.config import settings
from src.integrations.notion import NotionClient
from src.integrations.business_profile import load_business_profile
from src.integrations import gmail
from src.services.email_enrichment import (
    synthesize_threads,
    load_existing_log_entries,
    write_client_log,
    append_profile_enrichments,
    apply_rule_set_flags,
    update_last_contact,
    write_flags_to_db,
    load_open_flags,
)

FLAGS_DB_ID = os.environ.get("NOTION_FLAGS_DB_ID", "").strip()


def _collect_client_emails(cfg: dict) -> list[str]:
    """Gather all known email addresses for a client from config."""
    emails: list[str] = []
    for field in ("email", "primary_contact_email"):
        v = cfg.get(field, "") or ""
        if "@" in v:
            emails.append(v.lower())
    return list(set(emails))


async def _load_extra_contacts_from_notion(notion: NotionClient, cfg: dict) -> list[str]:
    """Pull additional contact emails from Client Info DB."""
    ci_db = cfg.get("client_info_db_id", "")
    if not ci_db:
        return []
    try:
        rows = await notion._client.request(
            path=f"databases/{ci_db}/query", method="POST", body={"page_size": 1},
        )
        if rows.get("results"):
            props = rows["results"][0].get("properties", {})
            for field_name in ("Contacts", "Client Contacts"):
                field = props.get(field_name, {})
                text = "".join(
                    p.get("text", {}).get("content", "")
                    for p in field.get("rich_text", [])
                )
                if text:
                    return re.findall(r"[\w\.-]+@[\w\.-]+", text)
    except Exception:
        pass
    return []


async def run(client_key: str, days: int, dry_run: bool, max_threads: int) -> None:
    if not gmail.GMAIL_REFRESH_TOKEN:
        print("⚠ GOOGLE_GMAIL_REFRESH_TOKEN not set in .env")
        print("  Run: python3 scripts/setup/google_auth.py --gmail")
        sys.exit(1)

    cfg = CLIENTS.get(client_key)
    if not cfg:
        print(f"Client '{client_key}' not found. Available: {', '.join(sorted(CLIENTS.keys()))}")
        sys.exit(1)

    client_name = cfg.get("name", client_key)
    log_db_id   = cfg.get("client_log_db_id", "")
    profile_id  = cfg.get("business_profile_page_id", "")
    brand_db_id = cfg.get("brand_guidelines_db_id", "")

    print(f"\n{'='*60}")
    print(f"  Email Enrichment — {client_name}")
    print(f"  Window: last {days} days")
    print(f"{'='*60}\n")

    notion = NotionClient(api_key=settings.notion_api_key)

    # Gather all contact emails
    emails = _collect_client_emails(cfg)
    extra = await _load_extra_contacts_from_notion(notion, cfg)
    all_emails = list(set(emails + extra))
    print(f"  Contact emails found: {all_emails}")

    query = gmail.build_search_query(all_emails, days)
    if not query:
        print("⚠ No searchable domain/email found for this client")
        sys.exit(1)
    print(f"  Gmail query: {query[:200]}{'...' if len(query) > 200 else ''}\n")

    # Load existing log for dedup
    existing_summaries, thread_map = [], {}
    if log_db_id:
        print("  Loading existing Client Log for dedup...")
        existing_summaries, thread_map = await load_existing_log_entries(notion, log_db_id, days)
        print(f"    {len(existing_summaries)} existing entries, {len(thread_map)} thread IDs tracked")

    # Load existing Business Profile for dedup
    existing_profile = ""
    if profile_id:
        existing_profile = await load_business_profile(notion, cfg)

    # Fetch emails
    token = await gmail.get_access_token()
    async with httpx.AsyncClient() as http:
        msg_ids = await gmail.search_messages(http, token, query, max_results=1000)
        print(f"  Found {len(msg_ids)} messages")
        if not msg_ids:
            print("  No emails matched — nothing to enrich")
            return

        messages: list[dict] = []
        for i, mid in enumerate(msg_ids):
            try:
                messages.append(await gmail.get_message(http, token, mid))
            except Exception as e:
                print(f"    ⚠ Failed to fetch {mid}: {e}")
            if (i + 1) % 50 == 0:
                print(f"    Fetched {i+1}/{len(msg_ids)}")

    # Group into threads
    threads_raw = gmail.group_threads(messages)
    print(f"  Grouped into {len(threads_raw)} threads")

    # Summarize + filter
    summarized: list[dict] = []
    skipped_noise = 0
    skipped_dnl = 0
    skipped_known = 0

    for tid, thread in threads_raw.items():
        # Skip if we already have this thread logged
        if tid in thread_map:
            skipped_known += 1
            continue

        first = thread[0]
        headers = gmail.parse_headers(first)
        subject = headers.get("subject", "")
        from_addr = headers.get("from", "")
        body = gmail.decode_body(first.get("payload", {}))

        if gmail.is_dnl(subject, body):
            skipped_dnl += 1
            continue

        if gmail.is_automated_noise(subject, from_addr, body):
            skipped_noise += 1
            continue

        summarized.append(gmail.summarize_thread(thread))

    print(f"  Substantive: {len(summarized)} | Noise: {skipped_noise} | DNL: {skipped_dnl} | Already logged: {skipped_known}")

    if not summarized:
        print("  No new substantive threads — nothing to enrich")
        return

    # Cap for token budget
    summarized.sort(key=lambda t: t["last_date"], reverse=True)
    if len(summarized) > max_threads:
        print(f"  Limiting to {max_threads} most recent threads (token budget)")
        summarized = summarized[:max_threads]

    # Load existing open flags — feed them into synthesis so Claude doesn't re-emit
    existing_flags = []
    if FLAGS_DB_ID:
        existing_flags = await load_open_flags(notion, FLAGS_DB_ID, client_key)
        print(f"  Loaded {len(existing_flags)} existing open flags for dedup")

    print(f"\nSynthesizing with Claude...")
    synth = await synthesize_threads(
        summarized, client_name, existing_summaries, existing_profile,
        existing_flags=existing_flags,
    )

    log_entries  = synth.get("log_entries", []) or []
    enrichments  = synth.get("profile_enrichments", []) or []
    flags        = synth.get("flags", []) or []
    rule_flags   = [f for f in flags if f.get("type") == "rule_set"]
    other_flags  = [f for f in flags if f.get("type") != "rule_set"]

    print(f"  Log entries: {len(log_entries)}")
    print(f"  Profile enrichments: {len(enrichments)}")
    print(f"  Flags: {len(other_flags)} + {len(rule_flags)} rule_set")
    print(f"  Skipped by Claude: {synth.get('skipped_count', 0)}")

    if dry_run:
        out = Path(f"/tmp/enrich_{client_key}_emails.json")
        out.write_text(json.dumps(synth, indent=2))
        print(f"\n[DRY RUN] Output saved to: {out}")
        print("  Review, then run without DRY=1 to write to Notion.")
        if rule_flags:
            print(f"\n  rule_set flags that would auto-update Brand Guidelines:")
            for rf in rule_flags:
                print(f"    [{rf.get('brand_field')}] {rf.get('brand_value', '')[:120]}")
        return

    # Write to Notion
    print("\nWriting to Notion...")

    if log_entries and log_db_id:
        created, updated = await write_client_log(notion, log_db_id, client_name, log_entries, thread_map)
        print(f"  ✓ {created} new + {updated} updated Client Log entries")
    elif not log_db_id:
        print("  ⚠ No client_log_db_id — skipping Client Log")

    if enrichments and profile_id:
        await append_profile_enrichments(notion, profile_id, enrichments, flags, days)
        print(f"  ✓ {len(enrichments)} enrichments → Business Profile")
    elif not profile_id and enrichments:
        print("  ⚠ No business_profile_page_id — skipping profile append")

    if other_flags and FLAGS_DB_ID:
        created_flags = len(await write_flags_to_db(
            notion, FLAGS_DB_ID, client_name, client_key, other_flags, source="Email",
        ))
        print(f"  ✓ {created_flags} flags → Flags DB (skipped {len(other_flags) - created_flags} dupes)")
    elif other_flags:
        print("  ⚠ NOTION_FLAGS_DB_ID not set — skipping Flags DB writes")

    if rule_flags and brand_db_id:
        applied = await apply_rule_set_flags(notion, brand_db_id, rule_flags)
        print(f"  ✓ {applied} rule_set flags → Brand Guidelines")
    elif rule_flags:
        print("  ⚠ No brand_guidelines_db_id — skipping rule_set writes")

    # Update Last Contact on Clients DB
    if log_entries:
        latest_date = max(e.get("date", "") for e in log_entries if e.get("date"))
        if latest_date:
            await update_last_contact(notion, client_name, latest_date)
            print(f"  ✓ Last Contact → {latest_date}")

    print(f"\n✓ Done. Review in Notion before acting on flagged items.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Enrich Notion from Gmail history")
    parser.add_argument("--client", required=True)
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--max-threads", type=int, default=60)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.client, args.days, args.dry_run, args.max_threads))


if __name__ == "__main__":
    main()
