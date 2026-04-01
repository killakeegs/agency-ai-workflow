#!/usr/bin/env python3
"""
migrate_from_gdrive.py — Migrate existing client data from Google Drive to Notion

This script is a SCAFFOLD. Run sections individually by toggling the
STEPS_TO_RUN list at the bottom of the file.

Prerequisites:
  1. Google service account JSON at path in GOOGLE_SERVICE_ACCOUNT_JSON_PATH
     (Or use OAuth credentials — see Google Drive API docs)
  2. Notion structure created via: python scripts/setup_notion.py
  3. GDRIVE_CLIENT_FOLDER_ID environment variable set (the client's Drive folder)

Usage:
    python scripts/migrate_from_gdrive.py --gdrive-folder-id "1abc..." --client-notion-page-id "abc-def-..." --dry-run
    python scripts/migrate_from_gdrive.py --gdrive-folder-id "1abc..." --client-notion-page-id "abc-def-..." --step inventory
    python scripts/migrate_from_gdrive.py --gdrive-folder-id "1abc..." --client-notion-page-id "abc-def-..." --step meeting_notes

Steps:
  inventory      — List and categorize all files in the Drive folder
  meeting_notes  — Migrate Google Docs (meeting notes) → Notion Meeting Notes DB
  brand_assets   — Migrate brand docs → Notion Brand Guidelines DB
  images         — Migrate images (logos, references) → Notion pages as links
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings
from src.integrations.notion import NotionClient


# ── Google Drive helpers ──────────────────────────────────────────────────────

def _build_gdrive_service():
    """
    Build an authenticated Google Drive API service client.
    Uses service account JSON if configured, otherwise raises.
    """
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        if not settings.google_service_account_json_path:
            raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON_PATH not set in .env")

        credentials = service_account.Credentials.from_service_account_file(
            settings.google_service_account_json_path,
            scopes=["https://www.googleapis.com/auth/drive.readonly"],
        )
        return build("drive", "v3", credentials=credentials)
    except ImportError:
        raise RuntimeError(
            "Google API client not installed. Run: pip install google-api-python-client google-auth"
        )


def list_drive_files(service, folder_id: str) -> list[dict]:
    """List all files in a Google Drive folder (non-recursive)."""
    results = []
    page_token = None
    while True:
        response = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id, name, mimeType, modifiedTime, size)",
            pageToken=page_token,
        ).execute()
        results.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return results


def categorize_files(files: list[dict]) -> dict[str, list[dict]]:
    """
    Categorize Drive files by type for routing to the correct Notion database.

    Returns:
      {
        "google_docs": [...],      → Meeting Notes & Transcripts or Brand Guidelines
        "google_sheets": [...],    → Likely structured data
        "images": [...],           → Logo, mood board references, screenshots
        "pdfs": [...],             → Brand guides, proposals
        "other": [...]
      }
    """
    categories: dict[str, list[dict]] = {
        "google_docs": [],
        "google_sheets": [],
        "images": [],
        "pdfs": [],
        "other": [],
    }
    for f in files:
        mime = f.get("mimeType", "")
        if mime == "application/vnd.google-apps.document":
            categories["google_docs"].append(f)
        elif mime == "application/vnd.google-apps.spreadsheet":
            categories["google_sheets"].append(f)
        elif mime.startswith("image/"):
            categories["images"].append(f)
        elif mime == "application/pdf":
            categories["pdfs"].append(f)
        else:
            categories["other"].append(f)
    return categories


# ── Migration steps ───────────────────────────────────────────────────────────

async def step_inventory(
    service,
    folder_id: str,
    dry_run: bool = False,
) -> dict[str, list[dict]]:
    """
    STEP 1: List and categorize all files in the Google Drive folder.
    Prints a summary. Returns the categorized file list.
    """
    print("\nSTEP 1: Inventory Google Drive folder")
    print("-" * 40)
    files = list_drive_files(service, folder_id)
    print(f"Total files found: {len(files)}")

    categories = categorize_files(files)
    for category, category_files in categories.items():
        if category_files:
            print(f"\n{category.upper()} ({len(category_files)} files):")
            for f in category_files:
                print(f"  - {f['name']} ({f['mimeType']})")

    return categories


async def step_meeting_notes(
    service,
    folder_id: str,
    notion: NotionClient,
    meeting_notes_db_id: str,
    dry_run: bool = False,
) -> None:
    """
    STEP 2: Migrate Google Docs to Notion Meeting Notes & Transcripts DB.

    For each Google Doc:
    - Downloads the text content (via export as plain text)
    - Creates a Notion database entry with:
        * Title = filename
        * Raw Transcript = document text
        * Parsed = False (ready for TranscriptParserAgent)
    """
    print("\nSTEP 2: Migrate meeting notes / transcripts")
    print("-" * 40)

    # TODO: implement Google Doc text export and Notion entry creation
    # Reference: service.files().export(fileId=file_id, mimeType='text/plain').execute()
    print("TODO: Implement Google Doc export and Notion write")
    print("      See: googleapiclient.discovery.build('drive', 'v3').files().export()")


async def step_brand_assets(
    service,
    folder_id: str,
    notion: NotionClient,
    brand_guidelines_page_id: str,
    dry_run: bool = False,
) -> None:
    """
    STEP 3: Migrate brand documents to Notion Brand Guidelines DB.

    For each brand-related Google Doc:
    - Downloads text content
    - Creates or updates the Brand Guidelines Notion page with raw text
    """
    print("\nSTEP 3: Migrate brand assets")
    print("-" * 40)
    # TODO: implement brand doc migration
    print("TODO: Implement brand doc export and Notion write")


async def step_images(
    service,
    folder_id: str,
    notion: NotionClient,
    client_page_id: str,
    dry_run: bool = False,
) -> None:
    """
    STEP 4: Migrate images (logos, references) to Notion.

    Note: Notion doesn't support direct file upload via API for externally
    hosted files without a URL. Options:
    1. Re-upload images to a public location (e.g., Cloudinary, S3) and store URL in Notion
    2. Store Google Drive "view" links in Notion (accessible if shared)
    3. Download locally and attach via Notion's upload endpoint (if using Notion API v2+)
    """
    print("\nSTEP 4: Migrate images and visual assets")
    print("-" * 40)
    # TODO: implement image migration strategy
    print("TODO: Decide on image hosting strategy (Drive links vs re-upload)")
    print("      Simplest: store Drive view links in Notion as url properties")


# ── CLI entry point ───────────────────────────────────────────────────────────

async def main_async(args: argparse.Namespace) -> None:
    notion = NotionClient(settings.notion_api_key)
    service = _build_gdrive_service()

    if args.step == "inventory" or args.step == "all":
        await step_inventory(service, args.gdrive_folder_id, dry_run=args.dry_run)

    if args.step == "meeting_notes" or args.step == "all":
        await step_meeting_notes(
            service,
            args.gdrive_folder_id,
            notion,
            meeting_notes_db_id=args.meeting_notes_db_id or "",
            dry_run=args.dry_run,
        )

    if args.step == "brand_assets" or args.step == "all":
        await step_brand_assets(
            service,
            args.gdrive_folder_id,
            notion,
            brand_guidelines_page_id=args.brand_guidelines_page_id or "",
            dry_run=args.dry_run,
        )

    if args.step == "images" or args.step == "all":
        await step_images(
            service,
            args.gdrive_folder_id,
            notion,
            client_page_id=args.client_notion_page_id,
            dry_run=args.dry_run,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate client data from Google Drive to Notion")
    parser.add_argument("--gdrive-folder-id", required=True, help="Google Drive folder ID")
    parser.add_argument("--client-notion-page-id", required=True, help="Notion client root page ID")
    parser.add_argument("--step", default="inventory",
                        choices=["inventory", "meeting_notes", "brand_assets", "images", "all"],
                        help="Which migration step to run")
    parser.add_argument("--meeting-notes-db-id", help="Notion Meeting Notes DB ID")
    parser.add_argument("--brand-guidelines-page-id", help="Notion Brand Guidelines page ID")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to Notion")
    args = parser.parse_args()

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
