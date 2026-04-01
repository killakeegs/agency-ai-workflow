#!/usr/bin/env python3
"""
generate_sitemap_visual.py — Generate a Relume-style visual sitemap from Notion data

Reads all Sitemap DB entries, builds a hierarchy from URL slugs, and renders
an interactive HTML diagram with boxes connected by lines, color-coded by
page type and content mode.

Usage:
    python scripts/generate_sitemap_visual.py --client wellwell
    python scripts/generate_sitemap_visual.py --client wellwell --open
"""
import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings
from src.integrations.notion import NotionClient

CLIENTS = {
    "wellwell": {
        "name": "WellWell",
        "sitemap_db_id": "d70fe7ab-a5f4-4814-9209-4bb6eb05b21a",
        "output_file": "output/wellwell/sitemap.html",
        "brand_color": "#2BA8A4",
    }
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


def _page_emoji(page: dict) -> str:
    slug = page.get("slug", "")
    title = page.get("title", "").lower()
    if slug in ("/", "") or "home" in title:        return "🏠"
    if page.get("page_type") == "CMS":              return "🗄️"
    if "blog" in title:                             return "📝"
    if "about" in title:                            return "👋"
    if "contact" in title:                          return "📞"
    if any(w in title for w in ("dermatology", "weight", "neuro", "botox", "service", "treatment", "therapy")):
        return "🩺"
    if any(w in title for w in ("location", "city", "state", "[city]")):
        return "📍"
    if any(w in title for w in ("faq", "question")):     return "❓"
    if "pricing" in title:                               return "💰"
    if any(w in title for w in ("privacy", "terms", "disclaimer", "consent", "legal")): return "⚖️"
    if "coming soon" in title:                           return "🚧"
    if "condition" in title:                             return "🔬"
    return "📄"


def _slug_depth(slug: str) -> int:
    """Return the nesting depth of a URL slug."""
    return len([s for s in slug.strip("/").split("/") if s])


def _slug_parent(slug: str) -> str:
    """Return the parent slug."""
    parts = [s for s in slug.strip("/").split("/") if s]
    if len(parts) <= 1:
        return "/"
    return "/" + "/".join(parts[:-1])


def build_tree(pages: list[dict]) -> dict:
    """Build a tree structure from flat list of pages."""
    # Sort by depth then order
    pages.sort(key=lambda p: (_slug_depth(p["slug"]), p["order"]))

    # Build lookup by slug
    by_slug: dict[str, dict] = {}
    for page in pages:
        slug = page["slug"]
        if slug not in by_slug:
            by_slug[slug] = {**page, "children": []}

    # Build tree
    roots = []
    for page in pages:
        slug = page["slug"]
        node = by_slug[slug]
        parent_slug = _slug_parent(slug)
        if parent_slug in by_slug and parent_slug != slug:
            by_slug[parent_slug]["children"].append(node)
        else:
            roots.append(node)

    # Sort children by order
    def sort_children(node: dict) -> None:
        node["children"].sort(key=lambda c: c["order"])
        for child in node["children"]:
            sort_children(child)

    for root in roots:
        sort_children(root)

    roots.sort(key=lambda r: r["order"])
    return {"title": "ROOT", "slug": "", "children": roots}


PAGE_COLORS = {
    ("Static", "AI Generated"):    {"bg": "#EBF7F6", "border": "#2BA8A4", "badge_bg": "#2BA8A4", "badge_text": "#fff"},
    ("Static", "Client Provided"):  {"bg": "#FEF9EE", "border": "#C9A96E", "badge_bg": "#C9A96E", "badge_text": "#fff"},
    ("CMS",    "AI Generated"):    {"bg": "#EEF2FB", "border": "#4A6FA5", "badge_bg": "#4A6FA5", "badge_text": "#fff"},
    ("CMS",    "Client Provided"):  {"bg": "#FFF0EC", "border": "#E07B54", "badge_bg": "#E07B54", "badge_text": "#fff"},
}
DEFAULT_COLOR = {"bg": "#F5F5F5", "border": "#ccc", "badge_bg": "#999", "badge_text": "#fff"}


def _node_id(slug: str) -> str:
    return "node_" + slug.strip("/").replace("/", "_").replace("-", "_").replace("[", "").replace("]", "") or "home"


def render_node_js(node: dict, parent_id: str | None = None) -> str:
    """Recursively build JavaScript node data for the diagram."""
    if node["slug"] == "":
        js = ""
        for child in node["children"]:
            js += render_node_js(child, None)
        return js

    nid = _node_id(node["slug"])
    page_type = node.get("page_type", "Static")
    content_mode = node.get("content_mode", "AI Generated")
    colors = PAGE_COLORS.get((page_type, content_mode), DEFAULT_COLOR)

    title = node["title"].replace("'", "\\'").replace('"', '\\"')
    slug = node["slug"].replace("'", "\\'")
    purpose = node.get("purpose", "")[:80].replace("'", "\\'").replace('"', '\\"').replace("\n", " ")

    children_ids = [f"'{_node_id(c['slug'])}'" for c in node["children"]]
    children_str = "[" + ", ".join(children_ids) + "]"

    js = f"""
  nodes['{nid}'] = {{
    id: '{nid}',
    title: '{title}',
    slug: '{slug}',
    pageType: '{page_type}',
    contentMode: '{content_mode}',
    purpose: '{purpose}',
    bg: '{colors["bg"]}',
    border: '{colors["border"]}',
    badgeBg: '{colors["badge_bg"]}',
    children: {children_str},
    parent: {f"'{parent_id}'" if parent_id else 'null'},
  }};"""

    for child in node["children"]:
        js += render_node_js(child, nid)

    return js


def render_html(client_name: str, brand_color: str, tree: dict) -> str:
    nodes_js = render_node_js(tree)

    # Count totals
    all_pages = []
    def collect(node):
        if node["slug"]:
            all_pages.append(node)
        for c in node["children"]:
            collect(c)
    collect(tree)

    static_count = sum(1 for p in all_pages if p.get("page_type") == "Static")
    cms_count = sum(1 for p in all_pages if p.get("page_type") == "CMS")
    ai_count = sum(1 for p in all_pages if p.get("content_mode") == "AI Generated")
    client_count = sum(1 for p in all_pages if p.get("content_mode") == "Client Provided")

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
  body {{
    font-family: 'Inter', sans-serif;
    background: #F7F7F5;
    color: #1C2B3A;
  }}
  .header {{
    background: white;
    border-bottom: 1px solid #eee;
    padding: 18px 32px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    position: sticky; top: 0; z-index: 100;
  }}
  .header-title {{
    font-family: 'Quicksand', sans-serif;
    font-size: 18px; font-weight: 700; color: #1C2B3A;
  }}
  .legend {{
    display: flex; gap: 16px; align-items: center; flex-wrap: wrap;
  }}
  .legend-item {{
    display: flex; align-items: center; gap: 6px;
    font-size: 12px; color: #555;
  }}
  .legend-dot {{
    width: 12px; height: 12px; border-radius: 3px;
  }}
  .stats {{
    display: flex; gap: 20px;
  }}
  .stat {{
    text-align: center;
  }}
  .stat-num {{
    font-size: 20px; font-weight: 700; color: #1C2B3A;
    font-family: 'Quicksand', sans-serif;
  }}
  .stat-label {{
    font-size: 10px; color: #999; text-transform: uppercase; letter-spacing: .5px;
  }}
  #canvas-container {{
    overflow: auto;
    padding: 40px;
    min-height: calc(100vh - 70px);
  }}
  #diagram {{
    position: relative;
    min-width: 1200px;
  }}
  svg.connectors {{
    position: absolute; top: 0; left: 0;
    pointer-events: none;
    overflow: visible;
  }}
  .node {{
    position: absolute;
    background: white;
    border-radius: 8px;
    border: 2px solid #ddd;
    padding: 10px 14px;
    min-width: 140px;
    max-width: 180px;
    cursor: pointer;
    transition: box-shadow .15s, transform .15s;
    user-select: none;
  }}
  .node:hover {{
    box-shadow: 0 4px 16px rgba(0,0,0,.12);
    transform: translateY(-1px);
    z-index: 10;
  }}
  .node-title {{
    font-size: 13px; font-weight: 600; color: #1C2B3A;
    margin-bottom: 4px; line-height: 1.3;
    font-family: 'Quicksand', sans-serif;
  }}
  .node-slug {{
    font-size: 10px; color: #999; font-family: monospace;
    margin-bottom: 6px;
  }}
  .badges {{
    display: flex; gap: 4px; flex-wrap: wrap;
  }}
  .badge {{
    font-size: 9px; font-weight: 700; padding: 2px 6px;
    border-radius: 20px; text-transform: uppercase; letter-spacing: .4px;
  }}
  .tooltip {{
    position: fixed;
    background: #1C2B3A;
    color: white;
    padding: 10px 14px;
    border-radius: 8px;
    font-size: 12px;
    pointer-events: none;
    opacity: 0;
    transition: opacity .15s;
    max-width: 260px;
    z-index: 1000;
    line-height: 1.5;
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
    <div class="legend-item">
      <div class="legend-dot" style="background:#EBF7F6;border:2px solid #2BA8A4"></div>
      Static / AI
    </div>
    <div class="legend-item">
      <div class="legend-dot" style="background:#FEF9EE;border:2px solid #C9A96E"></div>
      Static / Client
    </div>
    <div class="legend-item">
      <div class="legend-dot" style="background:#EEF2FB;border:2px solid #4A6FA5"></div>
      CMS / AI
    </div>
    <div class="legend-item">
      <div class="legend-dot" style="background:#FFF0EC;border:2px solid #E07B54"></div>
      CMS / Client
    </div>
  </div>
  <div class="stats">
    <div class="stat"><div class="stat-num">{len(all_pages)}</div><div class="stat-label">Total</div></div>
    <div class="stat"><div class="stat-num">{static_count}</div><div class="stat-label">Static</div></div>
    <div class="stat"><div class="stat-num">{cms_count}</div><div class="stat-label">CMS</div></div>
    <div class="stat"><div class="stat-num">{ai_count}</div><div class="stat-label">AI Copy</div></div>
    <div class="stat"><div class="stat-num">{client_count}</div><div class="stat-label">Client Copy</div></div>
  </div>
</div>

<div id="canvas-container">
  <div id="diagram">
    <svg class="connectors" id="svg-connectors"></svg>
  </div>
</div>
<div class="tooltip" id="tooltip"></div>

<script>
const nodes = {{}};
{nodes_js}

// ── Layout engine ─────────────────────────────────────────────────────────────
const NODE_W = 160, NODE_H = 72, H_GAP = 24, V_GAP = 80;

function getLevel(nodeId, visited = new Set()) {{
  if (visited.has(nodeId)) return 0;
  visited.add(nodeId);
  const node = nodes[nodeId];
  if (!node || !node.parent) return 0;
  return 1 + getLevel(node.parent, visited);
}}

// Group by level
const byLevel = {{}};
Object.values(nodes).forEach(n => {{
  const lvl = getLevel(n.id);
  n._level = lvl;
  if (!byLevel[lvl]) byLevel[lvl] = [];
  byLevel[lvl].push(n);
}});

// Sort each level: root-order children first
Object.keys(byLevel).forEach(lvl => {{
  byLevel[lvl].sort((a, b) => {{
    if (!a.parent && !b.parent) return 0;
    // Try to order by parent's position + sibling order
    return a.id.localeCompare(b.id);
  }});
}});

// Assign x positions level by level
const levelCount = Object.keys(byLevel).length;
const diagramEl = document.getElementById('diagram');
const svgEl = document.getElementById('svg-connectors');
const positions = {{}};

// First pass: assign horizontal positions based on subtree width
function subtreeWidth(nodeId, visited = new Set()) {{
  if (visited.has(nodeId)) return NODE_W + H_GAP;
  visited.add(nodeId);
  const node = nodes[nodeId];
  if (!node || node.children.length === 0) return NODE_W + H_GAP;
  return node.children.reduce((sum, cid) => sum + subtreeWidth(cid, new Set(visited)), 0);
}}

function assignPositions(nodeId, x, y, visited = new Set()) {{
  if (visited.has(nodeId)) return;
  visited.add(nodeId);
  const node = nodes[nodeId];
  if (!node) return;
  const sw = subtreeWidth(nodeId, new Set());
  positions[nodeId] = {{ x: x + (sw - NODE_W - H_GAP) / 2, y }};

  let childX = x;
  node.children.forEach(cid => {{
    const csw = subtreeWidth(cid, new Set());
    assignPositions(cid, childX, y + NODE_H + V_GAP, new Set(visited));
    childX += csw;
  }});
}}

// Find roots (level 0)
const roots = Object.values(nodes).filter(n => !n.parent);
let rootX = 0;
roots.forEach(r => {{
  assignPositions(r.id, rootX, 0, new Set());
  rootX += subtreeWidth(r.id, new Set());
}});

// Render nodes
let maxX = 0, maxY = 0;
Object.values(nodes).forEach(node => {{
  const pos = positions[node.id];
  if (!pos) return;
  maxX = Math.max(maxX, pos.x + NODE_W + 40);
  maxY = Math.max(maxY, pos.y + NODE_H + 40);

  const div = document.createElement('div');
  div.className = 'node';
  div.id = node.id;
  div.style.left = pos.x + 'px';
  div.style.top = pos.y + 'px';
  div.style.background = node.bg;
  div.style.borderColor = node.border;
  div.style.width = NODE_W + 'px';

  const typeAbbr = node.pageType === 'CMS' ? 'CMS' : 'Static';
  const modeAbbr = node.contentMode === 'AI Generated' ? 'AI' : 'Client';

  div.innerHTML = `
    <div class="node-title">${{node.title}}</div>
    <div class="node-slug">${{node.slug}}</div>
    <div class="badges">
      <span class="badge" style="background:${{node.badgeBg}};color:white">${{typeAbbr}}</span>
      <span class="badge" style="background:#eee;color:#666">${{modeAbbr}}</span>
    </div>`;

  div.addEventListener('mouseenter', (e) => showTooltip(e, node));
  div.addEventListener('mouseleave', hideTooltip);
  div.addEventListener('mousemove', moveTooltip);

  diagramEl.appendChild(div);
}});

// Set diagram size
diagramEl.style.width = maxX + 'px';
diagramEl.style.height = maxY + 'px';
svgEl.setAttribute('width', maxX);
svgEl.setAttribute('height', maxY);

// Draw connector lines
Object.values(nodes).forEach(node => {{
  if (!node.parent) return;
  const parentPos = positions[node.parent];
  const childPos = positions[node.id];
  if (!parentPos || !childPos) return;

  const x1 = parentPos.x + NODE_W / 2;
  const y1 = parentPos.y + NODE_H;
  const x2 = childPos.x + NODE_W / 2;
  const y2 = childPos.y;
  const cy = (y1 + y2) / 2;

  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', `M ${{x1}} ${{y1}} C ${{x1}} ${{cy}}, ${{x2}} ${{cy}}, ${{x2}} ${{y2}}`);
  path.setAttribute('stroke', nodes[node.parent]?.border || '#ccc');
  path.setAttribute('stroke-width', '1.5');
  path.setAttribute('fill', 'none');
  path.setAttribute('opacity', '0.6');
  svgEl.appendChild(path);
}});

// Tooltip
const tooltip = document.getElementById('tooltip');
function showTooltip(e, node) {{
  tooltip.innerHTML = `<strong>${{node.title}}</strong><br>
    ${{node.slug}}<br>
    <span style="color:#aaa;font-size:11px">${{node.purpose || ''}}</span>`;
  tooltip.style.opacity = '1';
  moveTooltip(e);
}}
function moveTooltip(e) {{
  tooltip.style.left = (e.clientX + 14) + 'px';
  tooltip.style.top = (e.clientY - 10) + 'px';
}}
function hideTooltip() {{
  tooltip.style.opacity = '0';
}}
</script>
</body>
</html>"""


async def write_notion_visual(
    cfg: dict,
    notion: NotionClient,
    tree: dict,
    all_pages: list[dict],
) -> str:
    """
    Write the sitemap hierarchy to a Notion page for client review.
    Creates the page under the sitemap database's parent (the client root page).
    Returns the Notion page URL.
    """
    from src.config import settings

    # Find the client root page by reading the sitemap DB's parent
    db_info = await notion._client.request(
        path=f"databases/{cfg['sitemap_db_id']}",
        method="GET",
    )
    parent_page_id = db_info["parent"]["page_id"]

    # Create the review page
    page_id = await notion.create_page(
        parent_page_id=parent_page_id,
        title=f"{cfg['name']} — Sitemap Review",
    )

    # ── Build blocks ──────────────────────────────────────────────────────────
    static_count  = sum(1 for p in all_pages if p.get("page_type") == "Static")
    cms_count     = sum(1 for p in all_pages if p.get("page_type") == "CMS")
    ai_count      = sum(1 for p in all_pages if p.get("content_mode") == "AI Generated")
    client_count  = sum(1 for p in all_pages if p.get("content_mode") == "Client Provided")

    blocks: list[dict] = []

    # Stats callout
    blocks.append({
        "object": "block", "type": "callout",
        "callout": {
            "rich_text": [{"type": "text", "text": {"content":
                f"{len(all_pages)} pages total  ·  {static_count} static  ·  "
                f"{cms_count} CMS  ·  {ai_count} AI-written  ·  {client_count} client-provided"
            }}],
            "icon": {"type": "emoji", "emoji": "📊"},
            "color": "blue_background",
        }
    })

    # Instructions callout
    blocks.append({
        "object": "block", "type": "callout",
        "callout": {
            "rich_text": [{"type": "text", "text": {"content":
                "Review this structure and request any changes. "
                "Once approved, update Stage Status → Approved in Client Info."
            }}],
            "icon": {"type": "emoji", "emoji": "✅"},
            "color": "green_background",
        }
    })

    blocks.append({"object": "block", "type": "divider", "divider": {}})

    # Legend
    blocks.append({
        "object": "block", "type": "paragraph",
        "paragraph": {
            "rich_text": [
                {"type": "text", "text": {"content": "🗄️ = CMS collection   "},
                 "annotations": {"color": "blue"}},
                {"type": "text", "text": {"content": "[Client] = client-provided copy   "},
                 "annotations": {"color": "brown"}},
                {"type": "text", "text": {"content": "└── = sub-page"},
                 "annotations": {"color": "gray"}},
            ]
        }
    })

    blocks.append({"object": "block", "type": "divider", "divider": {}})

    # ── Hierarchy ─────────────────────────────────────────────────────────────
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
            # Top-level page — bold title + code slug
            title_rt = [
                {"type": "text",
                 "text": {"content": f"{emoji}  {node['title']}{cms_tag}{cli_tag}"},
                 "annotations": {"bold": True, "color": "blue" if is_cms else "default"}},
                {"type": "text",
                 "text": {"content": f"   {node['slug']}"},
                 "annotations": {"code": True, "color": "gray"}},
            ]
            blocks.append({
                "object": "block", "type": "paragraph",
                "paragraph": {"rich_text": title_rt}
            })
        else:
            indent    = "      " * depth
            connector = "└── "
            line      = f"{indent}{connector}{node['title']}{cms_tag}{cli_tag}"
            slug_part = f"   {node['slug']}"
            blocks.append({
                "object": "block", "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {"type": "text", "text": {"content": line},
                         "annotations": {"color": "blue" if is_cms else ("gray" if depth > 1 else "default")}},
                        {"type": "text", "text": {"content": slug_part},
                         "annotations": {"code": True, "color": "gray"}},
                    ]
                }
            })

        for child in node["children"]:
            render_node(child, depth + 1)

        # Blank line after each top-level section
        if depth == 0:
            blocks.append({
                "object": "block", "type": "paragraph",
                "paragraph": {"rich_text": []}
            })

    render_node(tree)

    # Append in batches of 100 (Notion API limit per call)
    for i in range(0, len(blocks), 100):
        await notion.append_blocks(page_id, blocks[i:i + 100])

    return f"https://notion.so/{page_id.replace('-', '')}"


async def main(client_key: str, open_browser: bool, write_notion: bool = False) -> None:
    cfg = CLIENTS[client_key]
    notion = NotionClient(settings.notion_api_key)

    print(f"Fetching sitemap entries for {cfg['name']}...")
    entries = await notion.query_database(cfg["sitemap_db_id"])
    print(f"Found {len(entries)} pages")

    pages = []
    for entry in entries:
        props = entry["properties"]
        pages.append({
            "title": _get_title(props.get("Name", {})) or "Untitled",
            "slug": _get_rich_text(props.get("Slug", {})) or "/unknown",
            "page_type": _get_select(props.get("Page Type", {})) or "Static",
            "content_mode": _get_select(props.get("Content Mode", {})) or "AI Generated",
            "order": _get_number(props.get("Order", {})),
            "purpose": _get_rich_text(props.get("Purpose", {}))[:120],
        })

    tree = build_tree(pages)

    print("Rendering visual sitemap...")
    html = render_html(cfg["name"], cfg["brand_color"], tree)

    output_path = Path(__file__).parent.parent / cfg["output_file"]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html)
    print(f"\n✓ Sitemap saved to: {output_path}")

    # Also save JSON for Figma plugin
    from datetime import date
    json_path = output_path.with_name("sitemap_data.json")
    json_data = {
        "client": cfg["name"],
        "generated_at": date.today().isoformat(),
        "pages": pages,
    }
    json_path.write_text(json.dumps(json_data, indent=2))
    print(f"✓ Figma JSON saved to: {json_path}")

    if open_browser:
        subprocess.run(["open", str(output_path)])
        print("  Opened in browser")

    if write_notion:
        print("\nWriting sitemap review page to Notion...")
        notion_url = await write_notion_visual(cfg, notion, tree, pages)
        print(f"✓ Notion review page: {notion_url}")
        print(f"  Share this link with the client for sitemap approval.")
        if not open_browser:
            subprocess.run(["open", notion_url])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", default="wellwell")
    parser.add_argument("--open", action="store_true", help="Open HTML in browser")
    parser.add_argument("--notion", action="store_true", help="Write hierarchy to a Notion review page")
    args = parser.parse_args()
    asyncio.run(main(args.client, args.open, args.notion))
