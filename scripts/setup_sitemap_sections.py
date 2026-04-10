#!/usr/bin/env python3
"""
setup_sitemap_sections.py — Add Section field to a client's Sitemap DB
and categorize all existing pages.

Usage:
    python scripts/setup_sitemap_sections.py --client summit_therapy

Sections:
    Core                  Home, About, Team, Careers, Contact
    Services              Service hub pages (Speech Therapy, OT, PT)
    Service Subcategories Individual subcategory pages
    Who We Serve          Audience gateway pages (Children, Adults)
    Locations             Location hub + individual clinic pages
    Programs              Enrichment programs, concierge, etc.
    Patient Resources     Insurance, New Patients, Book, Testimonials
    Blog                  Blog hub, post template, conditions template
    Legal                 Privacy, Terms, Accessibility, Sitemap page
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.clients import CLIENTS
from src.config import settings
from src.integrations.notion import NotionClient

SECTION_OPTIONS = [
    "Core",
    "Services",
    "Service Subcategories",
    "Who We Serve",
    "Locations",
    "Programs",
    "Patient Resources",
    "Blog",
    "Legal",
]


def _infer_section(slug: str, title: str) -> str:
    """Infer section from slug and title."""
    s = slug.lower().strip("/")
    t = title.lower()

    if s == "" or s == "/":
        return "Core"
    if s.startswith("who-we-serve"):
        return "Who We Serve"
    if s.startswith("locations"):
        return "Locations"
    if s.startswith("blog") or s.startswith("conditions"):
        return "Blog"
    if s.startswith("services/speech-therapy/") or s.startswith("services/occupational-therapy/") or s.startswith("services/physical-therapy/"):
        return "Service Subcategories"
    if s.startswith("services"):
        return "Services"
    if s in ("privacy-policy", "terms", "accessibility", "sitemap", "cookie-policy"):
        return "Legal"
    if s in ("insurance", "new-patients", "book", "testimonials"):
        return "Patient Resources"
    if any(k in t for k in ("enrichment", "program", "concierge", "summer")):
        return "Programs"
    if s in ("about", "team", "careers", "contact"):
        return "Core"
    # fallback: title-based
    if any(k in t for k in ("privacy", "terms", "legal", "accessib", "sitemap", "cookie")):
        return "Legal"
    return "Core"


async def main(client_key: str) -> None:
    cfg = CLIENTS[client_key]
    sitemap_db_id = cfg["sitemap_db_id"]
    notion = NotionClient(settings.notion_api_key)

    # ── Step 1: Add Section property to the DB ────────────────────────────────
    print(f"Adding Section field to Sitemap DB ({sitemap_db_id})...")
    await notion._client.request(
        path=f"databases/{sitemap_db_id}",
        method="PATCH",
        body={
            "properties": {
                "Section": {
                    "select": {
                        "options": [
                            {"name": name, "color": color}
                            for name, color in [
                                ("Core", "blue"),
                                ("Services", "green"),
                                ("Service Subcategories", "purple"),
                                ("Who We Serve", "orange"),
                                ("Locations", "yellow"),
                                ("Programs", "pink"),
                                ("Patient Resources", "gray"),
                                ("Blog", "red"),
                                ("Legal", "default"),
                            ]
                        ]
                    }
                }
            }
        },
    )
    print("  ✓ Section field added")

    # ── Step 2: Query all existing pages ─────────────────────────────────────
    print("Fetching existing sitemap pages...")
    pages = await notion.query_database(sitemap_db_id)
    print(f"  Found {len(pages)} pages")

    # ── Step 3: Update each page with its section ─────────────────────────────
    updated = 0
    for page in pages:
        props = page.get("properties", {})
        slug = "".join(
            p.get("text", {}).get("content", "")
            for p in props.get("Slug", {}).get("rich_text", [])
        )
        title_parts = props.get("Page Title", {}).get("title", [])
        title = "".join(p.get("text", {}).get("content", "") for p in title_parts)
        section = _infer_section(slug, title)

        await notion._client.request(
            path=f"pages/{page['id']}",
            method="PATCH",
            body={"properties": {"Section": {"select": {"name": section}}}},
        )
        print(f"  ✓ [{section}] {title} ({slug})")
        updated += 1

    print(f"\nDone — {updated} pages categorized.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", required=True)
    args = parser.parse_args()
    asyncio.run(main(args.client))
