#!/usr/bin/env python3
"""
ingest_past_team_syncs.py — Ingest historical RxMedia Weekly Synch Google Docs
into RxMedia's Client Log.

Takes a list of Google Doc IDs, pulls content via Drive API, parses the Gemini
transcript format, and creates structured Client Log entries so future weekly
sync prep docs have historical context to pull from.

Usage:
    python3 scripts/enrichment/ingest_past_team_syncs.py
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent.parent / ".env")

from config.clients import CLIENTS
from src.config import settings
from src.integrations.notion import NotionClient

import anthropic


# 13 historical weekly sync docs provided by user 2026-04-20
DOC_IDS = [
    "1Ty2TeXNbVoLUTR5-zT42bcAAsCpZW_wVkK05iuxlkVU",
    "1YJnrUTur74ANzoWpw9sOV7htBE0PJHRtBEBw8Z4pPPI",
    "1uejSVMW24kiyijFZqZjX191ERKAVs5ll0fYrjR2Sucs",
    "1qHDiTzWvv9mI8nNE6Rm_SHrG2Ggmd4cAUYbSz106tIA",
    "1AEtwm-swb8l_ep9BzAxuwxN92DW17WQdH_c9ccg5YSQ",
    "1SajWBYeb4qAOzfoy54JYeoPuFvv-cFBTmGaCpTRmno8",
    "1pGhO4-M11CbaF46XxlXTG-sXXGMKiO62M0y9ABHppCw",
    "1hnDlLORIhDzU3QvAOGl7_nVNXZaNHJSATL9KPyBT6H4",
    "1VHiJmIlA0A623EIleQH8d8Kd7DlO-q6F4JNDgQkuczk",
    "1tGEt4LEA1DaPK9QbpaqhwdHF879oN9dI4gtWZ8IAHlg",
    "1vsmAVXVl6A4isLC8y8PaodmVMJ_6Q3yvYajJCQjkgC4",
    "1AMLlFI-ddz6w3iIJVPN7Mzto_pZpMDPzd-le9SEqfLE",
    "1UDhsfJhCyFUr66axyDxHWFSOlVvof-WFv4FWiOxNsDE",
]

RXMEDIA_LOG_DB = CLIENTS.get("rxmedia", {}).get("client_log_db_id", "")


# ── Drive fetch ────────────────────────────────────────────────────────────────

async def _drive_access_token() -> str:
    async with httpx.AsyncClient() as http:
        r = await http.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": os.environ["GOOGLE_CLIENT_ID"].strip(),
                "client_secret": os.environ["GOOGLE_CLIENT_SECRET"].strip(),
                "refresh_token": os.environ["GOOGLE_GMAIL_REFRESH_TOKEN"].strip(),
                "grant_type": "refresh_token",
            },
            timeout=30.0,
        )
    r.raise_for_status()
    return r.json()["access_token"]


async def _fetch_doc_text(http: httpx.AsyncClient, token: str, doc_id: str) -> str:
    """Export Google Doc as plain text."""
    r = await http.get(
        f"https://www.googleapis.com/drive/v3/files/{doc_id}/export",
        params={"mimeType": "text/plain"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=60.0,
    )
    r.raise_for_status()
    return r.text


# ── Parse Gemini transcript format ─────────────────────────────────────────────

MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _extract_date(text: str) -> str:
    """Extract date from Gemini transcript header (e.g., 'Dec 1, 2025')."""
    # Look for patterns like "Dec 1, 2025" or "December 1, 2025" near the top
    match = re.search(
        r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2}),?\s+(20\d{2})\b",
        text[:500], re.IGNORECASE,
    )
    if match:
        month = MONTH_MAP[match.group(1).lower()[:3]]
        day = int(match.group(2))
        year = int(match.group(3))
        return f"{year:04d}-{month:02d}-{day:02d}"
    return ""


def _extract_sections(text: str) -> dict:
    """Split the Gemini doc into its standard sections."""
    # Find the "Summary" and "Details" and "Suggested next steps" sections
    sections = {}

    # Summary
    m = re.search(r"###?\s*Summary\s*\n(.*?)(?=###|\Z)", text, re.DOTALL)
    if m:
        sections["summary"] = m.group(1).strip()

    # Details
    m = re.search(r"###?\s*Details\s*\n(.*?)(?=###\s*Suggested|###\s*Transcript|#\s*Transcript|\Z)", text, re.DOTALL)
    if m:
        sections["details"] = m.group(1).strip()

    # Suggested next steps
    m = re.search(r"###?\s*Suggested next steps\s*\n(.*?)(?=###|#\s*Transcript|\Z)", text, re.DOTALL)
    if m:
        sections["next_steps"] = m.group(1).strip()

    return sections


async def _synthesize_with_claude(text: str, date_str: str) -> dict:
    """Use Claude to extract structured fields from the sync doc."""
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    # Use just the first 20K chars — skip the raw transcript tail
    excerpt = text[:20000]

    prompt = f"""Parse this RxMedia team weekly sync meeting note (dated {date_str}).

Extract ONLY a JSON object, no preamble:

{{
  "summary": "3-4 sentence executive summary of what was discussed",
  "key_decisions": "bulleted list of decisions made, one per line, prefixed with -",
  "action_items": "bulleted list of action items with owners when stated, one per line, prefixed with -",
  "themes": "comma-separated tags of major topics (e.g., goals, hiring, client X, SEO process)"
}}

Rules:
- Only include information actually stated in the doc.
- Action items: include the OWNER when named (e.g., "- Henna: write out a plan...").
- Do not repeat content across fields.

Meeting notes:

{excerpt}
"""
    response = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return {}
    return json.loads(m.group(0))


# ── Main ───────────────────────────────────────────────────────────────────────

async def run() -> None:
    if not RXMEDIA_LOG_DB:
        print("⚠ RxMedia Client Log DB not configured")
        sys.exit(1)

    notion = NotionClient(api_key=settings.notion_api_key)
    token = await _drive_access_token()

    created = 0
    skipped = 0

    async with httpx.AsyncClient() as http:
        # First, get existing sync entries to dedup
        try:
            existing = await notion._client.request(
                path=f"databases/{RXMEDIA_LOG_DB}/query", method="POST",
                body={"page_size": 100, "sorts": [{"property": "Date", "direction": "descending"}]},
            )
            existing_dates = set()
            for row in existing.get("results", []):
                props = row.get("properties", {})
                title = "".join(p.get("text", {}).get("content", "") for p in props.get("Title", {}).get("title", []))
                if "Weekly Synch" in title or "Team Sync" in title:
                    date_obj = props.get("Date", {}).get("date")
                    if date_obj:
                        existing_dates.add(date_obj.get("start", ""))
        except Exception:
            existing_dates = set()

        print(f"  Existing sync entries in Notion: {len(existing_dates)}\n")

        for doc_id in DOC_IDS:
            print(f"\nProcessing doc: {doc_id}")
            try:
                text = await _fetch_doc_text(http, token, doc_id)
            except Exception as e:
                print(f"  ⚠ Failed to fetch: {e}")
                continue

            date_str = _extract_date(text)
            if not date_str:
                print(f"  ⚠ Couldn't parse date — skipping")
                continue

            if date_str in existing_dates:
                print(f"  — Already in Notion ({date_str}), skipping")
                skipped += 1
                continue

            print(f"  Date: {date_str}, length: {len(text):,} chars")

            # Parse with Claude
            try:
                parsed = await _synthesize_with_claude(text, date_str)
            except Exception as e:
                print(f"  ⚠ Claude parse failed: {e}")
                continue

            if not parsed:
                print(f"  ⚠ Empty parse result")
                continue

            # Write to Notion
            doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
            props = {
                "Title": {"title": [{"text": {"content": f"RxMedia Weekly Synch — {date_str}"}}]},
                "Date": {"date": {"start": date_str}},
                "Type": {"select": {"name": "Meeting"}},
                "Meeting Type": {"select": {"name": "Pipeline Review"}},
                "Attendees": {"rich_text": [{"text": {"content": "Keegan, Justin, Henna, Andrea (team sync)"}}]},
                "Summary": {"rich_text": [{"text": {"content": parsed.get("summary", "")[:2000]}}]},
                "Key Decisions": {"rich_text": [{"text": {"content": parsed.get("key_decisions", "")[:2000]}}]},
                "Action Items": {"rich_text": [{"text": {"content": parsed.get("action_items", "")[:2000]}}]},
                "Processed": {"checkbox": True},
                "Source": {"rich_text": [{"text": {"content": f"Ingested from {doc_url}"}}]},
            }

            try:
                await notion._client.request(
                    path="pages", method="POST",
                    body={"parent": {"database_id": RXMEDIA_LOG_DB}, "properties": props},
                )
                print(f"  ✓ Created log entry for {date_str}")
                created += 1
            except Exception as e:
                print(f"  ⚠ Notion write failed: {e}")

    print(f"\n{'='*50}")
    print(f"Created: {created}  Skipped (dupe): {skipped}")


if __name__ == "__main__":
    asyncio.run(run())
