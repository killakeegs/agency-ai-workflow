#!/usr/bin/env python3
"""
advance_pipeline.py — Check approvals and advance the pipeline automatically

Architecture:
  - Notion = source of truth  (Stage Status field drives everything)
  - ClickUp = visibility       (task auto-created when stage is pending review,
                                closed when the approval is processed)

Stage Status values (in Client Info DB):
  In Progress        → stage is being generated, nothing to do
  Pending Review     → output ready; ClickUp task created, awaiting approval
  Approved           → advance to next stage, close ClickUp task
  Revision Requested → re-run current stage with Revision Notes, close old task

Day-to-day workflow:
  1. make mood-board          ← generates output in Notion
  2. make mark-pending        ← sets "Pending Review", creates ClickUp task
  3. Review output, meet with client
  4. Client approves → set Stage Status = "Approved" in Notion Client Info
  5. make advance             ← closes ClickUp task, runs next stage

  OR if revisions needed:
  4. Set Stage Status = "Revision Requested", add Revision Notes in Notion
  5. make advance             ← re-runs stage with your notes

Usage:
    python scripts/advance_pipeline.py --client wellwell            # check & advance
    python scripts/advance_pipeline.py --client wellwell --mark-pending  # after manual stage run
    python scripts/advance_pipeline.py --client wellwell --setup    # first-time field setup
    make advance
    make mark-pending
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config.clients import CLIENTS as _BASE_CLIENTS
from src.config import settings
from src.integrations.clickup import ClickUpClient
from src.integrations.notion import NotionClient
from src.models.pipeline import PipelineStage

# Advance pipeline needs client_name for display — derive from CLIENTS registry
CLIENTS: dict[str, dict] = {
    k: {**v, "client_name": v.get("name", k.title())}
    for k, v in _BASE_CLIENTS.items()
}

# ── Stage metadata ─────────────────────────────────────────────────────────────

STAGE_LABELS: dict[PipelineStage, str] = {
    PipelineStage.MOOD_BOARD_DRAFT:  "Mood Board",
    PipelineStage.SITEMAP_DRAFT:     "Sitemap",
    PipelineStage.CONTENT_DRAFT:     "Page Content",
    PipelineStage.WIREFRAME_DRAFT:   "Wireframe / Relume Export",
    PipelineStage.HIGH_FID_DRAFT:    "High-Fidelity Design",
    PipelineStage.CLIENT_REVIEW:     "Staging Site",
}

# Map: current draft stage → (next_stage, make_target | None)
STAGE_TRANSITIONS: dict[PipelineStage, tuple[PipelineStage, str | None]] = {
    PipelineStage.MOOD_BOARD_DRAFT:  (PipelineStage.SITEMAP_DRAFT,   "sitemap"),
    PipelineStage.SITEMAP_DRAFT:     (PipelineStage.CONTENT_DRAFT,   "content"),
    PipelineStage.CONTENT_DRAFT:     (PipelineStage.WIREFRAME_DRAFT, "relume-export"),
    PipelineStage.WIREFRAME_DRAFT:   (PipelineStage.HIGH_FID_DRAFT,  None),  # Designer → Figma
    PipelineStage.HIGH_FID_DRAFT:    (PipelineStage.WEBFLOW_BUILD,   None),  # Developer → Webflow
    PipelineStage.CLIENT_REVIEW:     (PipelineStage.COMPLETE,        None),
}

RERUNNABLE_STAGES: dict[PipelineStage, str] = {
    PipelineStage.MOOD_BOARD_DRAFT: "mood_board",
    PipelineStage.SITEMAP_DRAFT:    "sitemap",
    PipelineStage.CONTENT_DRAFT:    "content",
}

# ── Notion helpers ─────────────────────────────────────────────────────────────

def _get_rich_text(prop: dict) -> str:
    return "".join(
        p.get("text", {}).get("content", "") for p in prop.get("rich_text", [])
    )


def _get_select(prop: dict) -> str:
    sel = prop.get("select")
    return sel.get("name", "") if sel else ""


def _notion_db_url(db_id: str) -> str:
    return f"https://notion.so/{db_id.replace('-', '')}"


def _stage_notion_url(stage: PipelineStage, cfg: dict) -> str:
    db_key_map = {
        PipelineStage.MOOD_BOARD_DRAFT: "mood_board_db_id",
        PipelineStage.SITEMAP_DRAFT:    "sitemap_db_id",
        PipelineStage.CONTENT_DRAFT:    "content_db_id",
        PipelineStage.WIREFRAME_DRAFT:  "wireframes_db_id",
        PipelineStage.HIGH_FID_DRAFT:   "hifi_db_id",
    }
    key = db_key_map.get(stage)
    return _notion_db_url(cfg[key]) if key and key in cfg else ""


# ── ClickUp helpers ────────────────────────────────────────────────────────────

async def _create_clickup_task(
    clickup: ClickUpClient,
    list_id: str,
    client_name: str,
    stage: PipelineStage,
    notion_url: str,
) -> str | None:
    """Create a review task in ClickUp. Returns task ID or None on failure."""
    label = STAGE_LABELS.get(stage, stage.value)
    name = f"Review {label} — {client_name}"
    description = (
        f"The {label} output is ready for review in Notion.\n\n"
        f"Steps:\n"
        f"1. Review the output: {notion_url}\n"
        f"2. Meet with the client or review internally\n"
        f"3. Go to Notion → Client Info → set Stage Status:\n"
        f"   • Approved           → run: make advance\n"
        f"   • Revision Requested → add Revision Notes → run: make advance\n"
    )
    try:
        task_id = await clickup.create_task(
            list_id=list_id,
            name=name,
            description=description,
        )
        return task_id
    except Exception as e:
        print(f"  ⚠ ClickUp task creation failed (non-fatal): {e}")
        return None


async def _close_clickup_task(
    clickup: ClickUpClient,
    task_id: str,
    approved: bool = True,
) -> None:
    """Mark the ClickUp review task as complete."""
    if not task_id:
        return
    note = "✅ Approved — pipeline advanced automatically." if approved else \
           "🔄 Revision requested — stage re-running."
    try:
        await clickup.add_comment(task_id, note)
        # Try common closed status names; catch errors gracefully
        for status in ("Complete", "Done", "Closed"):
            try:
                await clickup.update_task(task_id, status=status)
                return
            except Exception:
                continue
    except Exception as e:
        print(f"  ⚠ ClickUp task close failed (non-fatal): {e}")


# ── Setup ──────────────────────────────────────────────────────────────────────

async def setup_fields(client_key: str, notion: NotionClient) -> None:
    """Add Stage Status, Revision Notes, and ClickUp Review Task ID to Client Info DB."""
    cfg = CLIENTS[client_key]
    print(f"Adding pipeline control fields to {client_key} Client Info DB...")
    await notion.update_database(
        database_id=cfg["client_info_db_id"],
        properties_schema={
            "Stage Status": {
                "select": {
                    "options": [
                        {"name": "In Progress",         "color": "blue"},
                        {"name": "Pending Review",       "color": "yellow"},
                        {"name": "Approved",             "color": "green"},
                        {"name": "Revision Requested",   "color": "red"},
                    ]
                }
            },
            "Revision Notes":         {"rich_text": {}},
            "ClickUp Review Task ID": {"rich_text": {}},
        },
    )
    print("  ✓ Stage Status field added")
    print("  ✓ Revision Notes field added")
    print("  ✓ ClickUp Review Task ID field added")
    print(f"\nNext: open Notion → {client_key} Client Info")
    print("      Set Pipeline Stage and Stage Status to match current project state.")


# ── Mark pending (called after a manual stage run) ─────────────────────────────

async def handle_mark_pending(
    client_key: str,
    notion: NotionClient,
    clickup: ClickUpClient,
) -> None:
    """Set Stage Status = Pending Review and create ClickUp task."""
    cfg = CLIENTS[client_key]

    entries = await notion.query_database(cfg["client_info_db_id"])
    if not entries:
        print(f"ERROR: No entries in Client Info DB for {client_key}")
        return

    entry = entries[0]
    entry_id = entry["id"]
    props = entry["properties"]

    current_stage_str = _get_select(props.get("Pipeline Stage", {}))
    current_task_id = _get_rich_text(props.get("ClickUp Review Task ID", {}))

    if not current_stage_str:
        print("ERROR: Pipeline Stage is not set in Client Info.")
        print("Set it first, then run make mark-pending.")
        return

    try:
        current_stage = PipelineStage(current_stage_str)
    except ValueError:
        print(f"ERROR: Unknown Pipeline Stage '{current_stage_str}'")
        return

    label = STAGE_LABELS.get(current_stage, current_stage.value)
    print(f"\nMarking {label} as Pending Review for {cfg['client_name']}...")

    # Close any existing task first
    if current_task_id:
        print(f"  Closing previous ClickUp task ({current_task_id[:8]}...)...")
        await _close_clickup_task(clickup, current_task_id, approved=False)

    # Create new ClickUp review task
    notion_url = _stage_notion_url(current_stage, cfg)
    task_id = await _create_clickup_task(
        clickup,
        cfg["clickup_review_list_id"],
        cfg["client_name"],
        current_stage,
        notion_url,
    )

    # Update Client Info
    update: dict = {
        "Stage Status": notion.select_property("Pending Review"),
    }
    if task_id:
        update["ClickUp Review Task ID"] = notion.text_property(task_id)

    await notion.update_database_entry(entry_id, update)

    print(f"  ✓ Stage Status → Pending Review")
    if task_id:
        print(f"  ✓ ClickUp task created: {task_id}")
        print(f"    https://app.clickup.com/t/{task_id}")
    if notion_url:
        print(f"  ✓ Notion: {notion_url}")
    print(f"\nWhen ready to advance:")
    print(f"  Approved       → Notion: Stage Status = 'Approved'          → make advance")
    print(f"  Needs changes  → Notion: Stage Status = 'Revision Requested' + add notes → make advance")


# ── Main approval flow ─────────────────────────────────────────────────────────

async def main(
    client_key: str,
    setup: bool = False,
    mark_pending: bool = False,
) -> None:
    cfg = CLIENTS[client_key]
    notion = NotionClient(settings.notion_api_key)
    clickup = ClickUpClient(
        settings.clickup_api_key, settings.clickup_workspace_id or ""
    )

    if setup:
        await setup_fields(client_key, notion)
        return

    if mark_pending:
        await handle_mark_pending(client_key, notion, clickup)
        return

    # ── Read Client Info ──────────────────────────────────────────────────────
    entries = await notion.query_database(cfg["client_info_db_id"])
    if not entries:
        print(f"ERROR: No entries in Client Info DB for {client_key}")
        return

    entry = entries[0]
    entry_id = entry["id"]
    props = entry["properties"]

    current_stage_str  = _get_select(props.get("Pipeline Stage", {}))
    stage_status       = _get_select(props.get("Stage Status", {}))
    revision_notes     = _get_rich_text(props.get("Revision Notes", {}))
    current_task_id    = _get_rich_text(props.get("ClickUp Review Task ID", {}))

    print(f"\nClient         : {cfg['client_name']}")
    print(f"Pipeline Stage : {current_stage_str or '(not set)'}")
    print(f"Stage Status   : {stage_status or '(not set)'}")
    if revision_notes:
        print(f"Revision Notes : {revision_notes[:80]}{'...' if len(revision_notes) > 80 else ''}")

    # ── Guard: fields missing ─────────────────────────────────────────────────
    if "Stage Status" not in props:
        print("\nStage Status field is missing. Run:")
        print(f"  make pipeline-setup")
        return

    # ── Pending Review: ensure ClickUp task exists ────────────────────────────
    if stage_status == "Pending Review":
        if not current_task_id:
            print("\nCreating ClickUp review task...")
            try:
                current_stage = PipelineStage(current_stage_str)
                notion_url = _stage_notion_url(current_stage, cfg)
                task_id = await _create_clickup_task(
                    clickup, cfg["clickup_review_list_id"],
                    cfg["client_name"], current_stage, notion_url,
                )
                if task_id:
                    await notion.update_database_entry(entry_id, {
                        "ClickUp Review Task ID": notion.text_property(task_id)
                    })
                    print(f"  ✓ ClickUp task created: https://app.clickup.com/t/{task_id}")
            except ValueError:
                pass
        else:
            print(f"\nAwaiting approval. ClickUp task: https://app.clickup.com/t/{current_task_id}")

        print("\nWhen ready:")
        print("  Approved       → Notion: Stage Status = 'Approved'           → make advance")
        print("  Needs changes  → Notion: Stage Status = 'Revision Requested' + add notes → make advance")
        return

    # ── Nothing to do ─────────────────────────────────────────────────────────
    if stage_status not in ("Approved", "Revision Requested"):
        print(f"\nNothing to advance — status is '{stage_status or 'not set'}'.")
        print("After running a stage, run: make mark-pending")
        return

    # ── Resolve current stage ─────────────────────────────────────────────────
    try:
        current_stage = PipelineStage(current_stage_str)
    except ValueError:
        print(f"\nERROR: Unknown Pipeline Stage '{current_stage_str}'")
        return

    # ── Handle revision ───────────────────────────────────────────────────────
    if stage_status == "Revision Requested":
        if not revision_notes:
            print("\nERROR: Stage Status is 'Revision Requested' but Revision Notes is empty.")
            print("Add feedback to 'Revision Notes' in Client Info, then run: make advance")
            return

        if current_stage not in RERUNNABLE_STAGES:
            print(f"\n{current_stage.value} cannot be automatically re-run.")
            print("Make changes manually, then set Stage Status back to 'In Progress'.")
            return

        print(f"\nRevision requested for {STAGE_LABELS.get(current_stage, current_stage.value)}")
        print(f"Notes: {revision_notes[:100]}")

        # Close existing ClickUp task
        if current_task_id:
            print("\nClosing ClickUp task...")
            await _close_clickup_task(clickup, current_task_id, approved=False)

        # Re-run the stage
        print(f"\nRe-running {current_stage.value}...")
        stage_key = RERUNNABLE_STAGES[current_stage]
        from scripts.run_pipeline_stage import STAGE_RUNNERS
        await STAGE_RUNNERS[stage_key](
            client_key, notion, clickup, revision_notes=revision_notes
        )

        # Create new ClickUp task for the revised output
        notion_url = _stage_notion_url(current_stage, cfg)
        new_task_id = await _create_clickup_task(
            clickup, cfg["clickup_review_list_id"],
            cfg["client_name"], current_stage, notion_url,
        )

        await notion.update_database_entry(entry_id, {
            "Stage Status":           notion.select_property("Pending Review"),
            "Revision Notes":         notion.text_property(""),
            "ClickUp Review Task ID": notion.text_property(new_task_id or ""),
        })

        print(f"\n✓ Re-run complete. Stage Status → Pending Review")
        if new_task_id:
            print(f"✓ New ClickUp task: https://app.clickup.com/t/{new_task_id}")
        print("Review the revised output, then approve or request another revision.")
        return

    # ── Handle approval ───────────────────────────────────────────────────────
    transition = STAGE_TRANSITIONS.get(current_stage)
    if not transition:
        print(f"\nNo transition defined for {current_stage.value}.")
        return

    next_stage, make_target = transition
    label = STAGE_LABELS.get(current_stage, current_stage.value)
    next_label = STAGE_LABELS.get(next_stage, next_stage.value)

    print(f"\n✓ {label} approved")

    # Close the ClickUp task
    if current_task_id:
        print(f"  Closing ClickUp task...")
        await _close_clickup_task(clickup, current_task_id, approved=True)

    # ── Manual handoff (no agent to run) ──────────────────────────────────────
    if make_target is None:
        await notion.update_database_entry(entry_id, {
            "Pipeline Stage":         notion.select_property(next_stage.value),
            "Stage Status":           notion.select_property("In Progress"),
            "ClickUp Review Task ID": notion.text_property(""),
        })
        print(f"✓ Pipeline Stage → {next_stage.value}")
        print(f"\nNext: {next_label} requires manual work.")
        _print_manual_instructions(next_stage)
        return

    # ── Run the next agent ────────────────────────────────────────────────────
    print(f"→ Starting {next_label}...\n")

    if make_target == "relume-export":
        from scripts.export_relume_prompt import main as relume_main
        await relume_main(client_key, open_output=False)
    else:
        from scripts.run_pipeline_stage import STAGE_RUNNERS
        stage_key = make_target.replace("-", "_")
        await STAGE_RUNNERS[stage_key](client_key, notion, clickup)

    # Create ClickUp task for the new stage
    notion_url = _stage_notion_url(next_stage, cfg)
    new_task_id = await _create_clickup_task(
        clickup, cfg["clickup_review_list_id"],
        cfg["client_name"], next_stage, notion_url,
    )

    # Advance Notion pipeline
    await notion.update_database_entry(entry_id, {
        "Pipeline Stage":         notion.select_property(next_stage.value),
        "Stage Status":           notion.select_property("Pending Review"),
        "Revision Notes":         notion.text_property(""),
        "ClickUp Review Task ID": notion.text_property(new_task_id or ""),
    })

    print(f"\n{'='*52}")
    print(f"✓ {next_label} complete")
    print(f"✓ Pipeline Stage → {next_stage.value}")
    print(f"✓ Stage Status   → Pending Review")
    if new_task_id:
        print(f"✓ ClickUp task   → https://app.clickup.com/t/{new_task_id}")
    if notion_url:
        print(f"✓ Notion         → {notion_url}")
    print(f"\nReview the output, then:")
    print(f"  Approved       → Notion: Stage Status = 'Approved'           → make advance")
    print(f"  Needs changes  → Notion: Stage Status = 'Revision Requested' + add notes → make advance")


def _print_manual_instructions(stage: PipelineStage) -> None:
    instructions = {
        PipelineStage.HIGH_FID_DRAFT: (
            "  Designer applies brand direction to the Relume wireframe in Figma.\n"
            "  When design is ready for client review:\n"
            "    → Set Stage Status = 'Pending Review' in Client Info → make mark-pending\n"
            "    → Share Figma link with client\n"
            "    → Client approves → Stage Status = 'Approved' → make advance"
        ),
        PipelineStage.WEBFLOW_BUILD: (
            "  Developer builds the approved design in Webflow.\n"
            "  When staging site is ready:\n"
            "    → Set Stage Status = 'Pending Review' in Client Info → make mark-pending\n"
            "    → Share staging URL with client\n"
            "    → Client approves → Stage Status = 'Approved' → make advance"
        ),
        PipelineStage.COMPLETE: (
            "  Project complete. Publish the site in Webflow."
        ),
    }
    print(instructions.get(stage, f"  See pipeline docs for {stage.value}."))


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Check approvals and advance the pipeline"
    )
    parser.add_argument("--client", default="wellwell")
    parser.add_argument(
        "--setup", action="store_true",
        help="Add Stage Status, Revision Notes, and ClickUp Task ID fields to Client Info DB"
    )
    parser.add_argument(
        "--mark-pending", action="store_true",
        help="Set Stage Status = Pending Review and create ClickUp review task (run after a manual stage)"
    )
    args = parser.parse_args()
    asyncio.run(main(args.client, setup=args.setup, mark_pending=args.mark_pending))
