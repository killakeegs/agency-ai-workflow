#!/usr/bin/env python3
"""
style_reference_sweep.py — Content DB + Blog Posts DB → Style Reference.

Reads each client's finalized agent outputs (website copy in the Content DB,
blog posts in the Blog Posts DB) with team feedback filled in, and writes
them to that client's Style Reference DB. Marks each source entry as
Style Logged so we never double-log.

Zero new team behavior required — the team already approves in Notion
with feedback. This sweep turns those approvals into the priming corpus
that every future agent run reads from.

Run daily via Railway cron, or manually on demand:

    make style-sweep                     # all eligible clients, both DBs
    make style-sweep CLIENT=cielo        # one client
    make style-sweep DRY=1               # preview without writing
    make style-sweep TARGET=blog         # only blog; skip content
    make style-sweep TARGET=content      # only content; skip blog
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.config import settings
from src.integrations.notion import NotionClient
from src.services.style_reference import log_feedback


# ── Status → Decision mappings (per-DB) ───────────────────────────────────────

# Content DB (website copy). Feedback present on an Approved entry almost
# always means the team edited, so default to "Approved with Edits" over plain
# "Approved." Rejected status stays clean.
CONTENT_STATUS_TO_DECISION = {
    "Approved":           "Approved with Edits",
    "Revision Requested": "Rejected",
}

# Blog Posts DB. Lifecycle: Idea → Approved → Draft → Under Review → Image
# Needed → Scheduled → Published. "Published" or "Scheduled" = team committed
# to this version. Under Review is too early (still iterating). We log only
# committed posts so Style Reference reflects what actually shipped for this
# client, not intermediate drafts.
BLOG_STATUS_TO_DECISION = {
    "Published":  "Approved",
    "Scheduled":  "Approved",
}


# ── Blog body extraction (priming needs actual voice, not just metadata) ─────

MAX_BODY_CHARS = 1500  # caps the snapshot; style_reference service also re-caps


def _blocks_to_text(blocks: list[dict]) -> str:
    """Flatten Notion block children into plain text (paragraphs, headings,
    list items). Enough to carry voice and rhythm into prompt priming."""
    lines: list[str] = []
    for block in blocks:
        t = block.get("type", "")
        content = block.get(t, {})
        rich = content.get("rich_text", [])
        text = "".join(seg.get("plain_text") or seg.get("text", {}).get("content", "") for seg in rich)
        if not text.strip():
            continue
        if t.startswith("heading"):
            lines.append(f"## {text}")
        elif t in ("bulleted_list_item", "numbered_list_item"):
            lines.append(f"- {text}")
        else:
            lines.append(text)
    return "\n".join(lines)


async def _fetch_post_body(notion: NotionClient, page_id: str) -> str:
    try:
        blocks = await notion.get_block_children(page_id)
    except Exception:
        return ""
    text = _blocks_to_text(blocks)
    return text[:MAX_BODY_CHARS] + (" …[truncated]" if len(text) > MAX_BODY_CHARS else "")


# ── Asset type classifiers ────────────────────────────────────────────────────

def classify_content_asset_type(page_title: str, slug: str) -> str:
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


# ── Notion property helpers ───────────────────────────────────────────────────

def _rt(prop: dict) -> str:
    return "".join(p.get("text", {}).get("content", "") for p in (prop or {}).get("rich_text", []))


def _title(prop: dict) -> str:
    return "".join(p.get("text", {}).get("content", "") for p in (prop or {}).get("title", []))


def _select(prop: dict) -> str:
    sel = (prop or {}).get("select")
    return sel.get("name", "") if sel else ""


def _url(prop: dict) -> str:
    return (prop or {}).get("url") or ""


# ── Schema self-heal ──────────────────────────────────────────────────────────

async def _ensure_db_fields(
    notion: NotionClient,
    db_id: str,
    label: str,
    required: dict[str, dict],
) -> None:
    """Add any missing fields to an existing DB so the sweep can query + write."""
    try:
        db = await notion._client.request(path=f"databases/{db_id}", method="GET")
    except Exception as e:
        print(f"  ⚠️  Could not read {label} {db_id}: {e}")
        return

    existing = db.get("properties", {})
    to_add = {k: v for k, v in required.items() if k not in existing}
    if to_add:
        await notion._client.request(
            path=f"databases/{db_id}",
            method="PATCH",
            body={"properties": to_add},
        )
        print(f"  Patched {label} — added: {', '.join(to_add.keys())}")


# ── Sweeps ────────────────────────────────────────────────────────────────────

async def sweep_content_db(
    notion: NotionClient,
    client_name: str,
    content_db_id: str,
    style_ref_db_id: str,
    dry_run: bool,
) -> dict[str, int]:
    print(f"  [content] sweep start")
    await _ensure_db_fields(
        notion, content_db_id, "Content DB",
        {
            "Feedback":     {"rich_text": {}},
            "Style Logged": {"checkbox": {}},
        },
    )

    filter_payload = {
        "and": [
            {"or": [
                {"property": "Status", "select": {"equals": "Approved"}},
                {"property": "Status", "select": {"equals": "Revision Requested"}},
            ]},
            {"property": "Style Logged", "checkbox": {"equals": False}},
        ]
    }
    try:
        entries = await notion.query_database(database_id=content_db_id, filter_payload=filter_payload)
    except Exception as e:
        print(f"  ✗ Content DB query failed: {e}")
        return {"logged": 0, "skipped_no_feedback": 0, "candidates": 0}

    logged = 0
    skipped = 0

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
            skipped += 1
            continue

        asset_type = classify_content_asset_type(page_title, slug)
        decision = CONTENT_STATUS_TO_DECISION.get(status, "Approved")

        parts = []
        if title_tag: parts.append(f"Title Tag: {title_tag}")
        if h1:        parts.append(f"H1: {h1}")
        if meta:      parts.append(f"Meta: {meta}")
        final_output = "\n".join(parts) or "(body not captured in snapshot)"

        if dry_run:
            print(f"  [DRY][content] {page_title} → {asset_type} | {decision}")
            continue

        try:
            await log_feedback(
                notion=notion, style_reference_db_id=style_ref_db_id,
                agent="ContentAgent", asset_type=asset_type, decision=decision,
                reason=feedback, original_output="", final_output=final_output,
                target=page_title,
            )
            await notion.update_database_entry(
                page_id=entry["id"],
                properties={"Style Logged": {"checkbox": True}},
            )
            logged += 1
            print(f"  ✓ [content] {page_title} → {asset_type} | {decision}")
        except Exception as e:
            print(f"  ✗ [content] {page_title} — {e}")

    print(f"  [content] logged {logged} | skipped {skipped} (no feedback) | candidates {len(entries)}")
    return {"logged": logged, "skipped_no_feedback": skipped, "candidates": len(entries)}


async def sweep_blog_db(
    notion: NotionClient,
    client_name: str,
    blog_db_id: str,
    style_ref_db_id: str,
    dry_run: bool,
) -> dict[str, int]:
    print(f"  [blog] sweep start")
    await _ensure_db_fields(
        notion, blog_db_id, "Blog Posts DB",
        {"Style Logged": {"checkbox": {}}},  # Feedback already in the schema
    )

    # Blog posts are only logged once they've reached Published or Scheduled.
    # Earlier lifecycle states (Draft, Under Review) are still iterating and
    # would pollute Style Reference with half-formed voice.
    filter_payload = {
        "and": [
            {"or": [
                {"property": "Status", "select": {"equals": "Published"}},
                {"property": "Status", "select": {"equals": "Scheduled"}},
            ]},
            {"property": "Style Logged", "checkbox": {"equals": False}},
        ]
    }
    try:
        entries = await notion.query_database(database_id=blog_db_id, filter_payload=filter_payload)
    except Exception as e:
        print(f"  ✗ Blog Posts DB query failed: {e}")
        return {"logged": 0, "skipped_no_feedback": 0, "candidates": 0}

    logged = 0
    skipped = 0

    for entry in entries:
        props = entry["properties"]
        title          = _title(props.get("Title", {})) or "Untitled"
        status         = _select(props.get("Status", {}))
        feedback       = _rt(props.get("Feedback", {})).strip()
        meta           = _rt(props.get("Meta Description", {}))
        target_kw      = _rt(props.get("Target Keyword", {})) or _rt(props.get("Primary Keyword", {}))
        published_url  = _url(props.get("Published URL", {}))

        # Blog Style Reference is most valuable WITHOUT feedback too — published
        # posts alone teach voice and structure. Only skip if Scheduled + no
        # feedback (still pre-publish; team may or may not have weighed in).
        # Published posts always get logged, feedback or not.
        if status != "Published" and not feedback:
            skipped += 1
            continue

        decision = BLOG_STATUS_TO_DECISION.get(status, "Approved")

        body_snippet = await _fetch_post_body(notion, entry["id"])

        parts: list[str] = [f"Title: {title}"]
        if target_kw:     parts.append(f"Target Keyword: {target_kw}")
        if meta:          parts.append(f"Meta: {meta}")
        if published_url: parts.append(f"Published URL: {published_url}")
        if body_snippet:  parts.append(f"\nBODY (first ~{MAX_BODY_CHARS} chars):\n{body_snippet}")
        final_output = "\n".join(parts)

        reason = feedback or "(team approved without written feedback — final shipped version is the signal)"

        if dry_run:
            print(f"  [DRY][blog] {title[:60]} → {decision} | {len(body_snippet)} chars body")
            continue

        try:
            await log_feedback(
                notion=notion, style_reference_db_id=style_ref_db_id,
                agent="BlogAgent", asset_type="Blog Post", decision=decision,
                reason=reason, original_output="", final_output=final_output,
                target=title,
            )
            await notion.update_database_entry(
                page_id=entry["id"],
                properties={"Style Logged": {"checkbox": True}},
            )
            logged += 1
            print(f"  ✓ [blog] {title[:60]} → {decision}")
        except Exception as e:
            print(f"  ✗ [blog] {title[:60]} — {e}")

    print(f"  [blog] logged {logged} | skipped {skipped} | candidates {len(entries)}")
    return {"logged": logged, "skipped_no_feedback": skipped, "candidates": len(entries)}


# ── Orchestrator ──────────────────────────────────────────────────────────────

async def sweep_all_for_client(client_key: str, target: str, dry_run: bool) -> dict[str, Any]:
    from config.clients import CLIENTS

    cfg = CLIENTS.get(client_key)
    if not cfg:
        print(f"✗ Client '{client_key}' not in registry")
        return {"client": client_key, "skipped": True}

    style_ref_db_id = cfg.get("style_reference_db_id") or ""
    if not style_ref_db_id:
        print(f"  ⚠️  {cfg['name']}: no style_reference_db_id — run `make style-reference-init CLIENT={client_key}` first")
        return {"client": client_key, "skipped": True, "reason": "no_style_reference_db"}

    content_db_id = cfg.get("content_db_id") or ""
    blog_db_id    = cfg.get("blog_posts_db_id") or ""

    do_content = target in ("all", "content") and bool(content_db_id)
    do_blog    = target in ("all", "blog") and bool(blog_db_id)

    if not (do_content or do_blog):
        print(f"  ⚠️  {cfg['name']}: no eligible DB for target={target} (content_db_id={'set' if content_db_id else 'empty'}, blog_posts_db_id={'set' if blog_db_id else 'empty'})")
        return {"client": client_key, "skipped": True, "reason": "no_eligible_db"}

    notion = NotionClient(settings.notion_api_key)
    print(f"\n── {cfg['name']} ──")

    totals = {"content": None, "blog": None}
    if do_content:
        totals["content"] = await sweep_content_db(notion, cfg["name"], content_db_id, style_ref_db_id, dry_run)
    if do_blog:
        totals["blog"] = await sweep_blog_db(notion, cfg["name"], blog_db_id, style_ref_db_id, dry_run)

    return {"client": client_key, **totals}


# ── Entry point ───────────────────────────────────────────────────────────────

async def main(client_key: str, target: str, dry_run: bool) -> None:
    from config.clients import CLIENTS

    if target not in ("all", "content", "blog"):
        print(f"✗ Invalid --target {target} (use: all | content | blog)")
        sys.exit(1)

    if client_key == "all":
        # Eligible = has style_reference_db_id + has at least one of the DBs
        # we'd be sweeping for the chosen target.
        def eligible(cfg: dict) -> bool:
            if not cfg.get("style_reference_db_id"):
                return False
            if target == "content":
                return bool(cfg.get("content_db_id"))
            if target == "blog":
                return bool(cfg.get("blog_posts_db_id"))
            return bool(cfg.get("content_db_id") or cfg.get("blog_posts_db_id"))
        targets = [k for k, cfg in CLIENTS.items() if eligible(cfg)]
        print(f"Sweeping {len(targets)} eligible client(s){' [DRY RUN]' if dry_run else ''} — target={target}\n")
    else:
        targets = [client_key]

    results: list[dict] = []
    for key in targets:
        results.append(await sweep_all_for_client(key, target, dry_run))

    # Aggregate
    total_content_logged = sum((r.get("content") or {}).get("logged", 0) for r in results)
    total_blog_logged    = sum((r.get("blog") or {}).get("logged", 0) for r in results)
    total_skipped        = sum(
        ((r.get("content") or {}).get("skipped_no_feedback", 0) +
         (r.get("blog")    or {}).get("skipped_no_feedback", 0))
        for r in results
    )
    print(f"\n── Summary ──")
    print(f"  Content DB logged: {total_content_logged}")
    print(f"  Blog Posts DB logged: {total_blog_logged}")
    print(f"  Skipped (no feedback): {total_skipped}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sweep Content + Blog Posts DBs → Style Reference")
    parser.add_argument("--client", default="all", help="client key, or 'all' to sweep every eligible client")
    parser.add_argument("--target", default="all", choices=["all", "content", "blog"], help="which DB(s) to sweep")
    parser.add_argument("--dry-run", action="store_true", help="preview what would be logged without writing")
    args = parser.parse_args()
    asyncio.run(main(client_key=args.client, target=args.target, dry_run=args.dry_run))
