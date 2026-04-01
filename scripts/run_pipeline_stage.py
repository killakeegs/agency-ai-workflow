#!/usr/bin/env python3
"""
run_pipeline_stage.py — Manually trigger a pipeline stage for a client

Usage:
    python scripts/run_pipeline_stage.py --stage transcript_parser --client wellwell
    python scripts/run_pipeline_stage.py --stage mood_board --client wellwell
    python scripts/run_pipeline_stage.py --stage sitemap --client wellwell
    python scripts/run_pipeline_stage.py --stage content --client wellwell
    python scripts/run_pipeline_stage.py --stage mood_board --client wellwell --revision "Option A too clinical"

Currently supported stages:
    transcript_parser   — Parse meeting transcript → structured Notion fields
    mood_board          — Generate mood board variation briefs
    sitemap             — Generate sitemap page hierarchy
    content             — Generate full page copy for all sitemap pages

Client configs live in config/clients.py — add new clients there.
"""
import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.clients import CLIENTS
from src.config import settings
from src.integrations.clickup import ClickUpClient
from src.integrations.notion import NotionClient

# ── Stage runners ──────────────────────────────────────────────────────────────

async def run_transcript_parser(client_key: str, notion: NotionClient, clickup: ClickUpClient, **_) -> dict:
    from src.agents.transcript_parser import TranscriptParserAgent

    cfg = CLIENTS[client_key]
    agent = TranscriptParserAgent(
        notion=notion,
        clickup=clickup,
        model=settings.anthropic_model,
        max_tokens=4096,
    )
    return await agent.run(
        client_id=cfg["client_id"],
        meeting_notes_entry_id=cfg["meeting_notes_entry_id"],
        action_items_db_id=cfg["action_items_db_id"],
        client_info_db_id=cfg["client_info_db_id"],
        meeting_title="Mood Board & Sitemap Review — Mar 20, 2026",
    )


async def run_mood_board(client_key: str, notion: NotionClient, clickup: ClickUpClient, revision_notes: str = "") -> dict:
    from src.agents.mood_board import MoodBoardAgent

    cfg = CLIENTS[client_key]
    agent = MoodBoardAgent(
        notion=notion,
        clickup=clickup,
        model=settings.anthropic_model,
        max_tokens=8192,
    )
    return await agent.run(
        client_id=cfg["client_id"],
        client_info_db_id=cfg["client_info_db_id"],
        meeting_notes_db_id=cfg["meeting_notes_db_id"],
        brand_guidelines_db_id=cfg["brand_guidelines_db_id"],
        mood_board_db_id=cfg["mood_board_db_id"],
        revision_notes=revision_notes,
    )


async def run_sitemap(client_key: str, notion: NotionClient, clickup: ClickUpClient, revision_notes: str = "") -> dict:
    from src.agents.sitemap import SitemapAgent

    cfg = CLIENTS[client_key]
    agent = SitemapAgent(
        notion=notion,
        clickup=clickup,
        model=settings.anthropic_model,
        max_tokens=16000,
    )
    return await agent.run(
        client_id=cfg["client_id"],
        client_info_db_id=cfg["client_info_db_id"],
        meeting_notes_db_id=cfg["meeting_notes_db_id"],
        brand_guidelines_db_id=cfg["brand_guidelines_db_id"],
        sitemap_db_id=cfg["sitemap_db_id"],
        mood_board_db_id=cfg["mood_board_db_id"],
        revision_notes=revision_notes,
    )


async def run_wireframe(client_key: str, notion: NotionClient, clickup: ClickUpClient, revision_notes: str = "", **_) -> dict:
    from src.agents.wireframe_spec import WireframeSpecAgent

    cfg = CLIENTS[client_key]
    agent = WireframeSpecAgent(
        notion=notion,
        clickup=clickup,
        model=settings.anthropic_model,
        max_tokens=12000,
    )
    return await agent.run(
        client_id=cfg["client_id"],
        client_info_db_id=cfg["client_info_db_id"],
        brand_guidelines_db_id=cfg["brand_guidelines_db_id"],
        sitemap_db_id=cfg["sitemap_db_id"],
        wireframes_db_id=cfg["wireframes_db_id"],
        content_db_id=cfg.get("content_db_id", ""),
        mood_board_db_id=cfg["mood_board_db_id"],
        revision_notes=revision_notes,
    )


async def run_content(client_key: str, notion: NotionClient, clickup: ClickUpClient, revision_notes: str = "", **_) -> dict:
    from src.agents.content import ContentAgent

    cfg = CLIENTS[client_key]
    agent = ContentAgent(
        notion=notion,
        clickup=clickup,
        model=settings.anthropic_model,
        max_tokens=16000,
    )
    return await agent.run(
        client_id=cfg["client_id"],
        client_info_db_id=cfg["client_info_db_id"],
        meeting_notes_db_id=cfg["meeting_notes_db_id"],
        brand_guidelines_db_id=cfg["brand_guidelines_db_id"],
        sitemap_db_id=cfg["sitemap_db_id"],
        mood_board_db_id=cfg["mood_board_db_id"],
        content_db_id=cfg.get("content_db_id", ""),
        revision_notes=revision_notes,
    )


STAGE_RUNNERS = {
    "transcript_parser": run_transcript_parser,
    "mood_board": run_mood_board,
    "sitemap": run_sitemap,
    "content": run_content,
    "wireframe": run_wireframe,
}


# ── Main ───────────────────────────────────────────────────────────────────────

async def main(stage: str, client_key: str, revision_notes: str = "") -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if client_key not in CLIENTS:
        print(f"ERROR: Unknown client '{client_key}'. Available: {list(CLIENTS.keys())}")
        sys.exit(1)

    if stage not in STAGE_RUNNERS:
        print(f"ERROR: Unknown stage '{stage}'. Available: {list(STAGE_RUNNERS.keys())}")
        sys.exit(1)

    print(f"\nRunning stage: {stage} | client: {client_key}")
    if revision_notes:
        print(f"Revision notes: {revision_notes[:80]}{'...' if len(revision_notes) > 80 else ''}")
    print("=" * 60)

    notion = NotionClient(settings.notion_api_key)
    clickup = ClickUpClient(settings.clickup_api_key, settings.clickup_workspace_id or "")

    runner = STAGE_RUNNERS[stage]
    result = await runner(client_key, notion, clickup, revision_notes=revision_notes)

    print("\n" + "=" * 60)
    print("RESULT:")
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run a pipeline stage for a client")
    parser.add_argument(
        "--stage", required=True, choices=list(STAGE_RUNNERS.keys()),
        help="Pipeline stage to run"
    )
    parser.add_argument("--client", required=True, default="wellwell")
    parser.add_argument("--revision", default="", metavar="NOTES",
                        help="Feedback from previous run to guide regeneration")
    args = parser.parse_args()

    asyncio.run(main(args.stage, args.client, revision_notes=args.revision))
