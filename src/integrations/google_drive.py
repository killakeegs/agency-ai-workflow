"""
Google Drive integration — list folders, read docs as plain text.

Uses the same OAuth refresh token as Gmail/Calendar (GOOGLE_GMAIL_REFRESH_TOKEN).
Requires scopes: drive.readonly, documents.readonly
(already granted per scripts/setup/google_auth.py SCOPES_GMAIL).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import httpx

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
REFRESH_TOKEN = os.environ.get("GOOGLE_GMAIL_REFRESH_TOKEN", "").strip()

DRIVE_BASE = "https://www.googleapis.com/drive/v3"


async def get_access_token(http: httpx.AsyncClient | None = None) -> str:
    client = http or httpx.AsyncClient()
    try:
        r = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "refresh_token": REFRESH_TOKEN,
                "grant_type": "refresh_token",
            },
            timeout=30.0,
        )
        r.raise_for_status()
        return r.json()["access_token"]
    finally:
        if http is None:
            await client.aclose()


async def list_files_in_folder(
    http: httpx.AsyncClient,
    token: str,
    folder_id: str,
    name_contains: str = "",
    modified_after: datetime | None = None,
    page_size: int = 50,
) -> list[dict[str, Any]]:
    """List Drive files whose parent is `folder_id`.

    Returns dicts with: id, name, mimeType, createdTime, modifiedTime.
    """
    q_parts = [f"'{folder_id}' in parents", "trashed = false"]
    if name_contains:
        escaped = name_contains.replace("'", "\\'")
        q_parts.append(f"name contains '{escaped}'")
    if modified_after is not None:
        if modified_after.tzinfo is None:
            modified_after = modified_after.replace(tzinfo=timezone.utc)
        q_parts.append(f"modifiedTime > '{modified_after.isoformat()}'")

    params = {
        "q": " and ".join(q_parts),
        "fields": "files(id,name,mimeType,createdTime,modifiedTime,owners)",
        "orderBy": "modifiedTime desc",
        "pageSize": page_size,
    }
    r = await http.get(
        f"{DRIVE_BASE}/files",
        params=params,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json().get("files", []) or []


async def fetch_doc_text(http: httpx.AsyncClient, token: str, doc_id: str) -> str:
    """Export a Google Doc as plain text."""
    r = await http.get(
        f"{DRIVE_BASE}/files/{doc_id}/export",
        params={"mimeType": "text/plain"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=60.0,
    )
    r.raise_for_status()
    return r.text
