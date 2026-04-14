#!/usr/bin/env python3
"""
generate_sections.py — Populate Key Sections for all sitemap pages using standard templates.

Reads each page from the Sitemap DB, infers its template type from the slug,
and writes the standard section list to the Key Sections field in Notion.

Run this after the sitemap is rebuilt to populate sections for existing pages.
Can be re-run safely — it overwrites Key Sections on every page.

Usage:
    python scripts/generate_sections.py --client summit_therapy
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.clients import CLIENTS
from config.page_sections import infer_template_key, get_sections
from src.config import settings
from src.integrations.notion import NotionClient


def _get_rich_text(prop: dict) -> str:
    return "".join(p.get("text", {}).get("content", "") for p in prop.get("rich_text", []))


def _get_title(prop: dict) -> str:
    return "".join(p.get("text", {}).get("content", "") for p in prop.get("title", []))


def _get_select(prop: dict) -> str:
    sel = prop.get("select")
    return sel.get("name", "") if sel else ""


async def main(client_key: str, force: bool = False) -> None:
    cfg = CLIENTS[client_key]
    sitemap_db_id = cfg["sitemap_db_id"]
    notion = NotionClient(settings.notion_api_key)

    print(f"Fetching sitemap pages for {client_key}...")
    entries = await notion.query_database(sitemap_db_id)
    print(f"  Found {len(entries)} pages")
    if not force:
        print("  (skipping pages that already have sections — use --force to overwrite)\n")
    else:
        print("  --force: overwriting all existing sections\n")

    updated = 0
    skipped = 0
    preserved = 0

    for entry in entries:
        props   = entry["properties"]
        page_id = entry["id"]
        title   = _get_title(props.get("Page Title", {}))
        slug    = _get_rich_text(props.get("Slug", {}))
        section = _get_select(props.get("Section", {}))

        # Skip pages that already have custom sections unless --force
        existing = _get_rich_text(props.get("Key Sections", {}))
        if existing and not force:
            print(f"  — [{title}] already has sections, skipping")
            preserved += 1
            continue

        template_key = infer_template_key(slug, title, section)
        sections     = get_sections(template_key)

        if not sections:
            print(f"  ⚠ No template for: {title} ({slug}) — skipping")
            skipped += 1
            continue

        sections_text = "\n".join(f"• {s}" for s in sections)

        await notion._client.request(
            path=f"pages/{page_id}",
            method="PATCH",
            body={"properties": {
                "Key Sections": {
                    "rich_text": [{"type": "text", "text": {"content": sections_text[:2000]}}]
                }
            }},
        )
        print(f"  ✓ [{template_key}] {title}")
        updated += 1

    print(f"\nDone — {updated} updated, {preserved} preserved, {skipped} skipped.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", default="summit_therapy")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite existing Key Sections (default: skip pages that already have sections)")
    args = parser.parse_args()
    asyncio.run(main(args.client, force=args.force))
