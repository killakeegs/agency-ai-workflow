#!/usr/bin/env python3
"""
fix_h1_length.py — Shorten H1s that exceed 70 characters.

Reads all Content DB entries, finds H1s over 70 chars, asks Claude to shorten
them while preserving the primary keyword, then updates both the H1 property
and the hero headline block in Notion.

Usage:
    python scripts/fix_h1_length.py --client summit_therapy
"""
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import anthropic

from config.clients import CLIENTS
from src.config import settings
from src.integrations.notion import NotionClient


def _get_rich_text(prop: dict) -> str:
    return "".join(p.get("text", {}).get("content", "") for p in prop.get("rich_text", []))


def _get_title(prop: dict) -> str:
    return "".join(p.get("text", {}).get("content", "") for p in prop.get("title", []))


async def main(client_key: str) -> None:
    cfg = CLIENTS[client_key]
    notion = NotionClient(settings.notion_api_key)
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    entries = await notion.query_database(cfg["content_db_id"])

    to_fix = []
    for entry in entries:
        props = entry["properties"]
        h1 = _get_rich_text(props.get("H1", {}))
        title = _get_title(props.get("Page Title", {})) or _get_title(props.get("Name", {}))
        primary_kw = _get_rich_text(props.get("Primary Keyword", {}))
        if not h1:
            continue
        if len(h1) > 70:
            to_fix.append({
                "id": entry["id"],
                "title": title,
                "h1": h1,
                "primary_keyword": primary_kw,
            })
        else:
            print(f"  ✓ [{len(h1)} chars] {title}")

    if not to_fix:
        print("\nAll H1s are within the 20-70 char range.")
        return

    print(f"\n{len(to_fix)} H1s over 70 chars — shortening via Claude...\n")

    page_list = "\n".join(
        f"- Title: {p['title']} | Primary Keyword: {p['primary_keyword']} "
        f"| Current H1 ({len(p['h1'])} chars): {p['h1']}"
        for p in to_fix
    )

    response = await client.messages.create(
        model=settings.anthropic_model,
        max_tokens=1024,
        messages=[{"role": "user", "content": (
            "Shorten these H1s to 20-70 characters. Rules:\n"
            "- Must include the primary keyword\n"
            "- Strip all marketing fluff after the core keyword phrase\n"
            "- No em dashes\n"
            '- Return a JSON array only, no markdown: [{"title": "...", "h1": "..."}]\n\n'
            f"Pages to shorten:\n{page_list}"
        )}],
    )

    raw = response.content[0].text if response.content else ""
    clean = re.sub(r"```(?:json)?\n?", "", raw).strip()
    shortened = json.loads(clean)
    shortened_map = {p["title"]: p["h1"] for p in shortened}

    for page in to_fix:
        new_h1 = shortened_map.get(page["title"], page["h1"])

        if len(new_h1) > 70:
            print(f"  ⚠ Still over 70 after shortening ({len(new_h1)} chars): {new_h1}")

        # Update H1 property
        await notion._client.request(
            path=f"pages/{page['id']}",
            method="PATCH",
            body={"properties": {
                "H1": {"rich_text": [{"type": "text", "text": {"content": new_h1}}]}
            }},
        )

        # Update hero headline block
        blocks = await notion.get_block_children(page["id"])
        for block in blocks:
            bt = block.get("type", "")
            rt = block.get(bt, {}).get("rich_text", [])
            text = "".join(seg.get("text", {}).get("content", "") for seg in rt)
            if bt == "heading_3" and text.startswith("Headline:"):
                await notion._client.request(
                    path=f"blocks/{block['id']}",
                    method="PATCH",
                    body={"heading_3": {"rich_text": [{"type": "text", "text": {
                        "content": f'Headline: "{new_h1}"'
                    }}]}},
                )
                break

        print(f"  ✓ [{len(new_h1)} chars] {page['title']}: \"{new_h1}\"")

    print(f"\nDone — {len(to_fix)} H1s shortened.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", default="summit_therapy")
    args = parser.parse_args()
    asyncio.run(main(args.client))
