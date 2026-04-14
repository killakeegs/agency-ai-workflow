#!/usr/bin/env python3
"""
export_relume_sitemap.py — Export a clean Pre-Relume sitemap from the Notion Sitemap DB.

Outputs a text file with three columns per page:
  Page Title | Parent Page | Key Sections

Sorted by Parent Page so the hierarchy reads top-down (Home first, then main
nav pages, then children). Paste this into Relume's sitemap builder or AI prompt.

Usage:
    python scripts/export_relume_sitemap.py --client summit_therapy
    python scripts/export_relume_sitemap.py --client summit_therapy --open
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.clients import CLIENTS
from src.config import settings
from src.integrations.notion import NotionClient


def _get_rich_text(prop: dict) -> str:
    return "".join(p.get("text", {}).get("content", "") for p in prop.get("rich_text", []))


def _get_title(prop: dict) -> str:
    return "".join(p.get("text", {}).get("content", "") for p in prop.get("title", []))


def _get_select(prop: dict) -> str:
    sel = prop.get("select")
    return sel.get("name", "") if sel else ""


def _clean_sections(raw: str) -> str:
    """Strip bullet chars and return comma-separated section names."""
    lines = [l.strip().lstrip("•–- ").strip() for l in raw.split("\n") if l.strip()]
    return ", ".join(lines)


def _sort_key(page: dict) -> tuple:
    """Sort: Home first, then by parent, then by order."""
    parent = page["parent"] or ""
    order = page["order"]
    if not parent:
        return (0, "", order)
    if parent == "Home":
        return (1, "", order)
    return (2, parent, order)


async def main(client_key: str, open_output: bool, all_cms: bool = False) -> None:
    cfg = CLIENTS[client_key]
    notion = NotionClient(settings.notion_api_key)
    client_name = cfg.get("name", client_key)

    print(f"Building Relume sitemap export for {client_name}...")

    entries = await notion.query_database(
        cfg["sitemap_db_id"],
        sorts=[{"property": "Order", "direction": "ascending"}],
    )
    print(f"  Found {len(entries)} pages")

    pages = []
    for entry in entries:
        pp = entry["properties"]
        title = _get_title(pp.get("Page Title", {})) or _get_title(pp.get("Name", {})) or "Untitled"
        slug = _get_rich_text(pp.get("Slug", {}))
        parent = _get_rich_text(pp.get("Parent Page", {}))
        key_sections = _get_rich_text(pp.get("Key Sections", {}))
        order = pp.get("Order", {}).get("number", 99) or 99
        page_type = _get_select(pp.get("Page Type", {}))

        sections = _clean_sections(key_sections)
        pages.append({
            "title": title,
            "slug": slug,
            "parent": parent,
            "sections": sections,
            "order": order,
            "page_type": page_type,
        })

    pages.sort(key=_sort_key)

    # ── Build compact indented tree (Relume AI target: under 5,000 chars) ────
    # Group pages by parent
    from collections import defaultdict
    children: dict[str, list] = defaultdict(list)
    roots: list = []
    for pg in pages:
        if not pg["parent"]:
            roots.append(pg)
        else:
            children[pg["parent"]].append(pg)

    def render_tree(pg: dict, depth: int = 0, collapse_cms: bool = True) -> list[str]:
        indent = "  " * depth
        cms_tag = " [CMS]" if pg["page_type"] == "CMS" else ""
        secs = pg["sections"].split(", ")[:5]
        secs_part = f" [{', '.join(secs)}]" if secs and pg["sections"] else ""
        line = f"{indent}{pg['title']}{cms_tag}{secs_part}"
        result = [line]

        child_list = children.get(pg["title"], [])
        cms_children = [c for c in child_list if c["page_type"] == "CMS"]
        static_children = [c for c in child_list if c["page_type"] != "CMS"]

        # Static children always render normally
        for child in static_children:
            result.extend(render_tree(child, depth + 1, collapse_cms))

        # CMS children: collapse many to one template entry, or render normally
        if cms_children:
            if not collapse_cms or len(cms_children) == 1:
                for child in cms_children:
                    result.extend(render_tree(child, depth + 1, collapse_cms))
            else:
                # Use first CMS child's sections as the template representative
                template = cms_children[0]
                child_indent = "  " * (depth + 1)
                t_secs = template["sections"].split(", ")[:5]
                t_secs_part = f" [{', '.join(t_secs)}]" if t_secs and template["sections"] else ""
                result.append(f"{child_indent}[CMS Template]{t_secs_part}  ({len(cms_children)} pages)")
        return result

    collapse_cms = not all_cms
    tree_lines = []
    for root in roots:
        if root["title"] == "Untitled":
            continue
        tree_lines.extend(render_tree(root, collapse_cms=collapse_cms))

    output = f"{client_name} — Website Sitemap\n\n" + "\n".join(tree_lines)

    # ── Save ──────────────────────────────────────────────────────────────────
    out_dir = Path(__file__).parent.parent / "output" / client_key
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "relume_sitemap_export.txt"
    out_path.write_text(output)

    print(f"\n✓ Saved to: {out_path}")
    print(f"  Character count: {len(output)} / 5,000")
    if len(output) > 5000:
        print(f"  ⚠ Over limit — consider reducing sections per page further")
    print()
    print(output)

    print(f"\nPaste the contents of relume_sitemap_export.txt into Relume.")

    if open_output:
        import subprocess
        subprocess.run(["open", str(out_path)])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", default="summit_therapy")
    parser.add_argument("--open", action="store_true", help="Open the output file after export")
    parser.add_argument("--all-cms", action="store_true", help="Show all CMS pages instead of collapsing to one template entry per group")
    args = parser.parse_args()
    asyncio.run(main(args.client, open_output=args.open, all_cms=args.all_cms))
