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
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

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


_BUSINESS_SUFFIX_RE = re.compile(
    r"\s*,?\s*(llc|inc\.?|incorporated|corp\.?|corporation|co\.?|company|ltd\.?|limited|l\.l\.c\.|p\.c\.|pc|pllc|plc)\b\.?\s*$",
    re.IGNORECASE,
)


def _normalize_business_name(name: str) -> str:
    """Fuzzy-match key: lowercase, trim, strip common business suffixes + punctuation.

    Examples:
      'Crown Behavioral Health LLC'  → 'crown behavioral health'
      'Crown Behavioral Health, Inc' → 'crown behavioral health'
      'Crown Behavioral Health'      → 'crown behavioral health'
    """
    cleaned = name.strip().lower()
    # Strip one suffix at a time until none match (handles "LLC, Inc" edge cases)
    for _ in range(3):
        new = _BUSINESS_SUFFIX_RE.sub("", cleaned).strip().rstrip(",.").strip()
        if new == cleaned:
            break
        cleaned = new
    return cleaned


def _group_key(props: dict) -> str:
    """Stable key for grouping submissions from the same client.
    Uses normalized business name so 'Crown Behavioral Health' and 'Crown Behavioral Health LLC' group together.
    """
    name = _normalize_business_name(_get_business_name(props))
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


# ── Field-presence signatures to infer Intake Type when it's blank ─────────────

_CORE_BUSINESS_FIELDS = {"Mission Statement", "Core Values", "Differentiators", "Ideal Patient/Client", "Tagline / Slogan"}
_WEBSITE_BUILD_FIELDS = {"Website Project Type", "Content Ready?", "Existing Website", "Required Pages", "Specific Pages Needed", "Blog Migration Scope"}
_SEO_ADS_FIELDS       = {"Ad Platforms", "Ads Geo Targeting", "Monthly Ad Spend (est.)", "SEO Keywords", "Customer LTV (est.)"}


def _has_any_filled(props: dict, field_names: set[str]) -> int:
    """Count how many of the given fields have non-empty values."""
    count = 0
    for f in field_names:
        p = props.get(f, {})
        if not p:
            continue
        t = p.get("type", "")
        if t == "rich_text" and _get_rich_text(p):
            count += 1
        elif t == "select" and _get_select(p):
            count += 1
        elif t == "multi_select" and p.get("multi_select"):
            count += 1
        elif t == "number" and p.get("number") is not None:
            count += 1
        elif t == "url" and p.get("url"):
            count += 1
    return count


def _infer_intake_type(props: dict) -> str:
    """When Intake Type is blank, guess it from which fields are populated."""
    scores = {
        "Core Business Intake": _has_any_filled(props, _CORE_BUSINESS_FIELDS),
        "Website Build Intake": _has_any_filled(props, _WEBSITE_BUILD_FIELDS),
        "SEO + Ads Intake":     _has_any_filled(props, _SEO_ADS_FIELDS),
    }
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] >= 2 else ""


def _submission_is_empty(props: dict) -> bool:
    """A submission is 'empty' if Business Name + all key content fields are blank."""
    if _get_business_name(props).strip():
        return False
    for f in _CORE_BUSINESS_FIELDS | _WEBSITE_BUILD_FIELDS | _SEO_ADS_FIELDS:
        if _has_any_filled(props, {f}):
            return False
    # Also allow submission if email is present
    return not (_get_email(props.get("Email", {})) or _get_email(props.get("Core Intake Email", {})))


async def _self_heal_submissions(notion: NotionClient, entries: list[dict]) -> list[dict]:
    """Auto-fix three common form submission issues:
      1. Intake Type blank → infer from filled fields
      2. Pipeline Status blank on non-empty row → set to 'New Submission'
      3. Business Name has trailing company suffix → leave alone here (grouping handles it)

    Returns the entries with updated properties in-memory (matching Notion writes).
    """
    for entry in entries:
        props = entry.get("properties", {})
        updates: dict = {}

        # Skip truly empty rows entirely
        if _submission_is_empty(props):
            continue

        intake_type = _get_select(props.get("Intake Type", {}))
        pipeline    = _get_select(props.get("Pipeline Status", {}))

        if not intake_type:
            inferred = _infer_intake_type(props)
            if inferred:
                updates["Intake Type"] = {"select": {"name": inferred}}
                # Update local copy too so grouping/sorting works without re-fetching
                props["Intake Type"] = {"type": "select", "select": {"name": inferred}}

        if not pipeline:
            updates["Pipeline Status"] = {"select": {"name": "New Submission"}}
            props["Pipeline Status"] = {"type": "select", "select": {"name": "New Submission"}}

        if updates:
            try:
                await notion._client.request(
                    path=f"pages/{entry['id']}", method="PATCH",
                    body={"properties": updates},
                )
                fields_fixed = ", ".join(updates.keys())
                biz_name = _get_business_name(props) or "(no name)"
                print(f"  ↻ Auto-fixed {fields_fixed} on {biz_name!r} submission")
            except Exception as e:
                print(f"  ⚠ Couldn't patch submission {entry['id']}: {e}")

    return entries


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
    """Return all submissions with Pipeline Status = 'New Submission'.

    Self-heals three common form submission issues before filtering:
      1. Intake Type blank → inferred from field content
      2. Pipeline Status blank (with non-empty row) → set to 'New Submission'
      3. Company-suffix mismatches ('LLC', 'Inc') → handled at grouping time
    """
    entries = await notion.query_database(INTAKE_DB_ID)
    entries = await _self_heal_submissions(notion, entries)
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
