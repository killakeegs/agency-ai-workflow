#!/usr/bin/env python3
"""
gbp_posts.py — Generate Google Business Profile post drafts from website content

Reads approved sitemap pages + their Notion Content DB entries, picks 3 pages
to post about, and generates one GBP post per page grounded in the actual copy.

Posts are written to the client's GBP Posts DB in Notion for team review.

Usage:
    make gbp-posts CLIENT=summit_therapy
    make gbp-posts CLIENT=summit_therapy NOTES="make post 2 warmer, less clinical"
    make gbp-posts CLIENT=summit_therapy MONTH="May 2026"

Flow:
    1. Load brand guidelines + client info (voice, tone, website URL)
    2. Load approved sitemap pages (those with content in Content DB)
    3. Pick 3 pages — rotating by post type: service, educational, community
    4. Generate one 150–300 word GBP post per page, grounded in page copy
    5. Write 3 drafts to GBP Posts DB in Notion
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import anthropic

from config.clients import CLIENTS
from src.config import settings
from src.integrations.notion import NotionClient

CLIENTS_JSON_PATH = Path(__file__).parent.parent / "config" / "clients.json"

# ── Notion field helpers ───────────────────────────────────────────────────────

def _get_rt(prop: dict) -> str:
    if not prop:
        return ""
    return "".join(p.get("text", {}).get("content", "") for p in prop.get("rich_text", []))

def _get_title(prop: dict) -> str:
    if not prop:
        return ""
    return "".join(p.get("text", {}).get("content", "") for p in prop.get("title", []))

def _get_url(prop: dict) -> str:
    if not prop:
        return ""
    return prop.get("url", "") or ""

def _get_select(prop: dict) -> str:
    if not prop:
        return ""
    sel = prop.get("select")
    return sel.get("name", "") if sel else ""

def _get_checkbox(prop: dict) -> bool:
    if not prop:
        return False
    return bool(prop.get("checkbox", False))


# ── GBP Posts DB schema ────────────────────────────────────────────────────────

GBP_POSTS_SCHEMA = {
    "Post Title":     {"title": {}},
    "Post Body":      {"rich_text": {}},
    "Post Type": {
        "select": {
            "options": [
                {"name": "Service",     "color": "blue"},
                {"name": "Educational", "color": "green"},
                {"name": "Community",   "color": "yellow"},
                {"name": "Offer",       "color": "orange"},
                {"name": "Event",       "color": "purple"},
            ]
        }
    },
    "CTA Button": {
        "select": {
            "options": [
                {"name": "Book",       "color": "blue"},
                {"name": "Call",       "color": "green"},
                {"name": "Learn More", "color": "gray"},
                {"name": "Sign Up",    "color": "orange"},
            ]
        }
    },
    "CTA URL":        {"url": {}},
    "Status": {
        "select": {
            "options": [
                {"name": "Draft",     "color": "gray"},
                {"name": "Approved",  "color": "green"},
                {"name": "Scheduled", "color": "blue"},
                {"name": "Published", "color": "purple"},
            ]
        }
    },
    "Month":           {"rich_text": {}},
    "Source Page":     {"rich_text": {}},
    "Primary Keyword": {"rich_text": {}},
    "Char Count":      {"number": {}},
    "Feedback":        {"rich_text": {}},
}


async def _ensure_gbp_posts_db(
    notion: NotionClient,
    client_key: str,
    cfg: dict,
) -> str:
    """Create GBP Posts DB under the client's Notion page if it doesn't exist yet."""
    db_id = cfg.get("gbp_posts_db_id", "")
    if db_id:
        return db_id

    # Find the client root page — use client_info_db_id's parent
    client_info_db = cfg.get("client_info_db_id", "")
    if not client_info_db:
        raise ValueError(f"No client_info_db_id found for {client_key}")

    # Get parent page of the client info DB
    db_meta = await notion._client.request(
        path=f"databases/{client_info_db}",
        method="GET",
    )
    parent = db_meta.get("parent", {})
    parent_page_id = parent.get("page_id", "")
    if not parent_page_id:
        raise ValueError("Could not determine parent page for GBP Posts DB")

    result = await notion._client.request(
        path="databases",
        method="POST",
        body={
            "parent":      {"type": "page_id", "page_id": parent_page_id},
            "title":       [{"type": "text", "text": {"content": "GBP Posts"}}],
            "properties":  GBP_POSTS_SCHEMA,
        },
    )
    new_db_id = result["id"]
    print(f"  Created GBP Posts DB: {new_db_id}")

    # Save to clients.json
    _save_db_id(client_key, "gbp_posts_db_id", new_db_id)
    return new_db_id


def _save_db_id(client_key: str, field: str, value: str) -> None:
    """Persist a new DB ID into config/clients.json."""
    try:
        data = json.loads(CLIENTS_JSON_PATH.read_text()) if CLIENTS_JSON_PATH.exists() else {}
        if client_key not in data:
            data[client_key] = {}
        data[client_key][field] = value
        CLIENTS_JSON_PATH.write_text(json.dumps(data, indent=4))
    except Exception as e:
        print(f"  ⚠ Could not save {field} to clients.json: {e}")


# ── Data loaders ───────────────────────────────────────────────────────────────

def _blocks_to_text(blocks: list[dict]) -> str:
    """Extract plain text from Notion block children."""
    parts = []
    for block in blocks:
        btype = block.get("type", "")
        content = block.get(btype, {})
        rich = content.get("rich_text", [])
        text = "".join(r.get("text", {}).get("content", "") for r in rich)
        if text:
            parts.append(text)
    return "\n".join(parts)


async def _load_brand(notion: NotionClient, cfg: dict) -> dict:
    """Load brand guidelines and client info."""
    brand: dict = {}

    # Brand Guidelines DB
    bg_db = cfg.get("brand_guidelines_db_id", "")
    if bg_db:
        rows = await notion._client.request(
            path=f"databases/{bg_db}/query",
            method="POST",
            body={"page_size": 1},
        )
        if rows.get("results"):
            props = rows["results"][0].get("properties", {})
            brand["voice"]       = _get_rt(props.get("Voice & Tone", {}))
            brand["power_words"] = _get_rt(props.get("Power Words", {}))
            brand["avoid_words"] = _get_rt(props.get("Words to Avoid", {}))
            brand["cta_style"]   = _get_rt(props.get("CTA Style", {}))
            brand["tone_desc"]   = _get_rt(props.get("Tone of Voice", {}))

    # Client Info DB — business name + website URL
    ci_db = cfg.get("client_info_db_id", "")
    if ci_db:
        rows = await notion._client.request(
            path=f"databases/{ci_db}/query",
            method="POST",
            body={"page_size": 1},
        )
        if rows.get("results"):
            props = rows["results"][0].get("properties", {})
            # Try common field names for business name
            brand["business_name"] = (
                _get_title(props.get("Company", {})) or
                _get_title(props.get("Client Name", {})) or
                _get_rt(props.get("Company", {})) or
                cfg.get("name", "")
            )
            # Try common field names for website URL
            brand["website_url"] = (
                _get_url(props.get("Website", {})) or
                _get_url(props.get("Current Website URL", {})) or
                _get_rt(props.get("Website", {})) or
                _get_rt(props.get("Current Website URL", {})) or ""
            )

    brand.setdefault("business_name", cfg.get("name", ""))
    brand.setdefault("website_url", "")
    return brand


async def _load_content_pages(notion: NotionClient, cfg: dict) -> list[dict]:
    """
    Load content DB entries that have page blocks written by the content agent.
    Returns list of dicts: {page_name, slug, h1, body, primary_keyword, meta}
    Excludes utility pages (Privacy, Terms, Contact, Blog hub, etc.)
    """
    content_db = cfg.get("content_db_id", "")
    if not content_db:
        return []

    # Load all content entries
    content_rows = await notion._client.request(
        path=f"databases/{content_db}/query",
        method="POST",
        body={"page_size": 100},
    )

    skip_keywords = ["privacy", "terms", "accessibility", "404", "thank you",
                     "blog", "contact", "insurance", "billing", "new patient", "resources"]

    pages = []
    for row in content_rows.get("results", []):
        props   = row.get("properties", {})
        # Content DB uses "Page Title" as the title field
        name    = _get_title(props.get("Page Title", {})) or _get_title(props.get("Page Name", {}))
        h1      = _get_rt(props.get("H1", {}))
        keyword = _get_rt(props.get("Primary Keyword", {}))
        meta    = _get_rt(props.get("Meta Description", {}))
        slug    = _get_rt(props.get("Slug", {}))

        if not name:
            continue

        lower = name.lower()
        if any(s in lower for s in skip_keywords):
            continue

        # Body copy is stored as Notion page blocks (not a property)
        try:
            block_resp = await notion._client.request(
                path=f"blocks/{row['id']}/children",
                method="GET",
            )
            blocks = block_resp.get("results", [])
            body = _blocks_to_text(blocks)[:1500]
        except Exception:
            body = ""

        if not body and not h1:
            continue  # skip empty pages

        pages.append({
            "page_name":       name,
            "slug":            slug,
            "h1":              h1,
            "body":            body,
            "primary_keyword": keyword,
            "meta":            meta,
        })

    return pages


# ── Post generation ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a local SEO content writer specializing in Google Business Profile posts.
Your posts are grounded in the client's actual website content — never invented.

Rules:
- 150–300 words per post (GBP displays ~1,500 chars; aim for ~800 chars)
- Lead with a hook relevant to the page topic
- Use the page's primary keyword naturally (once or twice, not stuffed)
- Match the brand voice exactly — no corporate jargon, no em dashes (—)
- End with a single clear CTA tied to the page
- No hashtags
- No generic filler like "We are committed to excellence"
- Posts must feel local and specific, not chain-franchise generic

Post types:
- Service: highlights a specific service offering, who it helps, what to expect
- Educational: teaches something useful from the page (tip, myth-bust, explainer)
- Community: connects the service to local life, seasons, back-to-school, etc.
"""

def _build_prompt(
    brand: dict,
    pages: list[dict],
    month: str,
    notes: str,
    post_count: int = 8,
) -> str:
    business = brand.get("business_name", "the business")
    website  = brand.get("website_url", "").rstrip("/")
    voice    = brand.get("voice", "") or brand.get("tone_desc", "")
    power    = brand.get("power_words", "")
    avoid    = brand.get("avoid_words", "")
    cta_style= brand.get("cta_style", "")

    page_blocks = []
    for i, p in enumerate(pages, 1):
        slug = p.get("slug", "").lstrip("/")
        url  = f"{website}/{slug}" if slug and website else website
        page_blocks.append(
            f"PAGE {i}: {p['page_name']}\n"
            f"H1: {p['h1']}\n"
            f"Keyword: {p['primary_keyword']}\n"
            f"URL: {url}\n"
            f"Copy excerpt:\n{p['body'][:800]}"
        )

    pages_text = "\n\n---\n\n".join(page_blocks)

    brand_block = f"Business: {business}\n"
    if voice:    brand_block += f"Voice & tone: {voice}\n"
    if power:    brand_block += f"Power words to use: {power}\n"
    if avoid:    brand_block += f"Words to avoid: {avoid}\n"
    if cta_style:brand_block += f"CTA style: {cta_style}\n"

    notes_block = f"\nRevision notes from team: {notes}\n" if notes else ""

    distribution_lines = []
    types_cycle = ["Service", "Educational", "Service", "Community",
                   "Service", "Educational", "Community", "Service or Offer"]
    for i in range(1, post_count + 1):
        ptype = types_cycle[(i - 1) % len(types_cycle)]
        distribution_lines.append(f"- Post {i} → {ptype}")
    distribution = "\n".join(distribution_lines)

    return f"""\
Generate {post_count} Google Business Profile post drafts for {month}.

{brand_block}{notes_block}
One post per page below. Assign post types in this order:
{distribution}

SOURCE PAGES:
{pages_text}

Return ONLY a JSON array — no markdown, no preamble:
[
  {{
    "post_number": 1,
    "post_type": "Service",
    "title": "short internal title (not published)",
    "body": "full post text",
    "cta_button": "Book" | "Call" | "Learn More" | "Sign Up",
    "cta_url": "full URL to the source page",
    "primary_keyword": "keyword used",
    "source_page": "page name"
  }},
  ...
]
"""


def _pick_pages(pages: list[dict], count: int = 3) -> list[dict]:
    """
    Pick {count} pages to post about.
    Prioritizes: home, service hubs/subcategories, who-we-serve pages.
    Excludes: blog hub, contact, insurance/billing.
    """
    priority_keywords = [
        "home", "speech", "occupational", "physical", "therapy",
        "children", "adults", "who we serve", "autism", "sensory",
        "feeding", "language", "motor",
    ]
    skip_keywords = ["blog", "contact", "insurance", "billing", "privacy",
                     "terms", "accessibility", "new patient", "resources"]

    scored = []
    for p in pages:
        lower = p["page_name"].lower()
        if any(s in lower for s in skip_keywords):
            continue
        score = sum(1 for kw in priority_keywords if kw in lower)
        # Prefer pages with content and a keyword
        if p.get("primary_keyword"):
            score += 2
        if p.get("h1"):
            score += 1
        scored.append((score, p))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in scored[:count]]


async def _write_posts(
    notion: NotionClient,
    gbp_posts_db_id: str,
    posts: list[dict],
    month: str,
) -> None:
    """Write generated posts to the GBP Posts DB in Notion."""
    for post in posts:
        body_text = post.get("body", "")
        char_count = len(body_text)

        properties = {
            "Post Title": {
                "title": [{"text": {"content": post.get("title", f"GBP Post — {post.get('source_page', '')}")}}]
            },
            "Post Body": {
                "rich_text": [{"text": {"content": body_text}}]
            },
            "Post Type": {
                "select": {"name": post.get("post_type", "Service")}
            },
            "CTA Button": {
                "select": {"name": post.get("cta_button", "Learn More")}
            },
            "Status": {
                "select": {"name": "Draft"}
            },
            "Month": {
                "rich_text": [{"text": {"content": month}}]
            },
            "Source Page": {
                "rich_text": [{"text": {"content": post.get("source_page", "")}}]
            },
            "Primary Keyword": {
                "rich_text": [{"text": {"content": post.get("primary_keyword", "")}}]
            },
            "Char Count": {
                "number": char_count
            },
        }

        cta_url = post.get("cta_url", "")
        if cta_url:
            properties["CTA URL"] = {"url": cta_url}

        await notion._client.request(
            path="pages",
            method="POST",
            body={
                "parent":     {"database_id": gbp_posts_db_id},
                "properties": properties,
            },
        )
        print(f"  ✓ {post.get('post_type', 'Post')} — {post.get('source_page', '')} ({char_count} chars)")


# ── Main ───────────────────────────────────────────────────────────────────────

async def run(client_key: str, month: str, notes: str) -> None:
    cfg = CLIENTS.get(client_key)
    if not cfg:
        print(f"Client '{client_key}' not found in config.")
        sys.exit(1)

    notion = NotionClient(api_key=settings.notion_api_key)
    client_name = cfg.get("name", client_key)

    print(f"\n{'='*60}")
    print(f"  GBP Post Generator — {client_name} — {month}")
    print(f"{'='*60}\n")

    # 1. Ensure GBP Posts DB exists
    print("Checking GBP Posts DB...")
    gbp_posts_db_id = await _ensure_gbp_posts_db(notion, client_key, cfg)

    # 2. Load brand + content
    print("Loading brand guidelines...")
    brand = await _load_brand(notion, cfg)
    print(f"  Business: {brand['business_name']}")
    print(f"  Website:  {brand['website_url'] or '(not set)'}")

    print("Loading content pages...")
    all_pages = await _load_content_pages(notion, cfg)
    print(f"  Found {len(all_pages)} pages with content")

    if not all_pages:
        print("\n⚠ No content pages found. Run 'make content' first to generate page copy.")
        sys.exit(1)

    # 3. Pick pages based on configured count
    services   = cfg.get("services", {})
    gbp_count  = int(services.get("gbp_posts_per_month", 8) or 8)
    gbp_count  = max(1, min(16, gbp_count))  # clamp 1–16

    selected = _pick_pages(all_pages, count=gbp_count)
    print(f"\nSelected pages for this month's {gbp_count} posts:")
    for p in selected:
        print(f"  • {p['page_name']} ({p.get('primary_keyword', 'no keyword')})")

    # 4. Generate posts
    print(f"\nGenerating {gbp_count} posts with Claude...")
    if notes:
        print(f"  Revision notes: {notes}")

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    prompt = _build_prompt(brand, selected, month, notes, post_count=gbp_count)

    response = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=6000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()

    # Extract JSON array
    import re
    match = re.search(r'\[.*\]', raw, re.DOTALL)
    if not match:
        print("\n⚠ Could not parse Claude response as JSON array.")
        print(raw[:500])
        sys.exit(1)

    posts = json.loads(match.group(0))
    print(f"  Generated {len(posts)} posts\n")

    # 5. Write to Notion
    print("Writing to Notion GBP Posts DB...")
    await _write_posts(notion, gbp_posts_db_id, posts, month)

    print(f"\n✓ Done. Review drafts in Notion → GBP Posts DB.")
    if notes:
        print("  (Revision run — previous drafts are preserved above these new ones)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate GBP post drafts from website content")
    parser.add_argument("--client", required=True, help="Client key (e.g. summit_therapy)")
    parser.add_argument("--month",  default=date.today().strftime("%B %Y"), help="Month label (e.g. 'May 2026')")
    parser.add_argument("--notes",  default="", help="Revision notes from previous run")
    args = parser.parse_args()

    asyncio.run(run(args.client, args.month, args.notes))


if __name__ == "__main__":
    main()
