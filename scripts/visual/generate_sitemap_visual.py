#!/usr/bin/env python3
"""
generate_sitemap_visual.py — Generate a grouped visual sitemap from Notion data.

Reads all Sitemap DB entries and renders a clean, column-based HTML diagram
grouped by navigation location (Main Nav vs Footer), with service subcategories
nested under their parent service hub.

Usage:
    python scripts/generate_sitemap_visual.py --client summit_therapy
    python scripts/generate_sitemap_visual.py --client summit_therapy --open
    python scripts/generate_sitemap_visual.py --client summit_therapy --notion
"""
import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.clients import CLIENTS as _ALL_CLIENTS
from src.config import settings
from src.integrations.notion import NotionClient


def _build_client_config(client_key: str) -> dict:
    """Build visual generator config from the central client registry."""
    cfg = _ALL_CLIENTS[client_key]
    return {
        "name": cfg["name"],
        "sitemap_db_id": cfg["sitemap_db_id"],
        "client_info_db_id": cfg.get("client_info_db_id", ""),
        "output_file": f"output/{client_key}/sitemap.html",
        "brand_color": "#2563EB",
    }


def _get_rich_text(prop: dict) -> str:
    return "".join(p.get("text", {}).get("content", "") for p in prop.get("rich_text", []))


def _get_select(prop: dict) -> str:
    sel = prop.get("select")
    return sel.get("name", "") if sel else ""


def _get_title(prop: dict) -> str:
    return "".join(p.get("text", {}).get("content", "") for p in prop.get("title", []))


def _get_number(prop: dict) -> int:
    return prop.get("number") or 99


PAGE_COLORS = {
    ("Static", "AI Generated"):    {"bg": "#EBF7F6", "border": "#2BA8A4", "badge_bg": "#2BA8A4"},
    ("Static", "Client Provided"): {"bg": "#FEF9EE", "border": "#C9A96E", "badge_bg": "#C9A96E"},
    ("CMS",    "AI Generated"):    {"bg": "#EEF2FB", "border": "#4A6FA5", "badge_bg": "#4A6FA5"},
    ("CMS",    "Client Provided"): {"bg": "#FFF0EC", "border": "#E07B54", "badge_bg": "#E07B54"},
}
DEFAULT_COLOR = {"bg": "#F5F5F5", "border": "#ccc", "badge_bg": "#999"}


def render_html(client_name: str, brand_color: str, pages: list[dict]) -> str:
    """
    Render a vertical nested sitemap tree.

    STANDARD: Sitemap visuals always use vertical nesting — each child page
    sits directly below and indented from its parent, with connector lines
    showing the hierarchy. Never render sitemaps as horizontal columns.
    This makes parent-child relationships immediately legible for client review.
    """

    # ── Stats ──────────────────────────────────────────────────────────────────
    static_count = sum(1 for p in pages if p.get("page_type") == "Static")
    cms_count    = sum(1 for p in pages if p.get("page_type") == "CMS")
    ai_count     = sum(1 for p in pages if p.get("content_mode") == "AI Generated")
    cli_count    = sum(1 for p in pages if p.get("content_mode") == "Client Provided")

    # ── Group pages by nav structure ───────────────────────────────────────────
    def s(lst): return sorted(lst, key=lambda p: p["order"])

    home = next((p for p in pages if p["slug"] == "/"), None)

    st_hub  = next((p for p in pages if p["slug"] == "/services/speech-therapy"), None)
    ot_hub  = next((p for p in pages if p["slug"] == "/services/occupational-therapy"), None)
    pt_hub  = next((p for p in pages if p["slug"] == "/services/physical-therapy"), None)
    st_subs = s([p for p in pages if p["slug"].startswith("/services/speech-therapy/")])
    ot_subs = s([p for p in pages if p["slug"].startswith("/services/occupational-therapy/")])
    pt_subs = s([p for p in pages if p["slug"].startswith("/services/physical-therapy/")])

    wws       = s([p for p in pages if p["slug"].startswith("/who-we-serve/")])
    about     = s([p for p in pages if p["slug"] in ("/about", "/about/team", "/about/careers")])
    resources = s([p for p in pages if p["slug"] in ("/new-patients", "/insurance")])
    blog      = s([p for p in pages if p.get("section") == "Blog"])
    contact   = [p for p in pages if p["slug"] == "/contact"]

    locations    = s([p for p in pages if p.get("section") == "Locations"])
    testimonials = [p for p in pages if p["slug"] == "/testimonials"]
    legal        = s([p for p in pages if p.get("section") == "Legal"])

    # ── Render helpers ─────────────────────────────────────────────────────────
    def _card(p: dict, sub: bool = False) -> str:
        pt      = p.get("page_type", "Static")
        cm      = p.get("content_mode", "AI Generated")
        colors  = PAGE_COLORS.get((pt, cm), DEFAULT_COLOR)
        t_lbl   = "CMS" if pt == "CMS" else "STATIC"
        m_lbl   = "AI" if cm == "AI Generated" else "CLIENT"
        cls     = " sub" if sub else ""
        title   = p.get("title", "Untitled").replace("<", "&lt;").replace(">", "&gt;")
        slug    = p.get("slug", "/")
        purpose = p.get("purpose", "")[:150].replace('"', "&quot;").replace("\n", " ")
        # Sections shown inline
        key_secs = p.get("key_sections", "")
        secs_html = ""
        if key_secs:
            items = [s.lstrip("•").strip() for s in key_secs.split("\n") if s.strip()]
            secs_html = '<ul class="card-sections">' + "".join(f"<li>{s}</li>" for s in items) + "</ul>"
        return (
            f'<div class="page-card{cls}" '
            f'style="background:{colors["bg"]};border-color:{colors["border"]}" '
            f'data-title="{title}" data-slug="{slug}" data-purpose="{purpose}">'
            f'<div class="card-title">{title}</div>'
            f'<div class="card-slug">{slug}</div>'
            f'<div class="card-badges">'
            f'<span class="badge" style="background:{colors["badge_bg"]}">{t_lbl}</span>'
            f'<span class="badge badge-mode">{m_lbl}</span>'
            f'</div>'
            f'{secs_html}'
            f'</div>'
        )

    def _col(label: str, content: str) -> str:
        """Render a nav column — skip entirely if empty."""
        if not content.strip():
            return ""
        return (
            f'<div class="nav-col">'
            f'<div class="col-label">{label}</div>'
            f'{content}'
            f'</div>\n'
        )

    def _cards(pages_list: list, sub: bool = False) -> str:
        return "".join(_card(p, sub=sub) for p in pages_list)

    # ── Build column HTML ──────────────────────────────────────────────────────
    # Services column: hub card + subcategories indented below each hub
    svc_content = ""
    for hub, subs in [(st_hub, st_subs), (ot_hub, ot_subs), (pt_hub, pt_subs)]:
        if hub:
            svc_content += _card(hub)
            svc_content += _cards(subs, sub=True)

    main_nav_html = (
        _col("Services", svc_content) +
        _col("Who We Serve", _cards(wws)) +
        _col("About", _cards(about)) +
        _col("Resources", _cards(resources)) +
        _col("Blog", _cards(blog)) +
        _col("Contact", _cards(contact))
    )

    footer_html = (
        _col("Locations", _cards(locations)) +
        _col("Testimonials", _cards(testimonials)) +
        _col("Legal", _cards(legal))
    )

    home_html = _card(home) if home else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{client_name} — Sitemap</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Quicksand:wght@400;600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Inter', sans-serif; background: #F7F7F5; color: #1C2B3A; }}

  /* Header */
  .header {{
    background: white; border-bottom: 1px solid #eee;
    padding: 16px 32px; display: flex; align-items: center;
    justify-content: space-between; position: sticky; top: 0; z-index: 100;
    flex-wrap: wrap; gap: 12px;
  }}
  .header-title {{ font-family: 'Quicksand', sans-serif; font-size: 18px; font-weight: 700; }}
  .legend {{ display: flex; gap: 14px; align-items: center; flex-wrap: wrap; }}
  .legend-item {{ display: flex; align-items: center; gap: 5px; font-size: 11px; color: #666; }}
  .legend-dot {{ width: 11px; height: 11px; border-radius: 3px; flex-shrink: 0; }}
  .stats {{ display: flex; gap: 20px; }}
  .stat {{ text-align: center; }}
  .stat-num {{ font-size: 20px; font-weight: 700; font-family: 'Quicksand', sans-serif; }}
  .stat-label {{ font-size: 10px; color: #999; text-transform: uppercase; letter-spacing: .5px; }}

  /* Layout */
  .sitemap-container {{ padding: 36px 48px 60px; }}

  /* Home row */
  .home-row {{ display: flex; justify-content: center; margin-bottom: 6px; }}
  .home-row .page-card {{ min-width: 160px; max-width: 200px; }}

  /* Connector arrow */
  .v-connector {{ text-align: center; font-size: 18px; color: #ccc; line-height: 1; margin-bottom: 10px; }}

  /* Section labels */
  .section-label {{
    font-size: 10px; font-weight: 700; text-transform: uppercase;
    letter-spacing: 1px; color: #aaa;
    border-bottom: 1px solid #e5e5e5; padding-bottom: 8px; margin-bottom: 16px;
  }}

  /* Column row */
  .nav-row {{
    display: flex; gap: 14px; align-items: flex-start;
    overflow-x: auto; padding-bottom: 12px;
  }}

  /* Column */
  .nav-col {{ flex: 0 0 auto; min-width: 160px; max-width: 195px; }}

  /* Column header label */
  .col-label {{
    font-size: 9px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .8px; color: #777;
    background: #ebebeb; padding: 3px 8px;
    border-radius: 4px; margin-bottom: 8px; text-align: center;
  }}

  /* Page cards */
  .page-card {{
    border-radius: 8px; border: 2px solid #ddd;
    padding: 8px 10px; margin-bottom: 4px;
    cursor: default; transition: box-shadow .15s, transform .1s;
  }}
  .page-card:hover {{ box-shadow: 0 3px 10px rgba(0,0,0,.1); transform: translateY(-1px); }}

  /* Subcategory cards — indented below their parent hub */
  .page-card.sub {{
    margin-left: 12px; padding: 5px 8px;
    border-left-width: 3px;
  }}

  .card-title {{
    font-size: 12px; font-weight: 600; line-height: 1.3;
    margin-bottom: 2px; font-family: 'Quicksand', sans-serif;
  }}
  .page-card.sub .card-title {{ font-size: 11px; }}
  .card-slug {{
    font-size: 9px; color: #aaa; font-family: monospace;
    margin-bottom: 4px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }}
  .card-badges {{ display: flex; gap: 3px; flex-wrap: wrap; }}
  .badge {{
    font-size: 8px; font-weight: 700; padding: 1px 5px;
    border-radius: 20px; text-transform: uppercase; letter-spacing: .3px; color: white;
  }}
  .badge-mode {{ background: #e8e8e8 !important; color: #777 !important; }}

  /* Inline sections list */
  .card-sections {{
    list-style: none; margin-top: 6px;
    border-top: 1px solid rgba(0,0,0,.06); padding-top: 5px;
  }}
  .card-sections li {{
    font-size: 9px; color: #666; line-height: 1.6;
    padding-left: 8px; position: relative;
  }}
  .card-sections li::before {{
    content: '–'; position: absolute; left: 0; color: #bbb;
  }}

  /* Footer section */
  .footer-section {{ margin-top: 48px; padding-top: 28px; border-top: 2px dashed #ddd; }}
  .footer-section .section-label {{ color: #bbb; border-color: #efefef; }}

  /* Tooltip */
  .tooltip {{
    position: fixed; background: #1C2B3A; color: white;
    padding: 10px 14px; border-radius: 8px; font-size: 12px;
    pointer-events: none; opacity: 0; transition: opacity .15s;
    max-width: 320px; z-index: 1000; line-height: 1.6;
  }}
</style>
</head>
<body>

<div class="header">
  <div>
    <div class="header-title">{client_name} — Visual Sitemap</div>
    <div style="font-size:11px;color:#999;margin-top:2px">Hover pages for details · Generated by RxMedia AI Pipeline</div>
  </div>
  <div class="legend">
    <div class="legend-item"><div class="legend-dot" style="background:#EBF7F6;border:2px solid #2BA8A4"></div>Static / AI</div>
    <div class="legend-item"><div class="legend-dot" style="background:#FEF9EE;border:2px solid #C9A96E"></div>Static / Client</div>
    <div class="legend-item"><div class="legend-dot" style="background:#EEF2FB;border:2px solid #4A6FA5"></div>CMS / AI</div>
    <div class="legend-item"><div class="legend-dot" style="background:#FFF0EC;border:2px solid #E07B54"></div>CMS / Client</div>
  </div>
  <div class="stats">
    <div class="stat"><div class="stat-num">{len(pages)}</div><div class="stat-label">Total</div></div>
    <div class="stat"><div class="stat-num">{static_count}</div><div class="stat-label">Static</div></div>
    <div class="stat"><div class="stat-num">{cms_count}</div><div class="stat-label">CMS</div></div>
    <div class="stat"><div class="stat-num">{ai_count}</div><div class="stat-label">AI Copy</div></div>
    <div class="stat"><div class="stat-num">{cli_count}</div><div class="stat-label">Client Copy</div></div>
  </div>
</div>

<div class="sitemap-container">

  <!-- Home -->
  <div class="home-row">{home_html}</div>
  <div class="v-connector">↓</div>

  <!-- Main Navigation columns -->
  <div class="section-label">Main Navigation</div>
  <div class="nav-row">
    {main_nav_html}
  </div>

  <!-- Footer columns -->
  <div class="footer-section">
    <div class="section-label">Footer</div>
    <div class="nav-row">
      {footer_html}
    </div>
  </div>

</div>

<div class="tooltip" id="tooltip"></div>

<script>
const tooltip = document.getElementById('tooltip');
document.querySelectorAll('.page-card').forEach(card => {{
  card.addEventListener('mouseenter', () => {{
    const title   = card.dataset.title || '';
    const slug    = card.dataset.slug  || '';
    const purpose = card.dataset.purpose || '';
    const secs    = card.dataset.sections || '';
    let html = '<strong>' + title + '</strong><br>' +
               '<span style="color:#aaa;font-size:10px;font-family:monospace">' + slug + '</span>';
    if (purpose) html += '<br><span style="color:#bbb;font-size:11px;display:block;margin-top:4px">' + purpose + '</span>';
    if (secs)    html += '<br><span style="color:#7cb8f0;font-size:11px;display:block;margin-top:6px;white-space:pre-line">' + secs + '</span>';
    tooltip.innerHTML = html;
    tooltip.style.opacity = '1';
  }});
  card.addEventListener('mousemove', e => {{
    tooltip.style.left = (e.clientX + 14) + 'px';
    tooltip.style.top  = (e.clientY - 10) + 'px';
  }});
  card.addEventListener('mouseleave', () => {{ tooltip.style.opacity = '0'; }});
}});
</script>
</body>
</html>"""


# ── Write sitemap URL to Client Info ──────────────────────────────────────────

async def write_sitemap_url_to_client_info(
    cfg: dict,
    notion: NotionClient,
    url: str,
) -> None:
    """
    Add a 'Sitemap Visual URL' field to the Client Info DB and write the URL
    to the client's entry so the team can click directly from Notion.
    """
    db_id = cfg.get("client_info_db_id", "")
    if not db_id:
        return

    # Ensure the URL field exists on the DB (idempotent)
    await notion._client.request(
        path=f"databases/{db_id}",
        method="PATCH",
        body={"properties": {"Sitemap Visual URL": {"url": {}}}},
    )

    # Find the client's entry and write the URL
    entries = await notion.query_database(db_id)
    if not entries:
        return

    entry_id = entries[0]["id"]
    await notion._client.request(
        path=f"pages/{entry_id}",
        method="PATCH",
        body={"properties": {"Sitemap Visual URL": {"url": url}}},
    )


# ── Notion review page writer (unchanged) ──────────────────────────────────────

def _slug_parent(slug: str) -> str:
    parts = [s for s in slug.strip("/").split("/") if s]
    if len(parts) <= 1:
        return "/"
    return "/" + "/".join(parts[:-1])


def _page_emoji(page: dict) -> str:
    slug = page.get("slug", "")
    title = page.get("title", "").lower()
    if slug in ("/", "") or "home" in title:    return "🏠"
    if page.get("page_type") == "CMS":          return "🗄️"
    if "blog" in title:                         return "📝"
    if "about" in title:                        return "👋"
    if "contact" in title:                      return "📞"
    if any(w in title for w in ("therapy", "service", "treatment")): return "🩺"
    if any(w in title for w in ("location", "clinic", "frisco", "mckinney")): return "📍"
    if any(w in title for w in ("privacy", "terms", "legal", "accessib")): return "⚖️"
    return "📄"


def build_tree(pages: list[dict]) -> dict:
    pages.sort(key=lambda p: (len([s for s in p["slug"].strip("/").split("/") if s]), p["order"]))
    by_slug: dict[str, dict] = {}
    for page in pages:
        slug = page["slug"]
        if slug not in by_slug:
            by_slug[slug] = {**page, "children": []}
    roots = []
    for page in pages:
        slug = page["slug"]
        node = by_slug[slug]
        parent_slug = _slug_parent(slug)
        if parent_slug in by_slug and parent_slug != slug:
            by_slug[parent_slug]["children"].append(node)
        else:
            roots.append(node)

    def sort_children(node):
        node["children"].sort(key=lambda c: c["order"])
        for child in node["children"]:
            sort_children(child)
    for root in roots:
        sort_children(root)
    roots.sort(key=lambda r: r["order"])
    return {"title": "ROOT", "slug": "", "children": roots}


async def write_notion_visual(cfg, notion, tree, all_pages) -> str:
    db_info = await notion._client.request(path=f"databases/{cfg['sitemap_db_id']}", method="GET")
    parent_page_id = db_info["parent"]["page_id"]
    page_id = await notion.create_page(parent_page_id=parent_page_id, title=f"{cfg['name']} — Sitemap Review")

    static_count = sum(1 for p in all_pages if p.get("page_type") == "Static")
    cms_count    = sum(1 for p in all_pages if p.get("page_type") == "CMS")
    ai_count     = sum(1 for p in all_pages if p.get("content_mode") == "AI Generated")
    client_count = sum(1 for p in all_pages if p.get("content_mode") == "Client Provided")

    blocks: list[dict] = [
        {"object": "block", "type": "callout", "callout": {
            "rich_text": [{"type": "text", "text": {"content":
                f"{len(all_pages)} pages total  ·  {static_count} static  ·  "
                f"{cms_count} CMS  ·  {ai_count} AI-written  ·  {client_count} client-provided"
            }}],
            "icon": {"type": "emoji", "emoji": "📊"}, "color": "blue_background",
        }},
        {"object": "block", "type": "callout", "callout": {
            "rich_text": [{"type": "text", "text": {"content":
                "Review this structure and request any changes. "
                "Once approved, update Stage Status → Approved in Client Info."
            }}],
            "icon": {"type": "emoji", "emoji": "✅"}, "color": "green_background",
        }},
        {"object": "block", "type": "divider", "divider": {}},
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [
            {"type": "text", "text": {"content": "🗄️ = CMS collection   "},
             "annotations": {"color": "blue"}},
            {"type": "text", "text": {"content": "[Client] = client-provided copy   "},
             "annotations": {"color": "brown"}},
            {"type": "text", "text": {"content": "└── = sub-page"},
             "annotations": {"color": "gray"}},
        ]}},
        {"object": "block", "type": "divider", "divider": {}},
    ]

    def render_node(node: dict, depth: int = 0) -> None:
        if node["slug"] == "":
            for child in node["children"]:
                render_node(child, 0)
            return
        is_cms    = node.get("page_type") == "CMS"
        is_client = node.get("content_mode") == "Client Provided"
        emoji     = _page_emoji(node)
        cms_tag   = "  [CMS]"    if is_cms    else ""
        cli_tag   = "  [Client]" if is_client else ""
        if depth == 0:
            blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": [
                {"type": "text",
                 "text": {"content": f"{emoji}  {node['title']}{cms_tag}{cli_tag}"},
                 "annotations": {"bold": True, "color": "blue" if is_cms else "default"}},
                {"type": "text",
                 "text": {"content": f"   {node['slug']}"},
                 "annotations": {"code": True, "color": "gray"}},
            ]}})
        else:
            indent = "      " * depth
            line   = f"{indent}└── {node['title']}{cms_tag}{cli_tag}"
            blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": [
                {"type": "text", "text": {"content": line},
                 "annotations": {"color": "blue" if is_cms else ("gray" if depth > 1 else "default")}},
                {"type": "text", "text": {"content": f"   {node['slug']}"},
                 "annotations": {"code": True, "color": "gray"}},
            ]}})
        for child in node["children"]:
            render_node(child, depth + 1)
        if depth == 0:
            blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": []}})

    render_node(tree)
    for i in range(0, len(blocks), 100):
        await notion.append_blocks(page_id, blocks[i:i + 100])
    return f"https://notion.so/{page_id.replace('-', '')}"


async def main(client_key: str, open_browser: bool, write_notion: bool = False) -> None:
    cfg = _build_client_config(client_key)
    notion = NotionClient(settings.notion_api_key)

    print(f"Fetching sitemap entries for {cfg['name']}...")
    entries = await notion.query_database(cfg["sitemap_db_id"])
    print(f"Found {len(entries)} pages")

    pages = []
    for entry in entries:
        props = entry["properties"]
        # Title property may be named "Name" (Notion default) or "Page Title"
        _title_key = next((k for k, v in props.items() if v.get("type") == "title"), "Name")
        pages.append({
            "title":        _get_title(props.get(_title_key, {})) or "Untitled",
            "slug":         _get_rich_text(props.get("Slug", {})) or "/unknown",
            "page_type":    _get_select(props.get("Page Type", {})) or "Static",
            "content_mode": _get_select(props.get("Content Mode", {})) or "AI Generated",
            "section":      _get_select(props.get("Section", {})) or "Core",
            "nav_location": _get_select(props.get("Nav Location", {})) or "Main Nav",
            "order":        _get_number(props.get("Order", {})),
            "purpose":      _get_rich_text(props.get("Purpose", {}))[:150],
            "key_sections": _get_rich_text(props.get("Key Sections", {})),
        })

    print("Rendering visual sitemap...")
    html = render_html(cfg["name"], cfg["brand_color"], pages)

    output_path = Path(__file__).parent.parent / cfg["output_file"]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html)
    print(f"\n✓ Sitemap saved to: {output_path}")

    # Write clickable URL to Client Info in Notion
    file_url = output_path.resolve().as_uri()
    if cfg.get("client_info_db_id"):
        try:
            await write_sitemap_url_to_client_info(cfg, notion, file_url)
            print(f"✓ Sitemap URL written to Notion Client Info")
        except Exception as e:
            print(f"  ⚠ Could not write URL to Notion: {e}")

    json_path = output_path.with_name("sitemap_data.json")
    from datetime import date
    json_path.write_text(json.dumps({
        "client": cfg["name"],
        "generated_at": date.today().isoformat(),
        "pages": pages,
    }, indent=2))
    print(f"✓ Figma JSON saved to: {json_path}")

    if open_browser:
        subprocess.run(["open", str(output_path)])
        print("  Opened in browser")

    if write_notion:
        print("\nWriting sitemap review page to Notion...")
        tree = build_tree(pages)
        notion_url = await write_notion_visual(cfg, notion, tree, pages)
        print(f"✓ Notion review page: {notion_url}")
        if not open_browser:
            subprocess.run(["open", notion_url])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", default="summit_therapy")
    parser.add_argument("--open", action="store_true", help="Open HTML in browser")
    parser.add_argument("--notion", action="store_true", help="Write hierarchy to a Notion review page")
    args = parser.parse_args()
    asyncio.run(main(args.client, args.open, args.notion))
