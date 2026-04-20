#!/usr/bin/env python3
"""
setup_flags_db.py — Create the workspace-level Flags DB.

One-time setup. Creates the Flags database under the System container page,
saves its ID to .env as NOTION_FLAGS_DB_ID.

Schema:
  - Title: short description (first 80 chars of the flag)
  - Client: rich_text (client name)
  - Client Key: rich_text (slug for programmatic lookup)
  - Type: select (BLOCKER, OPEN_ACTION, STRATEGIC, RULE_SET, PROMISE_MADE, SCOPE_CHANGE)
  - Status: select (Open, In Progress, Resolved, Won't Fix)
  - Description: rich_text (full flag text)
  - Source: select (Email, Meeting, Manual)
  - Source Date: date
  - Resolved Date: date
  - Notes: rich_text
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent.parent / ".env")

from src.integrations.notion import NotionClient
from src.config import settings


async def find_system_page(notion: NotionClient) -> str:
    """Find the 'System' container page we created earlier."""
    r = await notion._client.request(
        path="search", method="POST",
        body={"query": "System", "filter": {"value": "page", "property": "object"}, "page_size": 10},
    )
    for p in r.get("results", []):
        props = p.get("properties", {})
        title_parts = props.get("title", {}).get("title", [])
        title = "".join(t.get("text", {}).get("content", "") for t in title_parts)
        if title.strip() == "System":
            return p["id"]
    return ""


async def run() -> None:
    notion = NotionClient(api_key=settings.notion_api_key)

    system_page = await find_system_page(notion)
    if not system_page:
        print("⚠ Couldn't find System page. Falling back to workspace root.")
        system_page = os.environ.get("NOTION_WORKSPACE_ROOT_PAGE_ID", "").strip()

    print(f"Creating Flags DB under: {system_page}")

    schema = {
        "Title": {"title": {}},
        "Client": {"rich_text": {}},
        "Client Key": {"rich_text": {}},
        "Type": {
            "select": {
                "options": [
                    {"name": "BLOCKER", "color": "red"},
                    {"name": "OPEN_ACTION", "color": "orange"},
                    {"name": "STRATEGIC", "color": "purple"},
                    {"name": "RULE_SET", "color": "yellow"},
                    {"name": "PROMISE_MADE", "color": "blue"},
                    {"name": "SCOPE_CHANGE", "color": "pink"},
                ]
            }
        },
        "Status": {
            "select": {
                "options": [
                    {"name": "Open", "color": "red"},
                    {"name": "In Progress", "color": "yellow"},
                    {"name": "Resolved", "color": "green"},
                    {"name": "Won't Fix", "color": "gray"},
                ]
            }
        },
        "Description": {"rich_text": {}},
        "Source": {
            "select": {
                "options": [
                    {"name": "Email", "color": "blue"},
                    {"name": "Meeting", "color": "purple"},
                    {"name": "Manual", "color": "gray"},
                ]
            }
        },
        "Source Date": {"date": {}},
        "Resolved Date": {"date": {}},
        "Notes": {"rich_text": {}},
    }

    result = await notion._client.request(
        path="databases", method="POST",
        body={
            "parent": {"type": "page_id", "page_id": system_page},
            "title": [{"type": "text", "text": {"content": "Flags"}}],
            "properties": schema,
        },
    )
    db_id = result["id"]
    print(f"✓ Created Flags DB: {db_id}")
    print(f"\nAdd to .env:")
    print(f"  NOTION_FLAGS_DB_ID={db_id}")

    # Auto-append to .env
    env_path = Path(__file__).parent.parent.parent / ".env"
    content = env_path.read_text() if env_path.exists() else ""
    if "NOTION_FLAGS_DB_ID" not in content:
        with env_path.open("a") as f:
            f.write(f"\nNOTION_FLAGS_DB_ID={db_id}\n")
        print(f"  ✓ Appended to .env")


if __name__ == "__main__":
    asyncio.run(run())
