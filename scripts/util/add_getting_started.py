#!/usr/bin/env python3
"""
add_getting_started.py — Add "How to Get Started" section to a client's Home page.

Updates both the Sitemap DB (Key Sections field) and Content DB (body blocks).
Generates the section copy via Claude using the client's brand voice.

Usage:
    python scripts/add_getting_started.py --client summit_therapy
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


def _h(text: str, level: int = 2) -> dict:
    ht = f"heading_{level}"
    return {"object": "block", "type": ht, ht: {
        "rich_text": [{"type": "text", "text": {"content": text}}]
    }}


def _p(text: str) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {
        "rich_text": [{"type": "text", "text": {"content": text}}]
    }}


def _divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


async def main(client_key: str) -> None:
    cfg = CLIENTS[client_key]
    notion = NotionClient(settings.notion_api_key)
    ai = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    # ── Read brand context ────────────────────────────────────────────────────
    brand_entries = await notion.query_database(cfg["brand_guidelines_db_id"])
    brand_props = brand_entries[0]["properties"] if brand_entries else {}
    voice_tone = _get_rich_text(brand_props.get("Voice & Tone", {}))
    avoid_words = _get_rich_text(brand_props.get("Words to Avoid", {}))

    client_entries = await notion.query_database(cfg["client_info_db_id"])
    client_props = client_entries[0]["properties"] if client_entries else {}
    company = _get_rich_text(client_props.get("Company", {})) or client_key

    # ── 1. Update Sitemap DB — Home Key Sections ──────────────────────────────
    sitemap_entries = await notion.query_database(cfg["sitemap_db_id"])
    home_sitemap = next(
        (e for e in sitemap_entries
         if _get_rich_text(e["properties"].get("Slug", {})) == "/"),
        None
    )

    if home_sitemap:
        existing = _get_rich_text(home_sitemap["properties"].get("Key Sections", {}))
        if "How to Get Started" not in existing:
            lines = existing.split("\n")
            updated = []
            for line in lines:
                if "Final CTA" in line:
                    updated.append("• How to Get Started")
                updated.append(line)
            new_sections = "\n".join(updated)
            await notion._client.request(
                path=f"pages/{home_sitemap['id']}",
                method="PATCH",
                body={"properties": {"Key Sections": {
                    "rich_text": [{"type": "text", "text": {"content": new_sections[:2000]}}]
                }}},
            )
            print("✓ Sitemap DB — Home Key Sections updated")
        else:
            print("  Sitemap already has How to Get Started")

    # ── 2. Generate section copy via Claude ───────────────────────────────────
    print("Generating section copy via Claude...")
    response = await ai.messages.create(
        model=settings.anthropic_model,
        max_tokens=600,
        messages=[{"role": "user", "content": (
            f"Write a 'How to Get Started' section for {company}.\n"
            f"Voice: {voice_tone}\n"
            f"Words to avoid: {avoid_words}\n\n"
            "Rules:\n"
            "- 3 clear numbered steps\n"
            "- Warm, parent-friendly tone\n"
            "- No em dashes\n"
            "- Front-load each step heading (most important word first)\n"
            "- Each step body is 1-2 sentences max\n"
            "- The H2 must be benefit-driven, 4+ words, not a label\n"
            'Return JSON only: {"h2": "...", "steps": [{"number": 1, "heading": "...", "body": "..."}]}'
        )}],
    )

    raw = response.content[0].text if response.content else ""
    clean = re.sub(r"```(?:json)?\n?", "", raw).strip()
    data = json.loads(clean)

    # ── 3. Update Content DB — Home page body blocks ──────────────────────────
    content_entries = await notion.query_database(cfg["content_db_id"])
    home_content = next(
        (e for e in content_entries
         if _get_rich_text(e["properties"].get("Slug", {})) == "/"),
        None
    )

    if not home_content:
        print("Home content entry not found in Content DB")
        return

    blocks = [
        _divider(),
        _h(f'[How to Get Started]  {data["h2"]}', 3),
    ]
    for step in data.get("steps", []):
        blocks.append(_h(f'Step {step["number"]}: {step["heading"]}', 3))
        blocks.append(_p(step["body"]))

    await notion.append_blocks(home_content["id"], blocks)

    print("✓ Content DB — How to Get Started section added to Home page")
    print(f"  H2: {data['h2']}")
    for step in data.get("steps", []):
        print(f"  Step {step['number']}: {step['heading']}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", default="summit_therapy")
    args = parser.parse_args()
    asyncio.run(main(args.client))
