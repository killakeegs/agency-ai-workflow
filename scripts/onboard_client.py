#!/usr/bin/env python3
"""
onboard_client.py — Provision new clients from Notion form submissions

Finds all entries in the Client Onboarding Submissions DB with
Pipeline Status = "New Submission", then for each:

  1. Creates all 9 Notion databases under the client's root page
  2. Creates a ClickUp folder + Website Development list
  3. Populates Client Info + Brand Guidelines from form answers
  4. Generates a Client Brief with Claude and writes it to Notion
  5. Adds the client to config/clients.json (pipeline commands work immediately)
  6. Marks the submission as "Active Client"

Requirements:
  CLICKUP_DEFAULT_SPACE_ID must be set in .env
  (the ClickUp Space ID to create client folders in)

  To find your Space ID:
    1. Open ClickUp → go to the Space you use for client work
    2. Click the "..." menu on the Space → "Space Settings"
    3. The ID is in the URL: app.clickup.com/<workspace>/v/s/<SPACE_ID>

Usage:
    python scripts/onboard_client.py                    # process all new submissions
    python scripts/onboard_client.py --list             # just show pending submissions
    python scripts/onboard_client.py --id <page_id>     # process a specific submission
    make onboard
"""
import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings
from src.integrations.clickup import ClickUpClient
from src.integrations.notion import NotionClient

ONBOARDING_DB_ID = "331f7f45-333e-819e-b3b0-e45e3852136d"


def _get_title(prop: dict) -> str:
    return "".join(t.get("text", {}).get("content", "") for t in prop.get("title", []))


def _get_select(prop: dict) -> str:
    sel = prop.get("select")
    return sel.get("name", "") if sel else ""


async def list_submissions(notion: NotionClient) -> list[dict]:
    """Return all submissions with Pipeline Status = 'New Submission'."""
    entries = await notion.query_database(ONBOARDING_DB_ID)
    return [
        e for e in entries
        if _get_select(e["properties"].get("Pipeline Status", {})) == "New Submission"
    ]


async def process_submission(
    submission: dict,
    notion: NotionClient,
    clickup: ClickUpClient,
    space_id: str,
) -> dict:
    from src.agents.onboarding import OnboardingAgent

    agent = OnboardingAgent(
        notion=notion,
        clickup=clickup,
        model=settings.anthropic_model,
        max_tokens=2048,
    )
    return await agent.run(
        client_id="onboarding",
        submission_page_id=submission["id"],
        onboarding_db_id=ONBOARDING_DB_ID,
        clickup_space_id=space_id,
    )


async def main(list_only: bool = False, submission_id: str = "") -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    space_id = settings.clickup_default_space_id
    if not space_id and not list_only:
        print("ERROR: CLICKUP_DEFAULT_SPACE_ID is not set in your .env file.")
        print()
        print("To find your Space ID:")
        print("  1. Open ClickUp → go to your client work Space")
        print("  2. Click '...' on the Space → Space Settings")
        print("  3. Copy the ID from the URL: app.clickup.com/<workspace>/v/s/<SPACE_ID>")
        print()
        print("Add it to .env:  CLICKUP_DEFAULT_SPACE_ID=<id>")
        sys.exit(1)

    notion  = NotionClient(settings.notion_api_key)
    clickup = ClickUpClient(settings.clickup_api_key, settings.clickup_workspace_id or "")

    # ── Find submissions ───────────────────────────────────────────────────────
    if submission_id:
        # Process a specific submission by page ID
        submission = await notion._client.request(path=f"pages/{submission_id}", method="GET")
        submissions = [submission]
    else:
        submissions = await list_submissions(notion)

    if not submissions:
        print("\nNo new submissions found in the Onboarding DB.")
        print(f"Notion DB: https://notion.so/{ONBOARDING_DB_ID.replace('-', '')}")
        print("\nNew clients appear here when their form Pipeline Status = 'New Submission'.")
        return

    print(f"\nFound {len(submissions)} new submission(s):\n")
    for i, sub in enumerate(submissions, 1):
        name = _get_title(sub["properties"].get("Business Name", {}))
        status = _get_select(sub["properties"].get("Pipeline Status", {}))
        print(f"  {i}. {name or '(unnamed)'} [{status}] — {sub['id']}")

    if list_only:
        print("\nRun without --list to process these submissions.")
        return

    # ── Confirm ───────────────────────────────────────────────────────────────
    print()
    if len(submissions) > 1:
        confirm = input(f"Process all {len(submissions)} submissions? [y/N] ").strip().lower()
    else:
        name = _get_title(submissions[0]["properties"].get("Business Name", {}))
        confirm = input(f"Onboard '{name}'? This will create Notion DBs + ClickUp folder. [y/N] ").strip().lower()

    if confirm != "y":
        print("Aborted.")
        return

    # ── Process each submission ────────────────────────────────────────────────
    results = []
    for sub in submissions:
        name = _get_title(sub["properties"].get("Business Name", {}))
        print(f"\n{'='*60}")
        print(f"Onboarding: {name}")
        print("=" * 60)

        try:
            result = await process_submission(sub, notion, clickup, space_id)
            results.append(result)

            print(f"\n✓ {result['client_name']} onboarded successfully")
            print(f"  Client key   : {result['client_key']}")
            print(f"  Notion page  : https://notion.so/{result['client_page_id'].replace('-', '')}")
            print(f"  Client brief : https://notion.so/{result['brief_page_id'].replace('-', '')}")
            print(f"  ClickUp      : https://app.clickup.com/t/{result['clickup_folder_id']}")
            print()
            print(f"  Pipeline commands ready:")
            print(f"    make transcript CLIENT={result['client_key']}")
            print(f"    make mood-board CLIENT={result['client_key']}")
            print(f"    make images-brand CLIENT={result['client_key']}")

        except Exception as e:
            print(f"\n✗ Failed to onboard {name}: {e}")
            logging.exception(e)

    print(f"\n{'='*60}")
    print(f"Done — {len(results)}/{len(submissions)} client(s) onboarded.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Provision new clients from Notion onboarding form submissions"
    )
    parser.add_argument(
        "--list", action="store_true",
        help="List pending submissions without processing them",
    )
    parser.add_argument(
        "--id", default="", metavar="PAGE_ID",
        help="Process a specific submission by Notion page ID",
    )
    args = parser.parse_args()
    asyncio.run(main(list_only=args.list, submission_id=args.id))
