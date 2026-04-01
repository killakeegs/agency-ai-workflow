#!/usr/bin/env python3
"""
generate_mood_board_visuals.py — Generate visual HTML mood boards from Notion data

Reads all Mood Board DB entries for a client, extracts colors/fonts/details
from the page blocks Claude wrote, and renders a professional HTML deck
with real color swatches, Google Fonts, and layout previews.

Usage:
    python scripts/generate_mood_board_visuals.py --client wellwell
    python scripts/generate_mood_board_visuals.py --client wellwell --open
"""
import argparse
import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings
from src.integrations.notion import NotionClient

CLIENTS = {
    "wellwell": {
        "name": "WellWell",
        "mood_board_db_id": "b2eb103b-a45e-490f-9521-1914780e5fdb",
        "output_file": "output/wellwell/mood_boards.html",
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
        content = block.get(bt, {})
        rt = content.get("rich_text", [])
        text = "".join(s.get("text", {}).get("content", "") for s in rt)
        if text:
            lines.append(text)
    return "\n".join(lines)


def _extract_hex_codes(text: str) -> list[str]:
    """Pull all hex color codes from a string."""
    return re.findall(r"#[0-9A-Fa-f]{6}", text)


def _extract_section(text: str, heading: str) -> str:
    """Extract content under a given heading from block text."""
    lines = text.split("\n")
    capturing = False
    result = []
    for line in lines:
        if heading.lower() in line.lower():
            capturing = True
            continue
        if capturing:
            if any(h in line for h in ["──", "Color", "Typography", "Imagery", "Sample", "Reference",
                                        "Target", "Best Prac", "Strength", "Risk", "Suited", "Recommendation"]):
                if result:
                    break
            if line.strip():
                result.append(line.strip().lstrip("•✓✗⚠").strip())
    return "\n".join(result[:6])  # limit to 6 lines


def _extract_fonts(text: str) -> tuple[str, str]:
    """Extract primary and secondary font names from block text."""
    primary = "Quicksand"
    secondary = "Inter"
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if "Primary:" in line and any(
            f in line for f in ["Quicksand", "Poppins", "DM Sans", "Raleway", "Lato",
                                  "Nunito", "Montserrat", "Playfair", "Cormorant",
                                  "Source Sans", "Outfit", "Plus Jakarta"]
        ):
            # Extract font name
            m = re.search(r"Primary:\s*([A-Za-z\s]+?)(?:\s*[-–(]|$)", line)
            if m:
                primary = m.group(1).strip()
        if "Secondary:" in line and any(
            f in line for f in ["Quicksand", "Poppins", "DM Sans", "Raleway", "Lato",
                                  "Nunito", "Montserrat", "Playfair", "Cormorant",
                                  "Source Sans", "Inter", "Outfit", "Plus Jakarta"]
        ):
            m = re.search(r"Secondary:\s*([A-Za-z\s]+?)(?:\s*[-–(]|$)", line)
            if m:
                secondary = m.group(1).strip()
    return primary, secondary


def _extract_headline(text: str) -> str:
    """Extract the sample hero headline."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if "Sample Hero Headline" in line or "sample_headline" in line:
            if i + 1 < len(lines):
                h = lines[i + 1].strip().strip('"').strip('"').strip('"')
                if h:
                    return h
    return "Expert care. Your terms."


def _extract_scores(text: str) -> dict:
    """Extract best practices scores."""
    scores = {}
    patterns = {
        "Trust": r"Trust[^:]*:\s*(?:Score\s*)?(\d+)/10",
        "Conversion": r"Conversion[^:]*:\s*(?:Score\s*)?(\d+)/10",
        "Accessibility": r"Accessibility[^:]*:\s*(?:Score\s*)?(\d+)/10",
        "Brand Fit": r"Brand[^:]*:\s*(?:Score\s*)?(\d+)/10",
        "Differentiation": r"Differ[^:]*:\s*(?:Score\s*)?(\d+)/10",
    }
    for label, pattern in patterns.items():
        m = re.search(pattern, text, re.IGNORECASE)
        scores[label] = int(m.group(1)) if m else 7
    return scores


def _parse_colors(palette_desc: str, block_text: str) -> list[dict]:
    """Build a list of color dicts from palette description and block text."""
    colors = []
    # Try parsing from block text first (more detailed)
    color_section = _extract_section(block_text, "Color Palette")
    combined = palette_desc + "\n" + color_section

    # Extract hex + label pairs
    lines = combined.split("\n")
    for line in lines:
        hexes = _extract_hex_codes(line)
        if hexes:
            # Get label: everything before the hex code, cleaned up
            label = re.sub(r"#[0-9A-Fa-f]{6}.*", "", line).strip()
            label = re.sub(r"^[•\-–:]+\s*", "", label).strip()
            label = re.sub(r"(Primary|Secondary|Accent|Background|BG|Text)[\s:]*", r"\1", label)
            if not label:
                label = "Color"
            for hex_code in hexes[:1]:  # one hex per line
                colors.append({"hex": hex_code, "label": label.split("|")[0].strip()[:30]})

    # Deduplicate by hex
    seen = set()
    unique = []
    for c in colors:
        if c["hex"] not in seen:
            seen.add(c["hex"])
            unique.append(c)

    # Ensure we have at least some colors
    if not unique:
        unique = [
            {"hex": "#2BA8A4", "label": "Primary Teal"},
            {"hex": "#F4ECE8", "label": "Soft Blush"},
            {"hex": "#C9A96E", "label": "Warm Brass"},
            {"hex": "#FAFAF8", "label": "Off-White"},
            {"hex": "#1C2B3A", "label": "Deep Navy"},
        ]

    return unique[:6]


def _google_fonts_url(fonts: list[str]) -> str:
    """Build Google Fonts CDN URL for a list of font names."""
    families = []
    for font in fonts:
        cleaned = font.strip().replace(" ", "+")
        families.append(f"family={cleaned}:wght@300;400;500;600;700")
    return "https://fonts.googleapis.com/css2?" + "&".join(families) + "&display=swap"


def _option_badge_color(option: str) -> str:
    return {
        "Option A": "#2BA8A4",
        "Option B": "#4A6FA5",
        "Option C": "#C9A96E",
        "Option D": "#1C2B3A",
    }.get(option, "#666")


def _status_badge(status: str) -> str:
    if status == "Pending Review":
        return '<span style="background:#2BA8A4;color:white;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600;letter-spacing:.5px">★ AGENCY PICK</span>'
    return ""


def _score_bar(score: int) -> str:
    pct = score * 10
    color = "#2BA8A4" if score >= 8 else "#C9A96E" if score >= 6 else "#e74c3c"
    return f"""
    <div style="display:flex;align-items:center;gap:8px;margin:4px 0">
      <div style="flex:1;background:#eee;border-radius:4px;height:6px;overflow:hidden">
        <div style="width:{pct}%;height:100%;background:{color};border-radius:4px"></div>
      </div>
      <span style="font-size:12px;font-weight:600;color:{color};min-width:24px">{score}/10</span>
    </div>"""


def render_html(client_name: str, variations: list[dict]) -> str:
    # Collect all unique fonts
    all_fonts = set(["Quicksand", "Inter"])
    for v in variations:
        all_fonts.add(v["primary_font"])
        all_fonts.add(v["secondary_font"])
    fonts_url = _google_fonts_url(list(all_fonts))

    # Build variation tabs HTML
    tabs_html = ""
    panels_html = ""

    for i, v in enumerate(variations):
        option = v["option"]
        is_active = i == 0
        badge_color = _option_badge_color(option)

        tabs_html += f"""
        <button class="tab {'active' if is_active else ''}"
                onclick="showTab({i})"
                data-index="{i}"
                style="border-left:3px solid {'transparent' if not is_active else badge_color}">
          <span style="font-size:11px;color:#999;display:block;margin-bottom:2px">{option}</span>
          <span style="font-size:13px;font-weight:600;color:#1C2B3A">{v['concept_name']}</span>
          {_status_badge(v['status'])}
        </button>"""

        # Color swatches
        swatches = ""
        for c in v["colors"]:
            r, g, b = int(c["hex"][1:3], 16), int(c["hex"][3:5], 16), int(c["hex"][5:7], 16)
            text_color = "#fff" if (r * 0.299 + g * 0.587 + b * 0.114) < 128 else "#1C2B3A"
            swatches += f"""
            <div style="flex:1;min-width:80px">
              <div style="background:{c['hex']};height:80px;border-radius:8px;
                          display:flex;align-items:flex-end;padding:8px;
                          border:1px solid rgba(0,0,0,.06)">
                <span style="font-size:10px;font-weight:600;color:{text_color};
                             font-family:monospace">{c['hex']}</span>
              </div>
              <div style="font-size:11px;color:#666;margin-top:5px;text-align:center;
                          font-weight:500">{c['label']}</div>
            </div>"""

        # Score bars
        score_bars = ""
        for label, score in v["scores"].items():
            score_bars += f"""
            <div style="margin-bottom:8px">
              <div style="font-size:11px;color:#666;margin-bottom:3px;font-weight:500">{label}</div>
              {_score_bar(score)}
            </div>"""

        # References
        refs_html = ""
        for ref in v["references"][:4]:
            refs_html += f'<li style="margin-bottom:4px;color:#555">{ref}</li>'

        # Strengths
        strengths_html = ""
        for s in v["strengths"][:3]:
            strengths_html += f'<li style="margin-bottom:4px;color:#2BA8A4">✓ {s}</li>'

        # Risks
        risks_html = ""
        for r in v["risks"][:2]:
            risks_html += f'<li style="margin-bottom:4px;color:#e67e22">⚠ {r}</li>'

        primary_font = v["primary_font"]
        secondary_font = v["secondary_font"]

        panels_html += f"""
        <div class="panel {'active' if is_active else ''}" id="panel-{i}">
          <!-- Hero Preview -->
          <div style="background:{v['hero_bg']};border-radius:12px;padding:48px 40px;
                      margin-bottom:28px;position:relative;overflow:hidden">
            <div style="position:absolute;top:0;right:0;width:40%;height:100%;
                        background:linear-gradient(135deg, {v['colors'][0]['hex']}22, {v['colors'][2]['hex'] if len(v['colors'])>2 else v['colors'][0]['hex']}33)">
            </div>
            <div style="position:relative;z-index:1;max-width:60%">
              <div style="font-family:'{secondary_font}',sans-serif;font-size:11px;
                          letter-spacing:2px;text-transform:uppercase;
                          color:{v['colors'][0]['hex']};margin-bottom:12px;font-weight:600">
                {option} — {v['concept_name']}
              </div>
              <h1 style="font-family:'{primary_font}',sans-serif;font-size:36px;
                         font-weight:700;color:#1C2B3A;line-height:1.2;margin:0 0 16px">
                {v['headline']}
              </h1>
              <p style="font-family:'{secondary_font}',sans-serif;font-size:15px;
                        color:#555;line-height:1.6;margin:0 0 24px">
                {v['concept_description'][:180]}...
              </p>
              <div style="display:inline-block;background:{v['colors'][0]['hex']};
                          color:white;padding:12px 28px;border-radius:6px;
                          font-family:'{primary_font}',sans-serif;font-size:14px;
                          font-weight:600;cursor:pointer">
                Book a Consultation
              </div>
            </div>
          </div>

          <div style="display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-bottom:24px">
            <!-- Color Palette -->
            <div style="background:white;border-radius:10px;padding:24px;
                        border:1px solid #eee">
              <h3 style="font-family:'{primary_font}',sans-serif;font-size:14px;
                         font-weight:700;color:#1C2B3A;margin:0 0 16px;
                         text-transform:uppercase;letter-spacing:1px">Color Palette</h3>
              <div style="display:flex;gap:8px;flex-wrap:wrap">
                {swatches}
              </div>
            </div>

            <!-- Typography -->
            <div style="background:white;border-radius:10px;padding:24px;
                        border:1px solid #eee">
              <h3 style="font-family:'{primary_font}',sans-serif;font-size:14px;
                         font-weight:700;color:#1C2B3A;margin:0 0 16px;
                         text-transform:uppercase;letter-spacing:1px">Typography</h3>
              <div style="font-family:'{primary_font}',sans-serif;font-size:28px;
                          font-weight:700;color:#1C2B3A;margin-bottom:4px">
                {primary_font}
              </div>
              <div style="font-size:12px;color:#999;margin-bottom:12px">Headlines &amp; CTAs</div>
              <div style="font-family:'{secondary_font}',sans-serif;font-size:18px;
                          font-weight:400;color:#555;margin-bottom:4px">
                {secondary_font}
              </div>
              <div style="font-size:12px;color:#999;margin-bottom:12px">Body &amp; Navigation</div>
              <div style="font-size:12px;color:#888;line-height:1.5;
                          font-family:'{secondary_font}',sans-serif">
                {v['pairing_rationale'][:120]}...
              </div>
            </div>
          </div>

          <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:24px">
            <!-- Best Practices -->
            <div style="background:white;border-radius:10px;padding:24px;border:1px solid #eee">
              <h3 style="font-family:'{primary_font}',sans-serif;font-size:13px;
                         font-weight:700;color:#1C2B3A;margin:0 0 14px;
                         text-transform:uppercase;letter-spacing:1px">Best Practices</h3>
              {score_bars}
            </div>

            <!-- References + Strengths -->
            <div style="background:white;border-radius:10px;padding:24px;border:1px solid #eee">
              <h3 style="font-family:'{primary_font}',sans-serif;font-size:13px;
                         font-weight:700;color:#1C2B3A;margin:0 0 12px;
                         text-transform:uppercase;letter-spacing:1px">References</h3>
              <ul style="margin:0 0 16px;padding-left:16px;font-size:12px">{refs_html}</ul>
              <h3 style="font-family:'{primary_font}',sans-serif;font-size:13px;
                         font-weight:700;color:#1C2B3A;margin:0 0 8px;
                         text-transform:uppercase;letter-spacing:1px">Strengths</h3>
              <ul style="margin:0;padding-left:16px;font-size:12px">{strengths_html}</ul>
            </div>

            <!-- Risks + Audience -->
            <div style="background:white;border-radius:10px;padding:24px;border:1px solid #eee">
              <h3 style="font-family:'{primary_font}',sans-serif;font-size:13px;
                         font-weight:700;color:#1C2B3A;margin:0 0 8px;
                         text-transform:uppercase;letter-spacing:1px">Watch Outs</h3>
              <ul style="margin:0 0 14px;padding-left:16px;font-size:12px">{risks_html}</ul>
              <h3 style="font-family:'{primary_font}',sans-serif;font-size:13px;
                         font-weight:700;color:#1C2B3A;margin:0 0 8px;
                         text-transform:uppercase;letter-spacing:1px">Best For</h3>
              <p style="font-size:12px;color:#555;margin:0;line-height:1.5">
                {v['target_fit'][:160]}
              </p>
            </div>
          </div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{client_name} — Mood Board Options</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="{fonts_url}" rel="stylesheet">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: 'Inter', -apple-system, sans-serif;
    background: #F7F7F5;
    color: #1C2B3A;
    min-height: 100vh;
  }}
  .header {{
    background: white;
    border-bottom: 1px solid #eee;
    padding: 20px 40px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    position: sticky;
    top: 0;
    z-index: 100;
  }}
  .header-title {{
    font-family: 'Quicksand', sans-serif;
    font-size: 20px;
    font-weight: 700;
    color: #1C2B3A;
  }}
  .header-subtitle {{
    font-size: 12px;
    color: #999;
    margin-top: 2px;
  }}
  .header-meta {{
    font-size: 12px;
    color: #999;
    text-align: right;
  }}
  .layout {{
    display: flex;
    max-width: 1400px;
    margin: 0 auto;
    padding: 32px 24px;
    gap: 24px;
  }}
  .sidebar {{
    width: 220px;
    flex-shrink: 0;
  }}
  .sidebar-label {{
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    color: #999;
    padding: 0 16px;
    margin-bottom: 8px;
  }}
  .tab {{
    display: block;
    width: 100%;
    background: white;
    border: 1px solid #eee;
    border-radius: 8px;
    padding: 14px 16px;
    margin-bottom: 8px;
    cursor: pointer;
    text-align: left;
    transition: all .15s;
  }}
  .tab:hover {{ background: #f9f9f9; }}
  .tab.active {{
    background: white;
    border-color: #ddd;
    box-shadow: 0 2px 8px rgba(0,0,0,.08);
  }}
  .main {{ flex: 1; min-width: 0; }}
  .panel {{ display: none; }}
  .panel.active {{ display: block; }}
  ul {{ list-style: none; padding: 0; }}
  ul li::before {{ content: ''; }}
</style>
</head>
<body>

<div class="header">
  <div>
    <div class="header-title">{client_name} — Mood Board Review</div>
    <div class="header-subtitle">4 creative directions · Select one to advance to build phase</div>
  </div>
  <div class="header-meta">
    Generated by RxMedia AI Pipeline<br>
    {len(variations)} variations · Click a tab to preview
  </div>
</div>

<div class="layout">
  <div class="sidebar">
    <div class="sidebar-label">Options</div>
    {tabs_html}
  </div>
  <div class="main">
    {panels_html}
  </div>
</div>

<script>
function showTab(index) {{
  document.querySelectorAll('.tab').forEach((t, i) => t.classList.toggle('active', i === index));
  document.querySelectorAll('.panel').forEach((p, i) => p.classList.toggle('active', i === index));
  // Update border color on active tab
  const colors = {[repr(_option_badge_color(f'Option {chr(65+i)}')) for i in range(len(variations))]};
  document.querySelectorAll('.tab').forEach((t, i) => {{
    t.style.borderLeftColor = i === index ? Object.values({{'0':'#2BA8A4','1':'#4A6FA5','2':'#C9A96E','3':'#1C2B3A'}})[i] : 'transparent';
  }});
}}
</script>

</body>
</html>"""


async def main(client_key: str, open_browser: bool) -> None:
    cfg = CLIENTS[client_key]
    notion = NotionClient(settings.notion_api_key)

    print(f"Fetching mood board entries for {cfg['name']}...")
    entries = await notion.query_database(cfg["mood_board_db_id"])
    print(f"Found {len(entries)} variations")

    variations = []
    for entry in entries:
        props = entry["properties"]
        page_id = entry["id"]

        option = _get_select(props.get("Variation", {}))
        concept_name_full = "".join(
            p.get("text", {}).get("content", "")
            for p in props.get("Name", {}).get("title", [])
        )
        # Strip "Option X — " prefix to get just the concept name
        concept_name = re.sub(r"^Option [A-F]\s*[—-]\s*", "", concept_name_full).strip()
        status = _get_select(props.get("Status", {}))
        palette_desc = _get_rich_text(props.get("Color Palette Description", {}))
        style_keywords = _get_rich_text(props.get("Style Keywords", {}))
        refs_raw = _get_rich_text(props.get("Visual References", {}))

        print(f"  Loading blocks for {option}...")
        blocks = await notion.get_block_children(page_id)
        block_text = _blocks_to_text(blocks)

        # Parse data from blocks
        colors = _parse_colors(palette_desc, block_text)
        primary_font, secondary_font = _extract_fonts(block_text)
        headline = _extract_headline(block_text)
        scores = _extract_scores(block_text)
        concept_desc = _extract_section(block_text, concept_name) or _extract_section(block_text, "Option")
        pairing_rationale = _extract_section(block_text, "pairing_rationale") or _extract_section(block_text, "Pairing")
        target_fit = _extract_section(block_text, "Target Audience")
        references = [r.strip() for r in refs_raw.split("|") if r.strip()]
        strengths = [s.strip() for s in _extract_section(block_text, "Strength").split("\n") if s.strip()][:3]
        risks = [r.strip() for r in _extract_section(block_text, "Risk").split("\n") if r.strip()][:2]

        # Background for hero preview (light version of primary color or off-white)
        hero_bg = "#FAFAF8"

        variations.append({
            "option": option or f"Option {chr(65 + len(variations))}",
            "concept_name": concept_name or style_keywords.split(",")[0].strip(),
            "status": status,
            "colors": colors,
            "primary_font": primary_font,
            "secondary_font": secondary_font,
            "headline": headline,
            "concept_description": concept_desc or "A distinctive creative direction for WellWell.",
            "pairing_rationale": pairing_rationale or "Selected for readability and brand personality.",
            "scores": scores,
            "references": references,
            "strengths": strengths or ["Strong brand alignment", "Clear visual hierarchy"],
            "risks": risks or ["Review with client before finalizing"],
            "target_fit": target_fit or "Designed for professional women seeking premium telehealth care.",
            "hero_bg": hero_bg,
        })

    # Sort by option label
    variations.sort(key=lambda v: v["option"])

    print("Rendering HTML...")
    html = render_html(cfg["name"], variations)

    output_path = Path(__file__).parent.parent / cfg["output_file"]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html)
    print(f"\n✓ Mood board saved to: {output_path}")

    # Save JSON for Figma plugin
    from datetime import date
    recommended = next((v["option"] for v in variations if v["status"] in ("Approved", "Pending Review")), None)
    json_data = {
        "client": cfg["name"],
        "generated_at": date.today().isoformat(),
        "recommended": recommended,
        "variations": [
            {
                "option": v["option"],
                "concept_name": v["concept_name"],
                "status": v["status"],
                "colors": v["colors"],
                "primary_font": v["primary_font"],
                "secondary_font": v["secondary_font"],
                "headline": v["headline"],
                "scores": v["scores"],
                "strengths": v["strengths"],
                "risks": v["risks"],
            }
            for v in variations
        ],
    }
    json_path = output_path.with_name("mood_board_data.json")
    json_path.write_text(json.dumps(json_data, indent=2))
    print(f"✓ Figma JSON saved to: {json_path}")

    if open_browser:
        subprocess.run(["open", str(output_path)])
        print("  Opened in browser")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", default="wellwell")
    parser.add_argument("--open", action="store_true", help="Open in browser after generating")
    args = parser.parse_args()
    asyncio.run(main(args.client, args.open))
