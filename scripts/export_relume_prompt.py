#!/usr/bin/env python3
"""
export_relume_prompt.py — Export a Relume AI prompt from approved Notion data

Reads the approved mood board, sitemap, brand guidelines, and page content
from Notion, then formats everything into a structured prompt you paste into
Relume AI. Relume uses this to auto-generate a wireframe that matches your
approved site structure, creative direction, and copy.

Usage:
    python scripts/export_relume_prompt.py --client wellwell
    python scripts/export_relume_prompt.py --client wellwell --open

Workflow:
    1. Run this script after content is approved in Notion
    2. Copy the output text (saved to output/<client>/relume_prompt.txt)
    3. Open relume.io → New Project → paste into the AI prompt box
    4. Relume generates the wireframe → export to Figma → push to Webflow
"""
import argparse
import asyncio
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings
from src.integrations.notion import NotionClient

CLIENTS = {
    "wellwell": {
        "name": "WellWell",
        "client_info_db_id": "79c6a439-f369-4a47-af1a-89645fef6f4f",
        "brand_guidelines_db_id": "b7604d57-de3f-455a-a54c-acf3d41fb276",
        "mood_board_db_id": "b2eb103b-a45e-490f-9521-1914780e5fdb",
        "sitemap_db_id": "d70fe7ab-a5f4-4814-9209-4bb6eb05b21a",
        "content_db_id": "330f7f45-333e-81fc-b84f-c92f533cdafd",
        "output_dir": "output/wellwell",
    }
}


def _get_rich_text(prop: dict) -> str:
    return "".join(
        p.get("text", {}).get("content", "") for p in prop.get("rich_text", [])
    )


def _get_title(prop: dict) -> str:
    return "".join(
        p.get("text", {}).get("content", "") for p in prop.get("title", [])
    )


def _get_select(prop: dict) -> str:
    sel = prop.get("select")
    return sel.get("name", "") if sel else ""


def _blocks_to_text(blocks: list[dict]) -> str:
    lines = []
    for block in blocks:
        block_type = block.get("type", "")
        content = block.get(block_type, {})
        rich_text = content.get("rich_text", [])
        text = "".join(
            seg.get("text", {}).get("content", "") for seg in rich_text
        )
        if text:
            lines.append(text)
    return "\n".join(lines)


async def main(client_key: str, open_output: bool) -> None:
    cfg = CLIENTS[client_key]
    notion = NotionClient(settings.notion_api_key)

    print(f"Building Relume prompt for {cfg['name']}...")

    # ── 1. Client info ────────────────────────────────────────────────────────
    client_entries = await notion.query_database(cfg["client_info_db_id"])
    client_props = client_entries[0]["properties"] if client_entries else {}
    company = _get_rich_text(client_props.get("Company", {})) or cfg["name"]
    client_notes = _get_rich_text(client_props.get("Notes", {}))
    print(f"  ✓ Client info loaded")

    # ── 2. Brand guidelines ───────────────────────────────────────────────────
    primary_font = "Quicksand"
    secondary_font = "DM Sans"
    tone_descriptors = ""
    brand_summary = ""

    brand_entries = await notion.query_database(cfg["brand_guidelines_db_id"])
    if brand_entries:
        bp = brand_entries[0]["properties"]
        primary_font = _get_rich_text(bp.get("Primary Font", {})) or primary_font
        secondary_font = _get_rich_text(bp.get("Secondary Font", {})) or secondary_font
        tone_descriptors = _get_rich_text(bp.get("Tone Descriptors", {}))
        raw = _get_rich_text(bp.get("Raw Guidelines", {}))
        brand_summary = raw[:800] if raw else ""
    print(f"  ✓ Brand guidelines loaded")

    # ── 3. Approved mood board ────────────────────────────────────────────────
    creative_direction = ""
    color_palette = ""
    style_keywords = ""

    mood_entries = await notion.query_database(cfg["mood_board_db_id"])
    if mood_entries:
        approved = next(
            (e for e in mood_entries
             if _get_select(e["properties"].get("Status", {})) == "Approved"),
            None
        ) or next(
            (e for e in mood_entries
             if _get_select(e["properties"].get("Status", {})) == "Pending Review"),
            None
        ) or mood_entries[0]

        mp = approved["properties"]
        variation = _get_select(mp.get("Variation", {}))
        concept = "".join(
            p.get("text", {}).get("content", "")
            for p in mp.get("Name", {}).get("title", [])
        )
        style_keywords = _get_rich_text(mp.get("Style Keywords", {}))
        color_palette = _get_rich_text(mp.get("Color Palette Description", {}))
        status = _get_select(mp.get("Status", {}))
        creative_direction = f"{concept} ({status})"
        print(f"  ✓ Mood board loaded: {creative_direction}")

    # ── 4. Sitemap pages ──────────────────────────────────────────────────────
    sitemap_entries = await notion.query_database(
        cfg["sitemap_db_id"],
        sorts=[{"property": "Order", "direction": "ascending"}],
    )
    print(f"  ✓ Sitemap loaded: {len(sitemap_entries)} pages")

    # ── 5. Content (H1s + hero headlines) ────────────────────────────────────
    content_map: dict[str, dict] = {}  # slug → {h1, title_tag, status}
    unapproved_pages: list[str] = []

    if cfg.get("content_db_id"):
        content_entries = await notion.query_database(cfg["content_db_id"])
        for entry in content_entries:
            ep = entry["properties"]
            slug = _get_rich_text(ep.get("Slug", {}))
            title = (
                _get_title(ep.get("Page Title", {}))
                or _get_title(ep.get("Name", {}))
                or ""
            )
            h1 = _get_rich_text(ep.get("H1", {}))
            title_tag = _get_rich_text(ep.get("Title Tag", {}))
            status = _get_select(ep.get("Status", {}))
            if slug:
                content_map[slug] = {
                    "h1": h1,
                    "title_tag": title_tag,
                    "status": status,
                    "title": title,
                }
            if status not in ("Approved", "Client Review"):
                unapproved_pages.append(title or slug)

        print(f"  ✓ Content loaded: {len(content_map)} pages")
        if unapproved_pages:
            print(
                f"  ⚠ Warning: {len(unapproved_pages)} pages not yet approved "
                f"({', '.join(unapproved_pages[:5])}"
                f"{'...' if len(unapproved_pages) > 5 else ''})"
            )
            print(f"    Proceeding anyway — review before pasting into Relume.")

    # ── 6. Build the prompt (target: under 5,000 characters) ─────────────────

    # Business intro (keep tight)
    intro = (
        f"{company} is a boutique telehealth medical practice. Services: "
        f"Teledermatology (virtual, nationwide), Medical Dermatology "
        f"(in-person, Savannah GA), GLP-1 Weight Loss Management (virtual), "
        f"Neurotoxin Therapy/Botox (in-person, Savannah GA + mobile). "
        f"Founder: board-certified PA-C. Target: professional women 25-55. "
        f"Tone: warm, clinically credible, boutique, elevated. "
        f"Fonts: {primary_font} headings, {secondary_font} body. "
        f"Colors: teal primary, blush accent, warm ivory background, brass detail. "
        f"Feel: Curology meets Hims & Hers feminine — not corporate, not spa."
    )

    # Page list — title + section NAMES only (no descriptions)
    page_lines: list[str] = []
    for entry in sitemap_entries:
        pp = entry["properties"]
        title = (
            _get_title(pp.get("Name", {}))
            or _get_title(pp.get("Page Title", {}))
            or _get_rich_text(pp.get("Name", {}))
            or "Untitled"
        )
        slug = _get_rich_text(pp.get("Slug", {}))
        page_type = _get_select(pp.get("Page Type", {}))
        key_sections = _get_rich_text(pp.get("Key Sections", {}))

        # Pull H1 from content if available
        h1 = content_map.get(slug, {}).get("h1", "")

        # Extract section names only — strip descriptions after " — " or ":"
        raw_sections = [
            s.strip().lstrip("•–- ").strip()
            for s in key_sections.split("\n")
            if s.strip().lstrip("•–- ").strip()
        ]
        section_names = []
        for s in raw_sections:
            # Keep only the name before " — " or ":"
            name = re.split(r"\s[—–-]\s|:\s", s)[0].strip()
            if name and len(name) < 40:
                section_names.append(name)

        cms_tag = " [CMS]" if page_type == "CMS" else ""
        h1_part = f': "{h1}"' if h1 else ""
        sections_part = (
            f" [{', '.join(section_names[:5])}]" if section_names else ""
        )
        page_lines.append(f"{title}{cms_tag}{h1_part}{sections_part}")

    pages_block = "\n".join(page_lines)

    prompt_text = f"""{intro}

Build a website for this telehealth practice. Pages:

{pages_block}

Requirements: mobile-first, Webflow-native (no custom code), Blog and Conditions Treated and Location SEO pages as Webflow CMS collections, booking CTAs link to external TEBRA EMR system."""

    # Trim to 4,900 chars to stay safely under limit
    if len(prompt_text) > 4900:
        prompt_text = prompt_text[:4897] + "..."

    # ── 7. Save to file ───────────────────────────────────────────────────────
    out_dir = Path(__file__).parent.parent / cfg["output_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "relume_prompt.txt"
    out_path.write_text(prompt_text)

    print(f"\n✓ Relume prompt saved to: {out_path}")
    print(f"  Pages: {len(sitemap_entries)}  |  Content entries: {len(content_map)}")
    print(f"\nNext steps:")
    print(f"  1. Open relume.io and create a new project")
    print(f"  2. Paste the contents of relume_prompt.txt into the Relume AI prompt")
    print(f"  3. Review the generated wireframe, adjust as needed")
    print(f"  4. Export to Figma via the Relume Figma plugin")
    print(f"  5. Review with client in Figma, approve, then push to Webflow")

    if open_output:
        import subprocess
        subprocess.run(["open", str(out_path)])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Export Relume AI prompt from approved Notion data"
    )
    parser.add_argument("--client", default="wellwell")
    parser.add_argument(
        "--open", action="store_true",
        help="Open the output file after export"
    )
    args = parser.parse_args()
    asyncio.run(main(args.client, args.open))
