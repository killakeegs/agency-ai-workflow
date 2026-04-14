#!/usr/bin/env python3
"""
export_brand_guidelines.py — Export brand guidelines JSON from the approved mood board

Reads the approved (or top-pick) mood board entry from Notion, combines it with
Brand Guidelines DB data, and outputs brand_guidelines_data.json for the Figma plugin.

Usage:
    python scripts/export_brand_guidelines.py --client wellwell
    python scripts/export_brand_guidelines.py --client wellwell --open
"""
import argparse
import asyncio
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings
from src.integrations.notion import NotionClient

CLIENTS = {
    "wellwell": {
        "name": "WellWell",
        "mood_board_db_id": "b2eb103b-a45e-490f-9521-1914780e5fdb",
        "brand_guidelines_db_id": "b7604d57-de3f-455a-a54c-acf3d41fb276",
        "output_dir": "output/wellwell",
    }
}


def _get_rich_text(prop: dict) -> str:
    return "".join(p.get("text", {}).get("content", "") for p in prop.get("rich_text", []))


def _get_select(prop: dict) -> str:
    sel = prop.get("select")
    return sel.get("name", "") if sel else ""


def _blocks_to_text(blocks: list[dict]) -> str:
    lines = []
    for block in blocks:
        bt = block.get("type", "")
        rt = block.get(bt, {}).get("rich_text", [])
        text = "".join(s.get("text", {}).get("content", "") for s in rt)
        if text:
            lines.append(text)
    return "\n".join(lines)


def _extract_hex_codes(text: str) -> list[str]:
    return re.findall(r"#[0-9A-Fa-f]{6}", text)


def _parse_colors(palette_desc: str, block_text: str) -> list[dict]:
    """Extract color swatches with labels and hex codes."""
    colors = []
    combined = palette_desc + "\n" + block_text
    lines = combined.split("\n")
    for line in lines:
        hexes = _extract_hex_codes(line)
        if hexes:
            label = re.sub(r"#[0-9A-Fa-f]{6}.*", "", line).strip()
            label = re.sub(r"^[•\-–:]+\s*", "", label).strip()
            if not label:
                label = "Color"
            colors.append({"hex": hexes[0], "name": label.split("|")[0].strip()[:30], "role": ""})
    # Deduplicate
    seen, unique = set(), []
    for c in colors:
        if c["hex"] not in seen:
            seen.add(c["hex"]); unique.append(c)
    if not unique:
        unique = [
            {"hex": "#2BA8A4", "name": "Primary Teal",  "role": "Primary brand color"},
            {"hex": "#F4ECE8", "name": "Soft Blush",    "role": "Accent / warmth"},
            {"hex": "#C9A96E", "name": "Warm Brass",    "role": "Premium accent"},
            {"hex": "#FAFAF8", "name": "Off-White",     "role": "Background"},
            {"hex": "#1C2B3A", "name": "Deep Navy",     "role": "Text / depth"},
        ]
    return unique[:5]


def _extract_fonts(text: str) -> tuple[str, str]:
    primary, secondary = "Quicksand", "Inter"
    for line in text.split("\n"):
        if "Primary:" in line:
            m = re.search(r"Primary:\s*([A-Za-z\s]+?)(?:\s*[-–(]|$)", line)
            if m: primary = m.group(1).strip()
        if "Secondary:" in line:
            m = re.search(r"Secondary:\s*([A-Za-z\s]+?)(?:\s*[-–(]|$)", line)
            if m: secondary = m.group(1).strip()
    return primary, secondary


def _extract_tone(text: str, style_keywords: str) -> list[str]:
    """Extract tone descriptors from block text or fall back to style keywords."""
    tone = []
    for line in text.split("\n"):
        if any(kw in line.lower() for kw in ["tone", "voice", "brand personality"]):
            words = [w.strip(" ,·|–-") for w in re.split(r"[,·|]+", line) if len(w.strip()) > 2]
            tone.extend([w for w in words if w and len(w) < 30][:4])
            if tone:
                break
    if not tone and style_keywords:
        tone = [k.strip().title() for k in re.split(r"[,·|]+", style_keywords) if k.strip()][:4]
    return tone or ["Knowledgeable", "Warm", "Elevated", "Trustworthy"]


async def main(client_key: str, open_output: bool) -> None:
    cfg = CLIENTS[client_key]
    notion = NotionClient(settings.notion_api_key)

    print(f"Fetching mood board entries for {cfg['name']}...")
    mood_entries = await notion.query_database(cfg["mood_board_db_id"])

    # Pick approved → pending review → first entry
    approved = next(
        (e for e in mood_entries if _get_select(e["properties"].get("Status", {})) == "Approved"), None
    ) or next(
        (e for e in mood_entries if _get_select(e["properties"].get("Status", {})) == "Pending Review"), None
    ) or (mood_entries[0] if mood_entries else None)

    if not approved:
        print("ERROR: No mood board entries found.")
        sys.exit(1)

    props = approved["properties"]
    option = _get_select(props.get("Variation", {})) or "Option A"
    concept_name_full = "".join(
        p.get("text", {}).get("content", "")
        for p in props.get("Name", {}).get("title", [])
    )
    concept_name = re.sub(r"^Option [A-F]\s*[—-]\s*", "", concept_name_full).strip()
    palette_desc = _get_rich_text(props.get("Color Palette Description", {}))
    style_keywords = _get_rich_text(props.get("Style Keywords", {}))
    status = _get_select(props.get("Status", {}))

    print(f"  Using: {option} — {concept_name} ({status})")
    blocks = await notion.get_block_children(approved["id"])
    block_text = _blocks_to_text(blocks)

    colors = _parse_colors(palette_desc, block_text)
    primary_font, secondary_font = _extract_fonts(block_text)
    tone = _extract_tone(block_text, style_keywords)

    # Brand guidelines DB for tone/positioning
    positioning = ""
    try:
        brand_entries = await notion.query_database(cfg["brand_guidelines_db_id"])
        if brand_entries:
            bp = brand_entries[0]["properties"]
            raw = _get_rich_text(bp.get("Raw Guidelines", {}))
            tone_raw = _get_rich_text(bp.get("Tone Descriptors", {}))
            if tone_raw:
                tone = [t.strip() for t in re.split(r"[,·|]+", tone_raw) if t.strip()][:5]
            # Extract positioning from raw guidelines (first meaningful sentence)
            for line in raw.split("\n"):
                if len(line.strip()) > 40:
                    positioning = line.strip()[:120]
                    break
    except Exception as e:
        print(f"  Warning: could not read brand guidelines DB — {e}")

    output_data = {
        "client": cfg["name"],
        "generated_at": date.today().isoformat(),
        "approved_variation": f"{option} — {concept_name}",
        "colors": colors,
        "typography": {
            "heading": {"family": primary_font,   "weights": ["600", "700"]},
            "body":    {"family": secondary_font,  "weights": ["400", "500"]},
        },
        "tone": tone,
        "positioning": positioning or f"Boutique telehealth combining clinical expertise with approachable, elevated wellness.",
        "style_keywords": style_keywords,
    }

    out_dir = Path(__file__).parent.parent / cfg["output_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "brand_guidelines_data.json"
    out_path.write_text(json.dumps(output_data, indent=2))
    print(f"\n✓ Brand guidelines JSON saved to: {out_path}")
    print(f"  Colors: {len(colors)}  |  Heading: {primary_font}  |  Body: {secondary_font}")
    print(f"  Tone: {', '.join(tone)}")

    if open_output:
        subprocess.run(["open", str(out_dir)])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", default="wellwell")
    parser.add_argument("--open", action="store_true", help="Open output folder after export")
    args = parser.parse_args()
    asyncio.run(main(args.client, args.open))
