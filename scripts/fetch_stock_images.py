#!/usr/bin/env python3
"""
fetch_stock_images.py — Pull curated stock images from Pexels for a client.

Two-pass workflow:
  Pass 1 (discovery): Queries Pexels across image categories, scores results via
    Claude for brand fit, and outputs an HTML report grouped by category.
    Flags photographers appearing 3+ times as "series candidates."

  Pass 2 (commit): Run with --photographer "Name" to pull more from a specific
    photographer's portfolio, or with --notes for revised search terms.
    Downloads final images to output/{client}/stock_images/.
    Saves metadata to Notion Images DB.

Image categories are derived from the client's approved sitemap services and
the "Photography Style" field in their Brand Guidelines DB.

Usage:
    python scripts/fetch_stock_images.py --client summit_therapy
    python scripts/fetch_stock_images.py --client summit_therapy --open
    python scripts/fetch_stock_images.py --client summit_therapy --photographer "Cottonbro Studio"
    python scripts/fetch_stock_images.py --client summit_therapy --notes "warmer tones, more candid"

Requirements:
    PEXELS_API_KEY must be set in .env
    Get a free key at: https://www.pexels.com/api/
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.clients import CLIENTS
from src.config import settings
from src.integrations.notion import NotionClient

PEXELS_API = "https://api.pexels.com/v1"

# ── Default image categories ──────────────────────────────────────────────────

DEFAULT_CATEGORIES = [
    {"key": "hero_lifestyle",   "label": "Hero Lifestyle",         "count": 5},
    {"key": "people_candid",    "label": "People — Candid",        "count": 4},
    {"key": "clinic_environment","label": "Clinic / Environment",  "count": 4},
    {"key": "detail_closeup",   "label": "Detail Close-Up",        "count": 3},
    {"key": "abstract_texture", "label": "Abstract / Texture",     "count": 3},
]


# ── Notion helpers ────────────────────────────────────────────────────────────

def _get_rich_text(prop: dict) -> str:
    return "".join(p.get("text", {}).get("content", "") for p in prop.get("rich_text", []))


def _get_title(prop: dict) -> str:
    return "".join(p.get("text", {}).get("content", "") for p in prop.get("title", []))


async def _read_brand_guidelines(notion: NotionClient, db_id: str) -> dict:
    entries = await notion.query_database(db_id)
    if not entries:
        return {}
    pp = entries[0]["properties"]
    return {
        "tone_descriptors": _get_rich_text(pp.get("Tone Descriptors", {})),
        "image_direction":  _get_rich_text(pp.get("Image Direction", {})),
        "photography_style": _get_rich_text(pp.get("Photography Style", {})),
        "primary_color":    _get_rich_text(pp.get("Primary Color", {})),
        "secondary_color":  _get_rich_text(pp.get("Secondary Color", {})),
    }


async def _read_approved_services(notion: NotionClient, sitemap_db_id: str) -> list[str]:
    """Return unique service/page titles from approved sitemap pages."""
    entries = await notion.query_database(sitemap_db_id)
    services = []
    for e in entries:
        pp = e["properties"]
        title = _get_title(pp.get("Page Title", {})) or _get_title(pp.get("Name", {}))
        if title and title != "Untitled":
            services.append(title)
    return services[:20]  # cap at 20 for prompt length


# ── Claude: generate search queries ──────────────────────────────────────────

async def _generate_search_queries(
    client_name: str,
    brand: dict,
    services: list[str],
    categories: list[dict],
    notes: str,
) -> dict[str, list[str]]:
    """Ask Claude to write 2 Pexels search queries per category."""
    import anthropic
    ac = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    services_summary = ", ".join(services[:12]) if services else "general healthcare"

    style_block = ""
    if brand.get("photography_style"):
        style_block = f"\nPhotography Style (from brand guidelines):\n{brand['photography_style']}"
    if brand.get("image_direction"):
        style_block += f"\nImage Direction notes:\n{brand['image_direction']}"
    if brand.get("tone_descriptors"):
        style_block += f"\nBrand tone: {brand['tone_descriptors']}"
    if notes:
        style_block += f"\nAdditional notes from reviewer: {notes}"

    categories_list = "\n".join(f"- {c['key']}: {c['label']}" for c in categories)

    prompt = f"""You are helping source stock photography for {client_name}, a pediatric and adult therapy clinic
offering: {services_summary}.
{style_block}

For each image category below, write exactly 2 Pexels search queries that will return
high-quality, on-brand results. Queries should be 2–5 words, specific, and avoid generic
stock photo clichés (no "happy family", "pointing at whiteboard", "handshake").

Categories:
{categories_list}

Return a JSON object with category keys mapping to arrays of 2 query strings.
Example format:
{{
  "hero_lifestyle": ["child speech therapy session", "pediatric therapy clinic warm"],
  "people_candid": ["therapist child playing floor", "family therapy session candid"]
}}

Return only the JSON object, no other text."""

    msg = await ac.messages.create(
        model=settings.anthropic_model,
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(raw)


# ── Pexels API ────────────────────────────────────────────────────────────────

async def _pexels_search(
    client: httpx.AsyncClient,
    query: str,
    per_page: int = 15,
    photographer_id: int | None = None,
) -> list[dict]:
    params = {"query": query, "per_page": per_page, "orientation": "landscape"}
    resp = await client.get(
        f"{PEXELS_API}/search",
        params=params,
        headers={"Authorization": settings.pexels_api_key},
    )
    resp.raise_for_status()
    photos = resp.json().get("photos", [])
    if photographer_id:
        photos = [p for p in photos if p["photographer_id"] == photographer_id]
    return photos


async def _fetch_photographer_photos(
    client: httpx.AsyncClient,
    photographer_id: int,
    per_page: int = 30,
) -> list[dict]:
    """Pull recent photos from a specific photographer."""
    resp = await client.get(
        f"{PEXELS_API}/search",
        params={"query": "therapy", "per_page": 1},
        headers={"Authorization": settings.pexels_api_key},
    )
    # Pexels doesn't have a photographer endpoint in the free API,
    # so we search and filter by photographer_id across multiple queries
    return []


# ── Claude: score and select images ──────────────────────────────────────────

async def _score_images(
    client_name: str,
    brand: dict,
    category_label: str,
    photos: list[dict],
    target_count: int,
) -> list[dict]:
    """Ask Claude to pick the best photos for a category."""
    import anthropic
    ac = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    style_context = brand.get("photography_style") or brand.get("image_direction") or ""

    candidates = [
        {
            "id": p["id"],
            "photographer": p["photographer"],
            "photographer_id": p["photographer_id"],
            "alt": p.get("alt", ""),
            "url": p["src"]["medium"],
            "width": p["width"],
            "height": p["height"],
        }
        for p in photos
    ]

    prompt = f"""You are curating stock photography for {client_name} — a pediatric and adult therapy clinic.

Category: {category_label}
Style guidance: {style_context or "Warm, professional, authentic — avoid overly posed or sterile imagery."}

Review these {len(candidates)} candidate images and select the best {target_count} for this category.
Prioritize: authenticity, brand alignment, visual quality, and consistency with other selections.
Avoid: overly staged poses, harsh lighting, dated styling, obvious stock photo clichés.

Candidates (id, photographer, alt text):
{json.dumps([{"id": c["id"], "photographer": c["photographer"], "alt": c["alt"]} for c in candidates], indent=2)}

Return a JSON array of the {target_count} best image IDs in order of preference.
Example: [12345, 67890, 11111]
Return only the JSON array, no other text."""

    msg = await ac.messages.create(
        model=settings.anthropic_model,
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
    selected_ids = json.loads(raw)

    id_map = {c["id"]: c for c in candidates}
    return [id_map[i] for i in selected_ids if i in id_map]


# ── HTML report ───────────────────────────────────────────────────────────────

def _build_html_report(
    client_name: str,
    results: dict[str, list[dict]],
    photographer_counts: Counter,
    notes: str,
) -> str:
    series_candidates = [
        (name, count)
        for name, count in photographer_counts.most_common()
        if count >= 3
    ]

    series_html = ""
    if series_candidates:
        items = "".join(
            f'<li><strong>{name}</strong> — {count} images selected</li>'
            for name, count in series_candidates
        )
        series_html = f"""
        <div class="series-box">
          <h2>Series Candidates</h2>
          <p>These photographers appear 3+ times in the selection. Consider leaning into one for visual consistency:</p>
          <ul>{items}</ul>
          <p class="hint">Re-run with <code>--photographer "Name"</code> to pull more from their portfolio.</p>
        </div>"""

    category_html = ""
    for cat_key, photos in results.items():
        label = next((c["label"] for c in DEFAULT_CATEGORIES if c["key"] == cat_key), cat_key)
        grid_items = "".join(
            f'''<div class="photo-card">
                  <img src="{p["url"]}" alt="{p["alt"]}" loading="lazy">
                  <div class="photo-meta">
                    <span class="photographer">{p["photographer"]}</span>
                    <span class="photo-id">ID: {p["id"]}</span>
                  </div>
                </div>'''
            for p in photos
        )
        category_html += f"""
        <div class="category">
          <h2>{label} <span class="count">({len(photos)} images)</span></h2>
          <div class="grid">{grid_items}</div>
        </div>"""

    notes_html = f'<p class="notes">Generated with notes: <em>{notes}</em></p>' if notes else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{client_name} — Stock Image Discovery Report</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; max-width: 1400px; margin: 0 auto; padding: 2rem; background: #f8f8f8; color: #222; }}
    h1 {{ font-size: 1.6rem; margin-bottom: 0.25rem; }}
    .subtitle {{ color: #666; margin-bottom: 2rem; font-size: 0.9rem; }}
    .notes {{ color: #888; font-size: 0.85rem; margin-bottom: 1.5rem; }}
    .series-box {{ background: #fff8e1; border: 1px solid #ffe082; border-radius: 8px; padding: 1.25rem 1.5rem; margin-bottom: 2rem; }}
    .series-box h2 {{ margin: 0 0 0.5rem; font-size: 1rem; color: #b45309; }}
    .series-box ul {{ margin: 0.5rem 0; padding-left: 1.25rem; }}
    .series-box .hint {{ margin: 0.75rem 0 0; font-size: 0.8rem; color: #78716c; }}
    .category {{ margin-bottom: 3rem; }}
    .category h2 {{ font-size: 1.1rem; border-bottom: 2px solid #e5e7eb; padding-bottom: 0.5rem; margin-bottom: 1rem; }}
    .count {{ font-weight: 400; color: #888; font-size: 0.9rem; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 1rem; }}
    .photo-card {{ background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
    .photo-card img {{ width: 100%; height: 200px; object-fit: cover; display: block; }}
    .photo-meta {{ padding: 0.6rem 0.75rem; display: flex; justify-content: space-between; align-items: center; }}
    .photographer {{ font-size: 0.78rem; font-weight: 600; color: #374151; }}
    .photo-id {{ font-size: 0.72rem; color: #9ca3af; font-family: monospace; }}
  </style>
</head>
<body>
  <h1>{client_name} — Stock Image Discovery Report</h1>
  <p class="subtitle">Pass 1 — Review and identify photographer series candidates before downloading</p>
  {notes_html}
  {series_html}
  {category_html}
</body>
</html>"""


# ── Download images ───────────────────────────────────────────────────────────

async def _download_images(
    http: httpx.AsyncClient,
    photos_by_category: dict[str, list[dict]],
    out_dir: Path,
) -> dict[str, list[dict]]:
    """Download full-size images locally, return updated photo dicts with local_path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    updated: dict[str, list[dict]] = {}

    for cat_key, photos in photos_by_category.items():
        updated[cat_key] = []
        for photo in photos:
            photo_id = photo["id"]
            filename = f"{cat_key}_{photo_id}.jpg"
            local_path = out_dir / filename

            if not local_path.exists():
                # Use large size for download
                orig_url = photo.get("src_large") or photo["url"].replace("medium", "large")
                try:
                    resp = await http.get(orig_url, follow_redirects=True, timeout=30)
                    resp.raise_for_status()
                    local_path.write_bytes(resp.content)
                except Exception as e:
                    print(f"  ⚠ Could not download {photo_id}: {e}")
                    local_path = None

            updated[cat_key].append({**photo, "local_path": str(local_path) if local_path else None, "filename": filename})

    return updated


# ── Notion: save to Images DB ─────────────────────────────────────────────────

async def _save_to_notion(
    notion: NotionClient,
    images_db_id: str,
    client_name: str,
    photos_by_category: dict[str, list[dict]],
) -> None:
    for cat_key, photos in photos_by_category.items():
        label = next((c["label"] for c in DEFAULT_CATEGORIES if c["key"] == cat_key), cat_key)
        for photo in photos:
            props = {
                "Image Name": {"title": [{"text": {"content": f"{label} — {photo['photographer']} ({photo['id']})"}}]},
                "Batch": {"select": {"name": "Stock"}},
                "Category": {"select": {"name": label}},
                "Page": {"rich_text": [{"text": {"content": ""}}]},
                "Status": {"select": {"name": "Candidate"}},
                "Image URL": {"url": photo["url"]},
                "Source": {"rich_text": [{"text": {"content": f"Pexels — {photo['photographer']} (ID: {photo['id']})"}}]},
            }
            try:
                await notion.create_database_entry(images_db_id, props)
            except Exception as e:
                print(f"  ⚠ Notion save failed for {photo['id']}: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(
    client_key: str,
    notes: str,
    photographer_name: str | None,
    commit: bool,
    open_output: bool,
) -> None:
    if not settings.pexels_api_key:
        print("✗ PEXELS_API_KEY is not set in .env")
        print("  Get a free key at: https://www.pexels.com/api/")
        sys.exit(1)

    cfg = CLIENTS[client_key]
    client_name = cfg.get("name", client_key)
    notion = NotionClient(settings.notion_api_key)

    out_dir = Path(__file__).parent.parent / "output" / client_key
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Fetching stock images for {client_name}...")

    # ── Read brand context ────────────────────────────────────────────────────
    brand = await _read_brand_guidelines(notion, cfg["brand_guidelines_db_id"])
    services = await _read_approved_services(notion, cfg["sitemap_db_id"])
    print(f"  Brand guidelines loaded. Services: {len(services)} pages")

    categories = DEFAULT_CATEGORIES

    # ── Generate search queries via Claude ────────────────────────────────────
    print("  Generating search queries via Claude...")
    queries = await _generate_search_queries(client_name, brand, services, categories, notes)
    print(f"  Queries ready: {sum(len(v) for v in queries.values())} total")

    # ── Search Pexels ─────────────────────────────────────────────────────────
    all_photos_by_cat: dict[str, list[dict]] = {}
    photographer_counter: Counter = Counter()

    async with httpx.AsyncClient(timeout=20) as http:
        for cat in categories:
            cat_key = cat["key"]
            cat_queries = queries.get(cat_key, [cat["label"]])
            raw_photos: list[dict] = []

            for q in cat_queries:
                print(f"  Searching Pexels: '{q}'...")
                try:
                    results = await _pexels_search(http, q, per_page=15)
                    # If leaning into a specific photographer, filter
                    if photographer_name:
                        results = [
                            r for r in results
                            if photographer_name.lower() in r["photographer"].lower()
                        ]
                    raw_photos.extend(results)
                except Exception as e:
                    print(f"  ⚠ Search failed for '{q}': {e}")

            # Deduplicate by photo ID
            seen = set()
            unique = []
            for p in raw_photos:
                if p["id"] not in seen:
                    seen.add(p["id"])
                    unique.append(p)

            # Score and select via Claude
            if unique:
                print(f"  Scoring {len(unique)} candidates for {cat['label']}...")
                selected = await _score_images(client_name, brand, cat["label"], unique, cat["count"])
                all_photos_by_cat[cat_key] = selected
                for photo in selected:
                    photographer_counter[photo["photographer"]] += 1
            else:
                print(f"  ⚠ No results for {cat['label']}")
                all_photos_by_cat[cat_key] = []

    total = sum(len(v) for v in all_photos_by_cat.values())
    print(f"\n  Selected {total} images across {len(all_photos_by_cat)} categories")

    # ── Photographer series summary ───────────────────────────────────────────
    series = [(n, c) for n, c in photographer_counter.most_common() if c >= 3]
    if series:
        print("\n  Series candidates (3+ images from same photographer):")
        for name, count in series:
            print(f"    • {name}: {count} images")
        print("  → Re-run with --photographer \"Name\" to lean into one series")

    # ── Build HTML report ─────────────────────────────────────────────────────
    report_html = _build_html_report(client_name, all_photos_by_cat, photographer_counter, notes)
    report_path = out_dir / "stock_images_report.html"
    report_path.write_text(report_html)
    print(f"\n✓ Report saved: {report_path}")

    if commit:
        # ── Download images locally ───────────────────────────────────────────
        images_dir = out_dir / "stock_images"
        print(f"\nDownloading {total} images to {images_dir}...")
        async with httpx.AsyncClient(timeout=60) as http:
            downloaded = await _download_images(http, all_photos_by_cat, images_dir)

        downloaded_count = sum(
            1 for photos in downloaded.values()
            for p in photos if p.get("local_path")
        )
        print(f"✓ Downloaded {downloaded_count}/{total} images")

        # ── Save to Notion ────────────────────────────────────────────────────
        if cfg.get("images_db_id"):
            print("Saving to Notion Images DB...")
            await _save_to_notion(notion, cfg["images_db_id"], client_name, downloaded)
            print("✓ Saved to Notion")
        else:
            print("⚠ No images_db_id in client config — skipping Notion save")
    else:
        print("\nPass 1 complete — discovery only. Review the report then run:")
        print(f"  make stock-images CLIENT={client_key}  (to download and save)")
        print(f"  make stock-images CLIENT={client_key} PHOTOGRAPHER=\"Name\"  (to lean into a series)")

    if open_output:
        import subprocess
        subprocess.run(["open", str(report_path)])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", default="summit_therapy")
    parser.add_argument("--notes", default="", help="Feedback or style direction for this run")
    parser.add_argument("--photographer", default=None, help="Lean into a specific photographer's work")
    parser.add_argument("--commit", action="store_true", help="Download images + save to Notion (Pass 2)")
    parser.add_argument("--open", action="store_true", dest="open_output", help="Open report in browser")
    args = parser.parse_args()
    asyncio.run(main(
        client_key=args.client,
        notes=args.notes,
        photographer_name=args.photographer,
        commit=args.commit,
        open_output=args.open_output,
    ))
