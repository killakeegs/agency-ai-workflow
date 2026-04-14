#!/usr/bin/env python3
"""
run.py — Interactive pipeline runner for the RxMedia Agency Workflow

Just run:
    python3 scripts/run.py

No flags, no memorizing commands. Pick your client and stage from the menu.
"""
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Stage descriptions ─────────────────────────────────────────────────────────

STAGES = [
    {
        "key": "transcript_parser",
        "label": "Parse Meeting Transcript",
        "description": "Read the kickoff meeting recording → extract decisions, action items, preferences into Notion",
        "supports_revision": False,
    },
    {
        "key": "mood_board",
        "label": "Generate Mood Boards",
        "description": "Generate 4 mood board variations (colors, fonts, style direction) → Notion + Figma JSON",
        "supports_revision": True,
    },
    {
        "key": "sitemap",
        "label": "Generate Sitemap",
        "description": "Generate full site structure (pages, slugs, SEO strategy, CMS collections) → Notion + Figma JSON",
        "supports_revision": True,
    },
    {
        "key": "content",
        "label": "Generate Page Copy",
        "description": "Write full copy for every page (H1s, hero, body, CTAs, title tags, meta descriptions) → Notion",
        "supports_revision": True,
    },
    {
        "key": "wireframe",
        "label": "Generate Wireframe Specs",
        "description": "Map every page section to a Relume component — developer-ready build blueprint → Notion",
        "supports_revision": True,
    },
]

CLIENTS = [
    {
        "key": "wellwell",
        "label": "WellWell",
        "description": "Boutique telehealth — dermatology, weight loss, neurotoxin therapy",
    },
    # Add new clients here as they onboard
]

# ── UI helpers ─────────────────────────────────────────────────────────────────

DIVIDER = "─" * 52


def header(text: str) -> None:
    print(f"\n{'═' * 52}")
    print(f"  {text}")
    print(f"{'═' * 52}")


def section(text: str) -> None:
    print(f"\n{DIVIDER}")
    print(f"  {text}")
    print(DIVIDER)


def menu(items: list[dict], key_field: str = "key", label_field: str = "label",
         desc_field: str = "description") -> str | None:
    """Display a numbered menu, return the selected item's key_field value."""
    for i, item in enumerate(items, 1):
        desc = item.get(desc_field, "")
        print(f"  [{i}]  {item[label_field]}")
        if desc:
            print(f"        {desc}")
        print()
    print(f"  [q]  Quit\n")

    while True:
        choice = input("  Enter number: ").strip().lower()
        if choice == "q":
            return None
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(items):
                return items[idx][key_field]
        except ValueError:
            pass
        print("  Invalid choice. Try again.")


def ask(prompt: str, default: str = "") -> str:
    hint = f" (press Enter to skip)" if default == "" else f" [{default}]"
    result = input(f"  {prompt}{hint}: ").strip()
    return result if result else default


# ── Runner ─────────────────────────────────────────────────────────────────────

async def run_stage(stage_key: str, client_key: str, revision_notes: str) -> None:
    from src.config import settings
    from src.integrations.clickup import ClickUpClient
    from src.integrations.notion import NotionClient

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    # Import CLIENTS config from run_pipeline_stage
    from scripts.run_pipeline_stage import (
        CLIENTS as CLIENT_CONFIGS,
        run_transcript_parser,
        run_mood_board,
        run_sitemap,
        run_content,
        run_wireframe,
    )

    RUNNERS = {
        "transcript_parser": run_transcript_parser,
        "mood_board": run_mood_board,
        "sitemap": run_sitemap,
        "content": run_content,
        "wireframe": run_wireframe,
    }

    notion = NotionClient(settings.notion_api_key)
    clickup = ClickUpClient(
        settings.clickup_api_key, settings.clickup_workspace_id or ""
    )

    runner = RUNNERS[stage_key]
    result = await runner(client_key, notion, clickup, revision_notes=revision_notes)

    section("RESULT")
    print(json.dumps(result, indent=2, default=str))


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    header("RxMedia Agency Pipeline Runner")

    # ── Step 1: Select client ─────────────────────────────────────────────────
    if len(CLIENTS) == 1:
        client = CLIENTS[0]
        print(f"\n  Client: {client['label']} — {client['description']}\n")
        client_key = client["key"]
    else:
        section("Select Client")
        client_key = menu(CLIENTS)
        if not client_key:
            print("\n  Exiting.\n")
            sys.exit(0)

    # ── Step 2: Select stage ──────────────────────────────────────────────────
    section("Select Stage")
    stage_key = menu(STAGES)
    if not stage_key:
        print("\n  Exiting.\n")
        sys.exit(0)

    stage_info = next(s for s in STAGES if s["key"] == stage_key)

    # ── Step 3: Revision notes (optional) ─────────────────────────────────────
    revision_notes = ""
    if stage_info["supports_revision"]:
        section("Revision Notes (optional)")
        print("  If this is a re-run and you want changes from the previous output,")
        print("  describe what to fix. Press Enter to skip for a fresh run.\n")
        revision_notes = ask("Feedback / revision notes")

    # ── Step 4: Confirm and run ───────────────────────────────────────────────
    section("Confirm")
    client_label = next(c["label"] for c in CLIENTS if c["key"] == client_key)
    print(f"  Client : {client_label}")
    print(f"  Stage  : {stage_info['label']}")
    if revision_notes:
        preview = revision_notes[:60] + ("..." if len(revision_notes) > 60 else "")
        print(f"  Notes  : {preview}")
    print()

    go = input("  Run now? [Y/n]: ").strip().lower()
    if go not in ("", "y", "yes"):
        print("\n  Cancelled.\n")
        sys.exit(0)

    print()
    asyncio.run(run_stage(stage_key, client_key, revision_notes))
    print()


if __name__ == "__main__":
    main()
