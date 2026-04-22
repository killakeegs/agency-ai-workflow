#!/usr/bin/env python3
"""
style_reference_sweep.py — Content DB → Style Reference auto-logger.

Reads the Content DB for entries where the team has finalized a decision
(Status ∈ {Approved, Revision Requested}) and filled in the Feedback field
with the reason why. For each unlogged entry, writes a Style Reference row
capturing the agent, asset type, decision, reason, and final shipped copy.
Marks the source entry `Style Logged = True` so we never double-log.

Zero new team behavior required — teams already approve in Notion with
feedback. This sweep turns those feedback notes into the prime corpus that
every future agent run reads from.

Run daily via Railway cron, or manually on demand:

    make style-sweep                     # all clients
    make style-sweep CLIENT=cielo        # one client
    make style-sweep DRY=1               # preview without writing
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.config import settings
from src.integrations.notion import NotionClient
from src.services.style_reference import log_feedback


# ── Mappings ──────────────────────────────────────────────────────────────────

# Content DB Status → Style Reference Decision. If the team filled in Feedback
# AND the page was Approved, they virtually always made edits — so default to
# "Approved with Edits" over plain "Approved." We lose minimal signal and the
# prompt is more honest about what happened.
STATUS_TO_DECISION = {
    "Approved":           "Approved with Edits",
    "Revision Requested": "Rejected",
}


def classify_asset_type(page_title: str, slug: str) -> str:
    """Map a Content DB page to one of the Style Reference asset types."""
    t = (page_title or "").lower()
    s = (slug or "").lower().rstrip("/")

    if s in ("", "/") or t in ("home", "home page"):
        return "Home Page"
    if "about" in t or s.endswith("/about") or s == "/about":
        return "About Page"
    if "/locations/" in s or s.endswith("/locations"):
        return "Location Page"
    if "/services/" in s or "service" in t:
        return "Service Page"
    return "Other"


# ── Notion property helpers (duplicated locally so this script stays self-contained) ──

def _rt(prop: dict) -> str:
    return "".join(p.get("text", {}).get("content", "") for p in (prop or {}).get("rich_text", []))


def _title(prop: dict) -> str:
    return "".join(p.get("text", {}).get("content", "") for p in (prop or {}).get("title", []))


def _select(prop: dict) -> str:
    sel = (prop or {}).get("select")
    return sel.get("name", "") if sel else ""


def _checkbox(prop: dict) -> bool:
    return bool((prop or {}).get("checkbox", False))


# ── Core sweep ────────────────────────────────────────────────────────────────

async def _ensure_feedback_fields(notion: NotionClient, content_db_id: str) -> None:
    """Add Feedback + Style Logged fields to an existing Content DB if missing."""
    try:
        db = await notion._client.request(path=f"databases/{content_db_id}", method="GET")
    except Exception as e:
        print(f"  ⚠️  Could not read Content DB {content_db_id}: {e}")
        return

    existing = db.get("properties", {})
    to_add: dict = {}
    if "Feedback" not in existing:
        to_add["Feedback"] = {"rich_text": {}}
    if "Style Logged" not in existing:
        to_add["Style Logged"] = {"checkbox": {}}

    if to_add:
        await notion._client.request(
            path=f"databases/{content_db_id}",
            method="PATCH",
            body={"properties": to_add},
        )
        print(f"  Patched Content DB — added: {', '.join(to_add.keys())}")


async def sweep_client(client_key: str, dry_run: bool = False) -> dict:
    from config.clients import CLIENTS

    cfg = CLIENTS.get(client_key)
    if not cfg:
        print(f"✗ Client '{client_key}' not in registry")
        return {"client": client_key, "skipped": True, "reason": "not_in_registry"}

    content_db_id = cfg.get("content_db_id") or ""
    style_ref_db_id = cfg.get("style_reference_db_id") or ""

    if not content_db_id:
        print(f"  ⚠️  {cfg['name']}: no content_db_id set — skipping")
        return {"client": client_key, "skipped": True, "reason": "no_content_db"}
    if not style_ref_db_id:
        print(f"  ⚠️  {cfg['name']}: no style_reference_db_id set — run `make style-reference-init CLIENT={client_key}` first")
        return {"client": client_key, "skipped": True, "reason": "no_style_reference_db"}

    notion = NotionClient(settings.notion_api_key)

    print(f"\n── {cfg['name']} ──")
    await _ensure_feedback_fields(notion, content_db_id)

    # Pull Content DB entries that are finalized + not yet logged.
    filter_payload = {
        "and": [
            {
                "or": [
                    {"property": "Status", "select": {"equals": "Approved"}},
                    {"property": "Status", "select": {"equals": "Revision Requested"}},
                ]
            },
            {"property": "Style Logged", "checkbox": {"equals": False}},
        ]
    }
    try:
        entries = await notion.query_database(
            database_id=content_db_id,
            filter_payload=filter_payload,
        )
    except Exception as e:
        print(f"  ✗ Query failed: {e}")
        return {"client": client_key, "skipped": True, "reason": str(e)}

    print(f"  {len(entries)} candidate entries")

    logged = 0
    skipped_no_feedback = 0

    for entry in entries:
        props = entry["properties"]

        page_title = _title(props.get("Page Title", {})) or "Untitled"
        slug       = _rt(props.get("Slug", {}))
        status     = _select(props.get("Status", {}))
        feedback   = _rt(props.get("Feedback", {})).strip()
        title_tag  = _rt(props.get("Title Tag", {}))
        h1         = _rt(props.get("H1", {}))
        meta       = _rt(props.get("Meta Description", {}))

        if not feedback:
            skipped_no_feedback += 1
            continue

        asset_type = classify_asset_type(page_title, slug)
        decision = STATUS_TO_DECISION.get(status, "Approved")

        # Compact snapshot of what shipped. Enough to show voice and structure
        # without replaying the entire page body (which we don't need for priming
        # — the team's reason + the headline-level fields carry the signal).
        final_output_parts = []
        if title_tag: final_output_parts.append(f"Title Tag: {title_tag}")
        if h1:        final_output_parts.append(f"H1: {h1}")
        if meta:      final_output_parts.append(f"Meta: {meta}")
        final_output = "\n".join(final_output_parts) or "(content body not captured in snapshot)"

        if dry_run:
            print(f"  [DRY] {page_title} → {asset_type} | {decision} | {len(feedback)} chars of feedback")
            continue

        try:
            await log_feedback(
                notion=notion,
                style_reference_db_id=style_ref_db_id,
                agent="ContentAgent",
                asset_type=asset_type,
                decision=decision,
                reason=feedback,
                original_output="",  # Agent's original draft isn't snapshotted yet — future enhancement
                final_output=final_output,
                target=page_title,
            )

            await notion.update_database_entry(
                page_id=entry["id"],
                properties={"Style Logged": {"checkbox": True}},
            )
            logged += 1
            print(f"  ✓ {page_title} → {asset_type} | {decision}")
        except Exception as e:
            print(f"  ✗ Failed on {page_title}: {e}")

    print(f"  Logged: {logged} | Skipped (no feedback): {skipped_no_feedback}")
    return {
        "client": client_key,
        "logged": logged,
        "skipped_no_feedback": skipped_no_feedback,
        "candidates": len(entries),
    }


# ── Entry point ───────────────────────────────────────────────────────────────

async def main(client_key: str, dry_run: bool) -> None:
    from config.clients import CLIENTS

    targets: list[str]
    if client_key == "all":
        # Only clients with both DBs set — skip gracefully, don't spam
        targets = [
            k for k, cfg in CLIENTS.items()
            if cfg.get("content_db_id") and cfg.get("style_reference_db_id")
        ]
        print(f"Sweeping {len(targets)} eligible client(s){' [DRY RUN]' if dry_run else ''}\n")
    else:
        targets = [client_key]

    results = []
    for key in targets:
        r = await sweep_client(key, dry_run=dry_run)
        results.append(r)

    total_logged = sum(r.get("logged", 0) for r in results)
    total_skipped = sum(r.get("skipped_no_feedback", 0) for r in results)
    print(f"\n── Summary ──")
    print(f"  Total logged: {total_logged}")
    print(f"  Total skipped (no feedback filled in): {total_skipped}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sweep Content DB → Style Reference")
    parser.add_argument("--client", default="all", help="client key, or 'all' to sweep every eligible client")
    parser.add_argument("--dry-run", action="store_true", help="preview what would be logged without writing")
    args = parser.parse_args()
    asyncio.run(main(client_key=args.client, dry_run=args.dry_run))
