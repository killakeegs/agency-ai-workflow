#!/usr/bin/env python3
"""
onboard_client.py — Provision new clients from the Notion intake form submissions

Finds all entries in the Client Intake — Submissions DB with
Pipeline Status = "New Submission", groups them by client, then for each client:

  1. Merges data from all their submitted forms (Core Business + Website Build + SEO/Ads)
  2. Creates all 9 Notion databases under the client's root page
  3. Populates Client Info + Brand Guidelines from merged form data
  4. Generates a Client Brief with Claude and writes it to Notion
  5. Adds the client to config/clients.json (pipeline commands work immediately)
  6. Marks all submissions as "Active Client"

Submissions are grouped by Business Name (or Core Intake Email as fallback).
Core Business Intake is always processed first within a group.

Note: ClickUp folder + Slack are created by the GHL integration when
a deal closes — this script does NOT create ClickUp resources.

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
from src.integrations.notion import NotionClient

INTAKE_DB_ID = "b9de2015-5658-4e6f-a808-793c5508cde2"


def _get_rich_text(prop: dict) -> str:
    return "".join(p.get("text", {}).get("content", "") for p in prop.get("rich_text", []))


def _get_title(prop: dict) -> str:
    return "".join(t.get("text", {}).get("content", "") for t in prop.get("title", []))


def _get_select(prop: dict) -> str:
    sel = prop.get("select")
    return sel.get("name", "") if sel else ""


def _get_email(prop: dict) -> str:
    return prop.get("email", "") or ""


def _get_business_name(props: dict) -> str:
    """Business Name is rich_text; Submission is the title field."""
    return _get_rich_text(props.get("Business Name", {})) or _get_title(props.get("Submission", {}))


def _group_key(props: dict) -> str:
    """Stable key for grouping submissions from the same client."""
    name = _get_business_name(props).strip().lower()
    if name:
        return name
    core_email = _get_email(props.get("Core Intake Email", {})).strip().lower()
    if core_email:
        return core_email
    return _get_email(props.get("Email", {})).strip().lower()


def _intake_sort_order(sub: dict) -> int:
    """Core Business Intake first, then Website Build, then SEO + Ads."""
    order = {"Core Business Intake": 0, "Website Build Intake": 1, "SEO + Ads Intake": 2}
    intake = _get_select(sub["properties"].get("Intake Type", {}))
    return order.get(intake, 99)


def group_by_client(submissions: list[dict]) -> list[list[dict]]:
    """
    Group submissions by client. Returns a list of groups, where each group
    is a list of submissions for the same client sorted Core Business Intake first.
    """
    groups: dict[str, list[dict]] = {}
    for sub in submissions:
        key = _group_key(sub["properties"]) or sub["id"]
        groups.setdefault(key, []).append(sub)

    result = []
    for group in groups.values():
        group.sort(key=_intake_sort_order)
        result.append(group)

    return result


async def list_submissions(notion: NotionClient) -> list[dict]:
    """Return all submissions with Pipeline Status = 'New Submission'."""
    entries = await notion.query_database(INTAKE_DB_ID)
    return [
        e for e in entries
        if _get_select(e["properties"].get("Pipeline Status", {})) == "New Submission"
    ]


async def process_client_group(group: list[dict], notion: NotionClient) -> dict:
    from src.agents.onboarding import OnboardingAgent

    agent = OnboardingAgent(
        notion=notion,
        model=settings.anthropic_model,
        max_tokens=2048,
    )
    return await agent.run(
        client_id="onboarding",
        submission_page_ids=[s["id"] for s in group],
        intake_db_id=INTAKE_DB_ID,
    )


async def main(list_only: bool = False, submission_id: str = "") -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    notion = NotionClient(settings.notion_api_key)

    # ── Find submissions ───────────────────────────────────────────────────────
    if submission_id:
        submission = await notion._client.request(path=f"pages/{submission_id}", method="GET")
        submissions = [submission]
    else:
        submissions = await list_submissions(notion)

    if not submissions:
        print("\nNo new submissions found.")
        print(f"Notion DB: https://notion.so/{INTAKE_DB_ID.replace('-', '')}")
        print("\nAdd a submission with Pipeline Status = 'New Submission' to trigger onboarding.")
        return

    # ── Group by client ────────────────────────────────────────────────────────
    groups = group_by_client(submissions)

    print(f"\nFound {len(groups)} client(s) to onboard ({len(submissions)} form submission(s)):\n")
    for i, group in enumerate(groups, 1):
        name = _get_business_name(group[0]["properties"])
        form_types = [
            _get_select(s["properties"].get("Intake Type", {})) or "Unknown"
            for s in group
        ]
        print(f"  {i}. {name or '(unnamed)'} — {len(group)} form(s): {', '.join(form_types)}")

    if list_only:
        print("\nRun without --list to process these clients.")
        return

    # ── Confirm ───────────────────────────────────────────────────────────────
    print()
    if len(groups) > 1:
        confirm = input(f"Onboard all {len(groups)} clients? [y/N] ").strip().lower()
    else:
        name = _get_business_name(groups[0][0]["properties"])
        confirm = input(f"Onboard '{name}'? This will create their Notion workspace. [y/N] ").strip().lower()

    if confirm != "y":
        print("Aborted.")
        return

    # ── Process each client group ──────────────────────────────────────────────
    results = []
    for group in groups:
        name = _get_business_name(group[0]["properties"])
        form_types = [_get_select(s["properties"].get("Intake Type", {})) or "Unknown" for s in group]
        print(f"\n{'='*60}")
        print(f"Onboarding: {name}  ({', '.join(form_types)})")
        print("=" * 60)

        try:
            result = await process_client_group(group, notion)
            results.append(result)

            print(f"\n✓ {result['client_name']} onboarded successfully")
            print(f"  Client key   : {result['client_key']}")
            print(f"  Notion page  : https://notion.so/{result['client_page_id'].replace('-', '')}")
            print(f"  Client brief : https://notion.so/{result['brief_page_id'].replace('-', '')}")
            print(f"  Forms merged : {len(group)}")
            print()
            print(f"  Next steps:")
            print(f"    make transcript CLIENT={result['client_key']}")
            print(f"    make sitemap CLIENT={result['client_key']}")
            print(f"    make content CLIENT={result['client_key']}")

        except Exception as e:
            print(f"\n✗ Failed to onboard {name}: {e}")
            logging.exception(e)

    print(f"\n{'='*60}")
    print(f"Done — {len(results)}/{len(groups)} client(s) onboarded.")


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
