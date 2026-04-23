#!/usr/bin/env python3
"""
local_setup_init.py — generate the Local SEO Setup Checklist for a client.

Every local SEO client needs the same ~30 directory / profile / foundation
items claimed + verified in their first 1-2 weeks (GBP, Bing Places, Apple
Business Connect, Yelp, healthcare directories, data aggregators, schema,
call tracking, photo library, first reviews).

Today this is tribal knowledge — someone remembers, someone forgets, and
some clients get partial setup. This script stamps a structured Notion
checklist page under the client's root with every item pre-filled and
per-vertical directories auto-included (addiction clients get Psychology
Today + SAMHSA + Recovery.com; speech clients get ASHA; etc.).

Also serves as a pre-flight gate — the rank monitor won't surface
meaningful data until Tier 1 is claimed, so visual completion status on
this page tells the team when it's safe to start paying attention to
ranks.

Usage:
    make local-setup-init CLIENT=cielo_treatment_center
    make local-setup-init CLIENT=new_client DRY=1

Idempotent — if a checklist page already exists for this client, prints
the existing page ID and exits (doesn't duplicate). Delete the page in
Notion + clear local_setup_checklist_page_id in clients.json to regenerate.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.config import settings
from src.integrations.notion import NotionClient


CLIENTS_JSON_PATH = Path(__file__).parent.parent.parent / "config" / "clients.json"


# ── Checklist items ──────────────────────────────────────────────────────────

TIER_1_CORE = [
    ("Claim Google Business Profile — fill every field, service areas, attributes, logo, cover", "https://business.google.com"),
    ("Grant RxMedia the Manager role on GBP (unlocks API writes for posts/Q&A/insights)", ""),
    ("Claim Bing Places — import from GBP", "https://www.bingplaces.com"),
    ("Claim Apple Business Connect — verify", "https://businessconnect.apple.com"),
    ("Claim Facebook Business Page — match NAP exactly", "https://business.facebook.com"),
    ("Claim Yelp — even if disliked (unclaimed = worse)", "https://biz.yelp.com"),
]

TIER_2_HEALTHCARE = [
    ("Claim Healthgrades provider profile(s)", "https://www.healthgrades.com"),
    ("Claim Vitals", "https://www.vitals.com"),
    ("Claim WebMD Care", "https://doctor.webmd.com"),
    ("Evaluate Zocdoc (paid — only if accepting new patients + insurance fits)", "https://www.zocdoc.com"),
]

# Vertical-specific directories — Tier 3
TIER_3_BY_VERTICAL: dict[str, list[tuple[str, str]]] = {
    "addiction_treatment": [
        ("Claim Psychology Today profile", "https://www.psychologytoday.com"),
        ("Register with SAMHSA Treatment Locator", "https://findtreatment.samhsa.gov"),
        ("Claim Recovery.com listing", "https://recovery.com"),
        ("Claim Rehab.com listing", "https://www.rehab.com"),
    ],
    "speech_pathology": [
        ("Claim ASHA ProFind profile", "https://www.asha.org/profind"),
    ],
    "occupational_therapy": [
        ("Claim AOTA Find an OT profile", "https://www.aota.org"),
    ],
    "physical_therapy": [
        ("Claim APTA Find a PT profile", "https://aptaapps.apta.org/APTAPTDirectory"),
        ("Claim PTandMe profile", "https://ptandme.com"),
    ],
    "mental_health": [
        ("Claim Psychology Today profile", "https://www.psychologytoday.com"),
        ("Claim GoodTherapy profile", "https://www.goodtherapy.org"),
        ("Claim TherapyDen profile", "https://www.therapyden.com"),
        ("Claim Inclusive Therapists profile", "https://www.inclusivetherapists.com"),
    ],
    "dermatology": [
        ("Claim AAD Find a Dermatologist profile", "https://find-a-derm.aad.org"),
        ("Claim RealSelf profile", "https://www.realself.com"),
    ],
}

TIER_4_AGGREGATORS = [
    ("Submit NAP to Data Axle (Infogroup) — free, feeds hundreds of small directories", "https://www.data-axle.com"),
    ("Submit NAP to Localeze (Neustar)", "https://www.neustarlocaleze.biz"),
    ("Claim Foursquare listing — feeds Apple Maps + Uber + others", "https://foursquare.com"),
]

TIER_5_FOUNDATION = [
    ("Run NAP audit — verify Name/Address/Phone consistent across every claimed profile (BrightLocal scan or manual)",
     "https://www.brightlocal.com"),
    ("Deploy LocalBusiness schema markup on website", ""),
    ("Configure call tracking number (if client on Care Plan + CTM)", ""),
    ("Upload photo library to GBP — minimum 10 (exterior, interior, team, logo, 5 service/environment)", ""),
    ("Send first 5 review requests to existing happy clients (seed reviews before agent response drafting kicks in)", ""),
    ("Grant GBP API OAuth to agent (unlocks posts + insights)", ""),
]

TIER_6_KICKSTART = [
    ("Publish first GBP post manually (establishes baseline the agent drafts against)", ""),
    ("Audit existing FAQ content + FAQ schema on site — add gaps for common patient questions (Ask Maps pulls from here)", ""),
    ("Respond to any pre-existing reviews on GBP (agent takes over for new reviews going forward)", ""),
]

READY_GATE_NOTES = (
    "Gate for agent activation: rank monitor + competitor auto-discovery become "
    "meaningful once Tier 1 + all applicable Tier 3 profiles are claimed. "
    "Starting monitoring before this = tracking a business that barely exists "
    "online. Resist the temptation to skip ahead."
)


# ── Notion block builders ────────────────────────────────────────────────────

def _heading(text: str, level: int = 2) -> dict:
    t = f"heading_{level}"
    return {"object": "block", "type": t, t: {
        "rich_text": [{"type": "text", "text": {"content": text}}]
    }}


def _paragraph(text: str) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {
        "rich_text": [{"type": "text", "text": {"content": text}}]
    }}


def _callout(text: str, emoji: str = "⚠️") -> dict:
    return {"object": "block", "type": "callout", "callout": {
        "rich_text": [{"type": "text", "text": {"content": text}}],
        "icon": {"emoji": emoji},
    }}


def _todo(text: str, url: str = "") -> dict:
    """Single Notion to_do checkbox. Link appended inline if URL is provided."""
    rich_text: list[dict] = [{"type": "text", "text": {"content": text}}]
    if url:
        rich_text.append({"type": "text", "text": {"content": f"  → {url}", "link": {"url": url}}})
    return {"object": "block", "type": "to_do", "to_do": {
        "rich_text": rich_text,
        "checked": False,
    }}


def _divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


# ── Block composition ────────────────────────────────────────────────────────

def compose_checklist_blocks(cfg: dict) -> list[dict]:
    name = cfg.get("name", cfg.get("client_id", ""))
    verticals = cfg.get("vertical") or []
    if not isinstance(verticals, list):
        verticals = [verticals]
    verticals = [str(v).strip().lower() for v in verticals]

    canonical_address = cfg.get("canonical_address", "") or cfg.get("address", "")
    canonical_phone   = cfg.get("canonical_phone", "")   or cfg.get("phone", "")
    tracking_exempt   = cfg.get("tracking_phone_directories") or []

    blocks: list[dict] = []

    blocks.append(_heading(f"{name} — Local SEO Setup Checklist", level=1))
    blocks.append(_paragraph(
        "Every item claimed or verified = one step closer to meaningful rank / "
        "review / local-pack data. Tier 1 + applicable Tier 3 should be fully "
        "checked before the team starts paying attention to weekly rank reports."
    ))
    blocks.append(_callout(READY_GATE_NOTES, emoji="🚦"))

    # Canonical NAP reference — team sees this first so every claim uses the same values
    blocks.append(_heading("Canonical NAP — use these exact values on every directory", level=2))
    nap_lines = []
    if canonical_address:
        nap_lines.append(f"Address: {canonical_address}")
    if canonical_phone:
        nap_lines.append(f"Phone (local-citation directories): {canonical_phone}")
    if tracking_exempt:
        nap_lines.append(
            "Tracking-phone exemptions (paid referral directories use unique phones "
            "for lead attribution — NOT the canonical phone above): "
            + ", ".join(tracking_exempt)
        )
    if not nap_lines:
        nap_lines.append(
            "(Canonical NAP not yet set for this client. Populate canonical_address, "
            "canonical_phone, and tracking_phone_directories in clients.json and "
            "re-run this script — or just fill the values here manually.)"
        )
    for line in nap_lines:
        blocks.append(_paragraph(line))
    blocks.append(_divider())

    # Tier 1 — always
    blocks.append(_heading("Tier 1 — Core profiles (required, all clients)", level=2))
    for text, url in TIER_1_CORE:
        blocks.append(_todo(text, url))

    # Tier 2 — healthcare heuristic: run if any vertical is healthcare-adjacent
    healthcare_verticals = {
        "addiction_treatment", "speech_pathology", "occupational_therapy",
        "physical_therapy", "mental_health", "dermatology", "telehealth",
    }
    is_healthcare = any(v in healthcare_verticals for v in verticals)
    if is_healthcare:
        blocks.append(_heading("Tier 2 — Healthcare directories", level=2))
        for text, url in TIER_2_HEALTHCARE:
            blocks.append(_todo(text, url))

    # Tier 3 — per-vertical
    vertical_items: list[tuple[str, str]] = []
    seen_titles: set[str] = set()
    for v in verticals:
        for text, url in TIER_3_BY_VERTICAL.get(v, []):
            if text in seen_titles:
                continue
            seen_titles.add(text)
            vertical_items.append((text, url))
    if vertical_items:
        vertical_label = ", ".join(v.replace("_", " ") for v in verticals)
        blocks.append(_heading(f"Tier 3 — Vertical-specific ({vertical_label})", level=2))
        for text, url in vertical_items:
            blocks.append(_todo(text, url))
    else:
        blocks.append(_heading("Tier 3 — Vertical-specific", level=2))
        blocks.append(_paragraph(
            f"(No vertical-specific directories configured for: "
            f"{', '.join(verticals) or 'none set'}. Add a mapping to TIER_3_BY_VERTICAL in "
            f"scripts/seo/local_setup_init.py when onboarding a new vertical.)"
        ))

    # Tier 4 — always (aggregators apply to everyone)
    blocks.append(_heading("Tier 4 — Data aggregators (free, feed the long tail)", level=2))
    for text, url in TIER_4_AGGREGATORS:
        blocks.append(_todo(text, url))

    # Tier 5 — always
    blocks.append(_heading("Tier 5 — Foundation + tech", level=2))
    for text, url in TIER_5_FOUNDATION:
        blocks.append(_todo(text, url))

    # Tier 6 — always
    blocks.append(_heading("Tier 6 — First-month kickstart", level=2))
    for text, url in TIER_6_KICKSTART:
        blocks.append(_todo(text, url))

    blocks.append(_divider())
    blocks.append(_paragraph(
        "When Tier 1 + Tier 2 + applicable Tier 3 are fully checked, the client "
        "has enough local presence for the rank monitor + competitor auto-discovery "
        "to start producing meaningful signal. Notify the account manager when "
        "this page hits that milestone."
    ))

    return blocks


# ── Client root page resolution ──────────────────────────────────────────────

async def resolve_client_root(notion: NotionClient, cfg: dict) -> str:
    """
    Find the client's root Notion page. Matches the pattern seo_activate uses —
    read the Client Info DB, look at its parent (which is the client root page).
    """
    client_info_db = cfg.get("client_info_db_id", "")
    if not client_info_db:
        return ""
    try:
        db = await notion._client.request(path=f"databases/{client_info_db}", method="GET")
    except Exception as e:
        print(f"  ⚠ couldn't read Client Info DB {client_info_db}: {e}")
        return ""
    parent = db.get("parent", {}) or {}
    if parent.get("type") == "page_id":
        return parent.get("page_id", "")
    return ""


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(client_key: str, dry_run: bool) -> None:
    from config.clients import CLIENTS
    cfg = CLIENTS.get(client_key)
    if not cfg:
        print(f"✗ Client '{client_key}' not found")
        sys.exit(1)

    existing = cfg.get("local_setup_checklist_page_id")
    if existing:
        print(f"Local Setup Checklist already exists for {cfg.get('name', client_key)}: {existing}")
        print("Delete the page in Notion + clear local_setup_checklist_page_id in clients.json to regenerate.")
        return

    notion = NotionClient(settings.notion_api_key)
    print(f"\n── Local Setup Checklist init {'[DRY RUN]' if dry_run else ''} ──")
    print(f"  Client: {cfg.get('name', client_key)}")
    print(f"  Verticals: {cfg.get('vertical', [])}")

    # Resolve root page
    root_page_id = await resolve_client_root(notion, cfg)
    if not root_page_id:
        print("  ✗ Could not resolve client root page (Client Info DB parent)")
        sys.exit(1)
    print(f"  Root page: {root_page_id}")

    # Compose blocks
    blocks = compose_checklist_blocks(cfg)
    print(f"  Composed {len(blocks)} blocks")

    if dry_run:
        print("\n[DRY] Would create page with these section headings:")
        for b in blocks:
            t = b.get("type", "")
            if t.startswith("heading_"):
                content = b[t]["rich_text"][0]["text"]["content"]
                print(f"  [{t}] {content}")
        return

    # Create the page
    page_id = await notion.create_page(
        parent_page_id=root_page_id,
        title=f"{cfg.get('name', client_key)} — Local SEO Setup Checklist",
    )
    print(f"  ✓ Page created: {page_id}")

    # Append blocks in batches (Notion caps at 100 per append)
    for i in range(0, len(blocks), 90):
        await notion.append_blocks(page_id, blocks[i:i + 90])
    print(f"  ✓ {len(blocks)} blocks appended")

    # Persist page_id to clients.json
    try:
        data = json.loads(CLIENTS_JSON_PATH.read_text()) if CLIENTS_JSON_PATH.exists() else {}
        if client_key in data:
            data[client_key]["local_setup_checklist_page_id"] = page_id
            CLIENTS_JSON_PATH.write_text(json.dumps(data, indent=2))
            print(f"  ✓ clients.json updated (local_setup_checklist_page_id)")
        else:
            print(
                f"  ⚠ {client_key} is a _MANUAL entry in config/clients.py — add this manually:\n"
                f'     "local_setup_checklist_page_id": "{page_id}",'
            )
    except Exception as e:
        print(f"  ⚠ couldn't update clients.json: {e}")

    print(f"\nDone. Open the page in Notion to start working through the checklist.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate Local SEO Setup Checklist page for a client")
    parser.add_argument("--client", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(client_key=args.client, dry_run=args.dry_run))
