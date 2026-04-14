#!/usr/bin/env python3
"""
generate_images.py — Generate brand and page images via Replicate (Flux Schnell)

Two modes:

  brand  — Creative library (~15 images): hero lifestyle, detail close-ups,
            textures, environments, product flat lays, brand abstracts.
            Run after mood board approval.

  pages  — Page-specific images (~3 per page): contextually relevant to each
            page's content and purpose. Run after content approval.

Style direction lives in Notion → Brand Guidelines → "Image Direction" field.
Write per-category notes there, e.g.:
  "Detail Close-Up: dewy skin texture, editorial beauty, not clinical"
  "Hero Lifestyle: professional woman 25-55, natural window light, teal/ivory"

Usage:
    python scripts/generate_images.py --client wellwell --mode brand
    python scripts/generate_images.py --client wellwell --mode pages
    python scripts/generate_images.py --client wellwell --mode brand --open
    python scripts/generate_images.py --client wellwell --mode brand --revision "Make textures softer"

Requirements:
    REPLICATE_API_KEY must be set in .env
    Get a token at: https://replicate.com/account/api-tokens
"""
import argparse
import asyncio
import logging
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.clients import CLIENTS
from src.config import settings
from src.integrations.clickup import ClickUpClient
from src.integrations.notion import NotionClient

# ── Images DB schema ───────────────────────────────────────────────────────────

def _images_schema() -> dict:
    return {
        "Image Name": {"title": {}},
        "Batch": {
            "select": {
                "options": [
                    {"name": "Brand Creative", "color": "blue"},
                    {"name": "Page Content",   "color": "green"},
                ]
            }
        },
        "Category": {
            "select": {
                "options": [
                    {"name": "Hero Lifestyle",     "color": "blue"},
                    {"name": "Detail Close-Up",    "color": "pink"},
                    {"name": "Texture Background", "color": "gray"},
                    {"name": "Environment",        "color": "green"},
                    {"name": "Product Flat Lay",   "color": "purple"},
                    {"name": "Brand Abstract",     "color": "orange"},
                    {"name": "Page Feature",       "color": "yellow"},
                ]
            }
        },
        "Status": {
            "select": {
                "options": [
                    {"name": "Generated",       "color": "blue"},
                    {"name": "Approved",        "color": "green"},
                    {"name": "Rejected",        "color": "red"},
                    {"name": "Revision Needed", "color": "yellow"},
                ]
            }
        },
        "Page":             {"rich_text": {}},
        "Image URL":        {"url": {}},
        "Prompt Used":      {"rich_text": {}},
        "Replicate Job ID": {"rich_text": {}},
        "Mood Board Option": {"rich_text": {}},
    }


async def _ensure_images_db(cfg: dict, notion: NotionClient) -> str:
    """Return existing images_db_id or auto-create the DB."""
    if cfg.get("images_db_id"):
        # Update schema to add new fields (idempotent — Notion ignores existing fields)
        try:
            await notion.update_database(
                database_id=cfg["images_db_id"],
                properties_schema=_images_schema(),
            )
        except Exception:
            pass  # Non-fatal — DB exists, fields may already be there
        return cfg["images_db_id"]

    print("  images_db_id not set — auto-creating Images database in Notion...")
    db_info = await notion._client.request(
        path=f"databases/{cfg['mood_board_db_id']}", method="GET"
    )
    parent_page_id = db_info["parent"].get("page_id", "").replace("-", "")
    if not parent_page_id:
        raise RuntimeError(
            "Could not determine parent page ID. Set images_db_id manually in CLIENTS."
        )

    db_id = await notion.create_database(
        parent_page_id=parent_page_id,
        title="Images",
        properties_schema=_images_schema(),
    )
    print(f"\n  ✓ Images database created: {db_id}")
    print(f"  Add this to CLIENTS['images_db_id'] in scripts/generate_images.py\n")
    return db_id


# ── Main ───────────────────────────────────────────────────────────────────────

async def main(
    client_key: str,
    mode: str = "brand",
    revision_notes: str = "",
    open_output: bool = False,
) -> dict:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if client_key not in CLIENTS:
        print(f"ERROR: Unknown client '{client_key}'. Available: {list(CLIENTS.keys())}")
        sys.exit(1)

    if mode not in ("brand", "pages"):
        print(f"ERROR: mode must be 'brand' or 'pages', got '{mode}'")
        sys.exit(1)

    if not settings.replicate_api_key:
        print("ERROR: REPLICATE_API_KEY is not set.")
        print("  1. Create an account at https://replicate.com")
        print("  2. Get your token at https://replicate.com/account/api-tokens")
        print("  3. Add REPLICATE_API_KEY=r8_... to your .env file")
        sys.exit(1)

    cfg = CLIENTS[client_key]
    mode_label = "Brand Creative Bucket" if mode == "brand" else "Page Content Images"
    expected = "~15 images (6 categories)" if mode == "brand" else "~3 images per page"

    print(f"\nGenerating images for: {cfg['name']}")
    print(f"Mode: {mode_label} ({expected})")
    if revision_notes:
        print(f"Revision: {revision_notes[:80]}{'...' if len(revision_notes) > 80 else ''}")
    print("=" * 60)
    print("Tip: Add style notes to Notion → Brand Guidelines → 'Image Direction'")
    print("     e.g. 'Detail Close-Up: dewy skin, editorial beauty, not clinical'")
    print()

    notion = NotionClient(settings.notion_api_key)
    clickup = ClickUpClient(settings.clickup_api_key, settings.clickup_workspace_id or "")

    images_db_id = await _ensure_images_db(cfg, notion)

    from src.agents.image_generation import ImageGenerationAgent
    agent = ImageGenerationAgent(
        notion=notion,
        clickup=clickup,
        model=settings.anthropic_model,
        max_tokens=8192,
    )

    run_kwargs: dict = {
        "mode": mode,
        "client_info_db_id":      cfg["client_info_db_id"],
        "brand_guidelines_db_id": cfg["brand_guidelines_db_id"],
        "mood_board_db_id":       cfg["mood_board_db_id"],
        "images_db_id":           images_db_id,
        "revision_notes":         revision_notes,
    }
    if mode == "pages":
        run_kwargs["sitemap_db_id"] = cfg["sitemap_db_id"]
        run_kwargs["content_db_id"] = cfg["content_db_id"]

    result = await agent.run(client_id=cfg["client_id"], **run_kwargs)

    # ── Print results ─────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    successes = [r for r in result.get("results", []) if r["status"] == "success"]
    errors    = [r for r in result.get("results", []) if r["status"] == "error"]

    print(f"✓ {len(successes)} generated | {len(errors)} failed")

    if result.get("direction_notes"):
        print(f"\nVisual direction:")
        print(f"  {result['direction_notes']}")

    print()
    for r in successes:
        page_tag = f" [{r['page']}]" if r.get("page") else ""
        cat_tag  = f" ({r['category']})" if r.get("category") else ""
        print(f"  ✓ {r['label']}{cat_tag}{page_tag}")
        print(f"    {r['image_url']}")

    for r in errors:
        print(f"  ✗ {r['label']}: {r.get('error', 'unknown')}")

    notion_db_url = f"https://notion.so/{images_db_id.replace('-', '')}"
    print(f"\nNotion Images DB: {notion_db_url}")

    if open_output:
        webbrowser.open(notion_db_url)

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate brand and page images via Replicate Flux Schnell"
    )
    parser.add_argument("--client", default="wellwell")
    parser.add_argument(
        "--mode", choices=["brand", "pages"], default="brand",
        help="brand = creative library (~15 images), pages = per-page images (~3 each)",
    )
    parser.add_argument(
        "--revision", default="", metavar="NOTES",
        help="Feedback to steer regeneration",
    )
    parser.add_argument(
        "--open", action="store_true",
        help="Open the Images DB in Notion when done",
    )
    args = parser.parse_args()
    asyncio.run(main(args.client, mode=args.mode, revision_notes=args.revision, open_output=args.open))
