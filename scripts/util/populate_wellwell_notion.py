#!/usr/bin/env python3
"""
populate_wellwell_notion.py — Migrate WellWell client data into Notion databases

Reads the four downloaded Google Drive documents from /tmp/ and writes structured
data into the WellWell Notion databases that were created by setup_notion.py.

Documents read:
  /tmp/wellwell_onboarding.txt    → Client Info DB (update existing entry)
  /tmp/wellwell_meeting.txt       → Meeting Notes & Transcripts DB (new entry + transcript blocks)
  /tmp/wellwell_brand.txt         → Brand Guidelines DB (new entry)
  /tmp/wellwell_content_outline.txt → appended as reference blocks on Brand Guidelines page

Usage:
    cd "AI-Powered Digital Marketing Agency Workflow"
    source .venv/bin/activate
    python scripts/populate_wellwell_notion.py
    python scripts/populate_wellwell_notion.py --dry-run
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings
from src.integrations.notion import NotionClient

# ── WellWell Notion IDs (created by setup_notion.py) ──────────────────────────
CLIENT_PAGE_ID = "32ff7f45-333e-8134-a061-df6a1578d251"
CLIENT_INFO_DB = "79c6a439-f369-4a47-af1a-89645fef6f4f"
MEETING_NOTES_DB = "6eedcfe8-bcd7-4de2-837c-8fb79fdce249"
BRAND_GUIDELINES_DB = "b7604d57-de3f-455a-a54c-acf3d41fb276"
ACTION_ITEMS_DB = "109f1a05-4f62-4b0b-aa1b-284f7eb7e619"
SITEMAP_DB = "d70fe7ab-a5f4-4814-9209-4bb6eb05b21a"

# ── Document paths ─────────────────────────────────────────────────────────────
ONBOARDING_PATH = Path("/tmp/wellwell_onboarding.txt")
MEETING_PATH = Path("/tmp/wellwell_meeting.txt")
BRAND_PATH = Path("/tmp/wellwell_brand.txt")
CONTENT_OUTLINE_PATH = Path("/tmp/wellwell_content_outline.txt")

# Notion rich_text property limit per text object (use 1800 as buffer for multi-byte chars)
NOTION_TEXT_LIMIT = 1800
# Blocks per append_blocks call
BLOCKS_PER_CALL = 90


def _chunk_text(text: str, size: int = NOTION_TEXT_LIMIT) -> list[str]:
    """Split text into chunks of at most `size` characters."""
    return [text[i : i + size] for i in range(0, len(text), size)]


def _text_to_blocks(text: str) -> list[dict]:
    """Convert a long string into a list of paragraph blocks (2000-char limit each)."""
    blocks = []
    for chunk in _chunk_text(text):
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": chunk}}]
            },
        })
    return blocks


def _heading_block(text: str, level: int = 2) -> dict:
    heading_type = f"heading_{level}"
    return {
        "object": "block",
        "type": heading_type,
        heading_type: {
            "rich_text": [{"type": "text", "text": {"content": text}}]
        },
    }


async def _append_blocks_chunked(notion: NotionClient, page_id: str, blocks: list[dict]) -> None:
    """Append blocks in batches of BLOCKS_PER_CALL (Notion API limit)."""
    for i in range(0, len(blocks), BLOCKS_PER_CALL):
        batch = blocks[i : i + BLOCKS_PER_CALL]
        await notion.append_blocks(page_id, batch)


# ── Step 1: Update Client Info entry ──────────────────────────────────────────

async def populate_client_info(notion: NotionClient, dry_run: bool) -> str:
    """
    Find the existing WellWell entry in Client Info DB and update it with
    structured data from the onboarding form.
    Returns the page_id of the updated entry.
    """
    print("\n[1/4] Updating Client Info...")

    text = ONBOARDING_PATH.read_text()

    # Build note summarizing key onboarding details
    notes = (
        "Services: Full Website Design & Development\n"
        "Scope: 6-10 pages (Home, About, Services, Pricing, Contact/EMR, FAQ, Blog)\n"
        "Target: Teens, young adults, middle-aged adults (primarily women)\n"
        "Keywords: GLP-1 weight loss, neurotoxin, teledermatology, board certified clinicians, HIPAA compliant\n"
        "Inspiration: Hims & Hers, Honeydew, Curology (avoid: overly clinical/spa aesthetic)\n"
        "Branding: Bubbly lowercase font, greens — logo not yet designed\n"
        "Misconception to address: Virtual care not as effective as in-person\n"
        "Domain: wellwell (owns skinetics-rx.com)\n"
        "CTAs: Schedule appointment, email list signup, learn about services\n"
        "Required pages: Privacy Policy, Terms & Conditions, Disclaimers"
    )

    properties = {
        "Company": notion.text_property("WellWell"),
        "Email": {"email": "lizziebrennan79@gmail.com"},
        "Phone": {"phone_number": "9126652114"},
        "Website": notion.url_property("https://wellwell.com"),
        "Business Type": notion.select_property("Service Business"),
        "Pipeline Stage": notion.select_property("MEETING_COMPLETE"),
        "Notes": notion.text_property(notes[:2000]),
    }

    # Find existing entry
    entries = await notion.query_database(CLIENT_INFO_DB)
    existing_id = entries[0]["id"] if entries else None

    if dry_run:
        print(f"  [DRY RUN] Would update Client Info entry (found: {existing_id})")
        return existing_id or "DRY_RUN_ID"

    if existing_id:
        await notion.update_database_entry(existing_id, properties)
        print(f"  ✓ Updated existing Client Info entry: {existing_id}")
        return existing_id
    else:
        entry_id = await notion.create_database_entry(CLIENT_INFO_DB, {
            "Name": notion.title_property("WellWell"),
            **properties,
        })
        print(f"  ✓ Created Client Info entry: {entry_id}")
        return entry_id


# ── Step 2: Create Meeting Notes entry ────────────────────────────────────────

async def populate_meeting_notes(notion: NotionClient, dry_run: bool) -> str:
    """
    Create a Meeting Notes entry for the March 20, 2026 meeting.
    Stores the Gemini summary in properties, then appends the full transcript
    as page blocks.
    """
    print("\n[2/4] Creating Meeting Notes entry...")

    full_text = MEETING_PATH.read_text()

    # Split into summary (before "📖 Transcript") and raw transcript
    transcript_marker = "📖 Transcript"
    if transcript_marker in full_text:
        summary_section, transcript_section = full_text.split(transcript_marker, 1)
    else:
        summary_section = full_text[:3000]
        transcript_section = full_text

    key_decisions = (
        "Font: Quicksand (friendly, approachable, elevated — builds patient trust)\n"
        "Mood board: Combine light teal green from Option 1 with elements from Option 3. "
        "Henna to revise and email Lizzy for final approval.\n"
        "EMR: TEBRA selected (user-friendly, online booking integration, AI note-taking). "
        "Follow-up call with TEBRA rep Darby to be arranged.\n"
        "Sitemap: Approved in principle. 'Weight loss' → 'Weight Loss Management'. "
        "4 main service pages: Medical Dermatology, Tele-dermatology, Weight Loss Management, Neurotoxin Therapy.\n"
        "SEO strategy: 15-20 sub-pages per virtual service for local/regional coverage.\n"
        "Before/After gallery: Cannot use previous patient photos (non-compete/chart restrictions). "
        "Will source licensed stock photos as placeholders.\n"
        "Timeline: September 1 live date. EMR integration must start July.\n"
        "Content: Henna to send Lizzy step-by-step list of required content pages and sections."
    )

    properties = {
        "Name": notion.title_property("Mood Board & Sitemap Review — Mar 20, 2026"),
        "Meeting Date": {"date": {"start": "2026-03-20"}},
        "Meeting Type": notion.select_property("Mood Board Review"),
        "Parsed": notion.checkbox_property(True),
        "Key Decisions": notion.text_property(key_decisions[:2000]),
        "Action Items Count": {"number": 9},
        "Raw Transcript": notion.text_property(summary_section[:1999]),
    }

    if dry_run:
        print("  [DRY RUN] Would create Meeting Notes entry and append full transcript as blocks")
        return "DRY_RUN_MEETING_ID"

    entry_id = await notion.create_database_entry(MEETING_NOTES_DB, properties)
    print(f"  ✓ Created Meeting Notes entry: {entry_id}")

    # Append full transcript as page body blocks
    print("  Appending full transcript as page blocks...")
    blocks: list[dict] = [
        _heading_block("Full Meeting Transcript", level=2),
        _heading_block("Well Well // RxMedia — March 20, 2026", level=3),
    ]
    blocks.extend(_text_to_blocks(transcript_section))
    await _append_blocks_chunked(notion, entry_id, blocks)
    print(f"  ✓ Appended {len(blocks)} blocks ({len(transcript_section):,} chars)")

    return entry_id


# ── Step 3: Create Brand Guidelines entry ─────────────────────────────────────

async def populate_brand_guidelines(notion: NotionClient, dry_run: bool) -> str:
    """
    Create a Brand Guidelines entry with the Mission & Brand Vision document.
    Also appends the content outline as a reference section on the same page.
    """
    print("\n[3/4] Creating Brand Guidelines entry...")

    brand_text = BRAND_PATH.read_text()
    content_text = CONTENT_OUTLINE_PATH.read_text()

    properties = {
        "Name": notion.title_property("WellWell Brand Guidelines — Mission & Vision"),
        "Primary Color": notion.text_property("Light teal green (#approx teal — exact hex TBD from mood board)"),
        "Secondary Color": notion.text_property("White / clean backgrounds"),
        "Primary Font": notion.text_property("Quicksand (rounded, bubbly, lowercase)"),
        "Tone Descriptors": notion.text_property(
            "Friendly, approachable, clinically credible, professional, "
            "welcoming, supportive, judgment-free, modern, evidence-based"
        ),
        "Inspiration URLs": notion.text_property(
            "Hims & Hers | Honeydew | Curology (approachable, creative, patient-friendly) — "
            "AVOID: Doctor on Demand (too clinical) or spa-like aesthetics"
        ),
        "Raw Guidelines": notion.text_property(brand_text[:2000]),
    }

    if dry_run:
        print("  [DRY RUN] Would create Brand Guidelines entry with mission doc + content outline")
        return "DRY_RUN_BRAND_ID"

    entry_id = await notion.create_database_entry(BRAND_GUIDELINES_DB, properties)
    print(f"  ✓ Created Brand Guidelines entry: {entry_id}")

    # Append full brand doc + content outline as page body
    print("  Appending full brand doc and content outline as page blocks...")
    blocks: list[dict] = [
        _heading_block("Mission & Brand Vision", level=2),
    ]
    blocks.extend(_text_to_blocks(brand_text))
    blocks.append(_heading_block("Content Outline (Reference)", level=2))
    blocks.extend(_text_to_blocks(content_text))

    await _append_blocks_chunked(notion, entry_id, blocks)
    print(f"  ✓ Appended {len(blocks)} blocks")

    return entry_id


# ── Step 4: Create Action Items from meeting ──────────────────────────────────

ACTION_ITEMS = [
    ("Share TEBRA user exploration link with Henna G.", "Keegan Warrington", "2026-03-25"),
    ("Revise mood board — combine light teal with Option 3 elements; email Lizzy for final approval", "Agency", "2026-03-27"),
    ("Update sitemap — rename 'Weight Loss' to 'Weight Loss Management'", "Agency", "2026-03-25"),
    ("Source licensed stock photos for before/after gallery placeholders", "Keegan Warrington", "2026-04-05"),
    ("Add note in homepage planning for 3 unique differentiators", "Agency", "2026-03-27"),
    ("Help Elizabeth finalize pricing strategy for services", "Agency", "2026-04-05"),
    ("Connect Keegan Warrington with TEBRA rep Darby via email", "Client", "2026-03-27"),
    ("Send Lizzy step-by-step content requirements list (pages + sections)", "Agency", "2026-03-25"),
    ("Follow up with Keegan re: project billing / invoicing", "Agency", "2026-03-25"),
]

async def populate_action_items(notion: NotionClient, dry_run: bool) -> None:
    """Create the 9 action items from the March 20 meeting in the Action Items DB."""
    print("\n[4/4] Creating Action Items...")

    if dry_run:
        for task, assigned_to, due_date in ACTION_ITEMS:
            print(f"  [DRY RUN] Would create: [{assigned_to}] {task[:60]}...")
        return

    for task, assigned_to, due_date in ACTION_ITEMS:
        # Map to Notion select options
        assignee = "Agency" if assigned_to in ("Keegan Warrington", "Henna G.", "Agency") else "Client"
        entry_id = await notion.create_database_entry(ACTION_ITEMS_DB, {
            "Name": notion.title_property(task),
            "Assigned To": notion.select_property(assignee),
            "Status": notion.select_property("To Do"),
            "Due Date": {"date": {"start": due_date}},
            "Source Meeting": notion.text_property("Mood Board & Sitemap Review — Mar 20, 2026"),
        })
        print(f"  ✓ [{assignee}] {task[:70]}{'...' if len(task) > 70 else ''}")


# ── Main ───────────────────────────────────────────────────────────────────────

async def main(dry_run: bool) -> None:
    print(f"\n{'[DRY RUN] ' if dry_run else ''}Populating WellWell Notion databases")
    print("=" * 60)

    # Verify source files exist
    for path in [ONBOARDING_PATH, MEETING_PATH, BRAND_PATH, CONTENT_OUTLINE_PATH]:
        if not path.exists():
            print(f"ERROR: Missing source file: {path}")
            print("Re-run the Google Drive download step first.")
            sys.exit(1)

    notion = NotionClient(settings.notion_api_key)

    await populate_client_info(notion, dry_run)
    await populate_meeting_notes(notion, dry_run)
    await populate_brand_guidelines(notion, dry_run)
    await populate_action_items(notion, dry_run)

    print("\n" + "=" * 60)
    print("COMPLETE" if not dry_run else "DRY RUN COMPLETE")
    print("=" * 60)
    if not dry_run:
        print(f"\nView in Notion: https://notion.so/{CLIENT_PAGE_ID.replace('-', '')}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Populate WellWell Notion databases from downloaded Drive docs")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to Notion")
    args = parser.parse_args()
    asyncio.run(main(args.dry_run))
