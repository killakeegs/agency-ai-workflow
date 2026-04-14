#!/usr/bin/env python3
"""
social_posts.py — Generate Instagram/Facebook post drafts (~8/month)

Reads approved/upcoming blog posts + website content pages, generates 8 social
posts with varied types. Posts land in the Social Posts DB in Notion for team
review. Team attaches creatives and flips Status to Scheduled.

Post types generated:
- Blog Tie-In: tied to an upcoming approved blog post
- Educational: tip or myth-bust from a service page
- Service: highlights a specific service + who it helps
- Seasonal/Community: local moment, awareness month, season

Usage:
    make social-posts CLIENT=summit_therapy
    make social-posts CLIENT=summit_therapy NOTES="more seasonal content this month"
    make social-posts CLIENT=summit_therapy MONTH="May 2026"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import anthropic

from config.clients import CLIENTS
from src.config import settings
from src.integrations.notion import NotionClient

CLIENTS_JSON_PATH = Path(__file__).parent.parent.parent / "config" / "clients.json"


# ── Notion field helpers ───────────────────────────────────────────────────────

def _rt(prop: dict) -> str:
    if not prop:
        return ""
    return "".join(p.get("text", {}).get("content", "") for p in prop.get("rich_text", []))

def _title_text(prop: dict) -> str:
    if not prop:
        return ""
    return "".join(p.get("text", {}).get("content", "") for p in prop.get("title", []))

def _select(prop: dict) -> str:
    if not prop:
        return ""
    sel = prop.get("select")
    return sel.get("name", "") if sel else ""

def _url(prop: dict) -> str:
    if not prop:
        return ""
    return prop.get("url", "") or ""

def _blocks_to_text(blocks: list[dict]) -> str:
    parts = []
    for block in blocks:
        btype = block.get("type", "")
        content = block.get(btype, {})
        rich = content.get("rich_text", [])
        text = "".join(r.get("text", {}).get("content", "") for r in rich)
        if text:
            parts.append(text)
    return "\n".join(parts)


# ── Social Posts DB schema ─────────────────────────────────────────────────────

SOCIAL_POSTS_SCHEMA = {
    "Post Title": {"title": {}},
    "Caption":    {"rich_text": {}},
    "Platform": {
        "select": {
            "options": [
                {"name": "Instagram + Facebook", "color": "blue"},
                {"name": "Instagram",            "color": "pink"},
                {"name": "Facebook",             "color": "blue"},
            ]
        }
    },
    "Post Type": {
        "select": {
            "options": [
                {"name": "Educational",      "color": "green"},
                {"name": "Service",          "color": "blue"},
                {"name": "Blog Tie-In",      "color": "purple"},
                {"name": "Behind the Scenes","color": "yellow"},
                {"name": "Testimonial",      "color": "orange"},
                {"name": "Seasonal",         "color": "red"},
            ]
        }
    },
    "Status": {
        "select": {
            "options": [
                {"name": "Draft",        "color": "gray"},
                {"name": "Approved",     "color": "green"},
                {"name": "Image Needed", "color": "yellow"},
                {"name": "Scheduled",    "color": "blue"},
                {"name": "Published",    "color": "purple"},
            ]
        }
    },
    "Month":                  {"rich_text": {}},
    "Source":                 {"rich_text": {}},
    "Image Notes":            {"rich_text": {}},
    "Hashtags":               {"rich_text": {}},
    "Suggested Publish Date": {"date": {}},
    "Char Count":             {"number": {}},
    "Feedback":               {"rich_text": {}},
}


# ── DB setup ───────────────────────────────────────────────────────────────────

async def _ensure_social_posts_db(notion: NotionClient, client_key: str, cfg: dict) -> str:
    db_id = cfg.get("social_posts_db_id", "")
    if db_id:
        return db_id

    client_info_db = cfg.get("client_info_db_id", "")
    if not client_info_db:
        raise ValueError(f"No client_info_db_id found for {client_key}")

    db_meta = await notion._client.request(path=f"databases/{client_info_db}", method="GET")
    parent_page_id = db_meta.get("parent", {}).get("page_id", "")
    if not parent_page_id:
        raise ValueError("Could not determine parent page for Social Posts DB")

    result = await notion._client.request(
        path="databases",
        method="POST",
        body={
            "parent":     {"type": "page_id", "page_id": parent_page_id},
            "title":      [{"type": "text", "text": {"content": "Social Posts"}}],
            "properties": SOCIAL_POSTS_SCHEMA,
        },
    )
    new_db_id = result["id"]
    print(f"  Created Social Posts DB: {new_db_id}")
    _save_db_id(client_key, "social_posts_db_id", new_db_id)
    return new_db_id


def _save_db_id(client_key: str, field: str, value: str) -> None:
    try:
        data = json.loads(CLIENTS_JSON_PATH.read_text()) if CLIENTS_JSON_PATH.exists() else {}
        if client_key not in data:
            data[client_key] = {}
        data[client_key][field] = value
        CLIENTS_JSON_PATH.write_text(json.dumps(data, indent=4))
    except Exception as e:
        print(f"  ⚠ Could not save {field} to clients.json: {e}")


# ── Data loaders ───────────────────────────────────────────────────────────────

async def _load_brand(notion: NotionClient, cfg: dict) -> dict:
    brand: dict = {}

    bg_db = cfg.get("brand_guidelines_db_id", "")
    if bg_db:
        rows = await notion._client.request(
            path=f"databases/{bg_db}/query", method="POST", body={"page_size": 1}
        )
        if rows.get("results"):
            props = rows["results"][0].get("properties", {})
            brand["voice"]       = _rt(props.get("Voice & Tone", {}))
            brand["power_words"] = _rt(props.get("Power Words", {}))
            brand["avoid_words"] = _rt(props.get("Words to Avoid", {}))
            brand["tone_desc"]   = _rt(props.get("Tone of Voice", {}))

    ci_db = cfg.get("client_info_db_id", "")
    if ci_db:
        rows = await notion._client.request(
            path=f"databases/{ci_db}/query", method="POST", body={"page_size": 1}
        )
        if rows.get("results"):
            props = rows["results"][0].get("properties", {})
            brand["business_name"] = (
                _title_text(props.get("Company", {})) or
                _title_text(props.get("Client Name", {})) or
                _rt(props.get("Company", {})) or
                cfg.get("name", "")
            )
            brand["website_url"] = (
                _url(props.get("Website", {})) or
                _url(props.get("Current Website URL", {})) or
                _rt(props.get("Website", {})) or
                _rt(props.get("Current Website URL", {})) or ""
            )

    brand.setdefault("business_name", cfg.get("name", ""))
    brand.setdefault("website_url", "")
    return brand


async def _load_upcoming_blog_posts(notion: NotionClient, cfg: dict) -> list[dict]:
    """Load Approved/Draft blog posts — these are the upcoming content to tie posts to."""
    db_id = cfg.get("blog_posts_db_id", "")
    if not db_id:
        return []

    rows = await notion._client.request(
        path=f"databases/{db_id}/query",
        method="POST",
        body={
            "page_size": 20,
            "filter": {
                "or": [
                    {"property": "Status", "select": {"equals": "Approved"}},
                    {"property": "Status", "select": {"equals": "Draft"}},
                    {"property": "Status", "select": {"equals": "Scheduled"}},
                ]
            },
        },
    )

    posts = []
    for row in rows.get("results", []):
        props   = row.get("properties", {})
        title   = _title_text(props.get("Title", {}))
        keyword = _rt(props.get("Target Keyword", {}))
        intent  = _rt(props.get("Search Intent", {}))
        month   = _select(props.get("Publish Month", {}))
        if title:
            posts.append({
                "title":   title,
                "keyword": keyword,
                "intent":  intent,
                "month":   month,
            })
    return posts[:6]  # cap at 6 — enough to pick 2-3 blog tie-in posts


async def _load_content_pages(notion: NotionClient, cfg: dict) -> list[dict]:
    """Load service/about pages from the Content DB for non-blog-tie-in posts."""
    content_db = cfg.get("content_db_id", "")
    if not content_db:
        return []

    rows = await notion._client.request(
        path=f"databases/{content_db}/query",
        method="POST",
        body={"page_size": 100},
    )

    skip_keywords = ["privacy", "terms", "accessibility", "404", "thank you",
                     "blog", "contact", "insurance", "billing", "new patient",
                     "resources", "home"]

    pages = []
    for row in rows.get("results", []):
        props = row.get("properties", {})
        name  = _title_text(props.get("Page Title", {})) or _title_text(props.get("Page Name", {}))
        h1    = _rt(props.get("H1", {}))
        kw    = _rt(props.get("Primary Keyword", {}))
        meta  = _rt(props.get("Meta Description", {}))

        if not name:
            continue
        lower = name.lower()
        if any(s in lower for s in skip_keywords):
            continue

        try:
            block_resp = await notion._client.request(
                path=f"blocks/{row['id']}/children", method="GET"
            )
            body = _blocks_to_text(block_resp.get("results", []))[:1000]
        except Exception:
            body = ""

        if not body and not h1:
            continue

        pages.append({"page_name": name, "h1": h1, "keyword": kw, "meta": meta, "body": body})

    # Prioritize service and condition pages
    priority_kws = ["speech", "occupational", "physical", "therapy", "children",
                    "adults", "autism", "sensory", "feeding", "language", "motor"]
    pages.sort(
        key=lambda p: sum(1 for kw in priority_kws if kw in p["page_name"].lower()),
        reverse=True,
    )
    return pages[:10]


# ── Post generation ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a social media content writer for healthcare and wellness practices on Instagram and Facebook.

Your posts educate, connect, and build trust — they are NOT ads. People are scrolling; you have 1-2 seconds.

WRITING RULES (no exceptions):
- No em dashes (—). Use commas or periods instead.
- No AI opener phrases: "In today's world," "It's worth noting," "Let's dive in," "As a parent,"
- No filler: "comprehensive," "holistic," "robust," "foster," "leverage," "navigate"
- Lead with something real — a question, a moment, an observation — never a definition or label
- Max 3 sentences per paragraph
- Write in the practice's voice (warm, approachable, clinical when needed — never corporate)
- Captions: 100–200 words. Punchy. Scannable. The first line must be the hook.
- Always end with a soft CTA or question to invite engagement ("Have questions? DM us.")
- Hashtags: 5–8. Mix of broad (#speechtherapy), specific (#pediatricfeedingtherapy), local (#FriscoTX)
- Image notes: describe the visual in 1 sentence so the team knows what to pull/create
"""

def _build_prompt(
    brand: dict,
    blog_posts: list[dict],
    content_pages: list[dict],
    month: str,
    notes: str,
    post_count: int = 8,
) -> str:
    business = brand.get("business_name", "the practice")
    voice    = brand.get("voice", "") or brand.get("tone_desc", "")
    power    = brand.get("power_words", "")
    avoid    = brand.get("avoid_words", "")

    brand_block = f"Business: {business}\n"
    if voice:  brand_block += f"Voice: {voice}\n"
    if power:  brand_block += f"Power words: {power}\n"
    if avoid:  brand_block += f"Words to avoid: {avoid}\n"

    blog_block = ""
    if blog_posts:
        blog_lines = "\n".join(
            f"  - \"{p['title']}\" (keyword: {p['keyword']}, {p['month']})"
            for p in blog_posts
        )
        blog_block = f"\nUPCOMING BLOG POSTS (use 2-3 of these for Blog Tie-In posts):\n{blog_lines}\n"

    page_block = ""
    if content_pages:
        page_lines = []
        for p in content_pages[:6]:
            page_lines.append(
                f"  PAGE: {p['page_name']}\n"
                f"  H1: {p['h1']}\n"
                f"  Keyword: {p['keyword']}\n"
                f"  Excerpt: {p['body'][:400]}"
            )
        page_block = "\nWEBSITE PAGES (use for Educational and Service posts):\n" + "\n\n".join(page_lines) + "\n"

    notes_block = f"\nRevision notes from team: {notes}\n" if notes else ""

    # Build proportional distribution based on post_count
    blog_count      = max(1, round(post_count * 0.25))
    edu_count       = max(1, round(post_count * 0.25))
    service_count   = max(1, round(post_count * 0.25))
    seasonal_count  = max(1, post_count - blog_count - edu_count - service_count)
    dist_block = (
        f"POST TYPE DISTRIBUTION (total {post_count} posts):\n"
        f"- {blog_count} Blog Tie-In: tease upcoming blog posts, drive curiosity\n"
        f"- {edu_count} Educational: teach one useful thing from a service page (tip, myth-bust, FAQ)\n"
        f"- {service_count} Service: highlight a specific service and who it helps, with a soft CTA\n"
        f"- {seasonal_count} Seasonal/Community: tie to {month} (awareness month, local school calendar, season)\n"
        "Mix the types throughout — don't group all of one type together."
    )

    return f"""\
Generate {post_count} Instagram/Facebook social post drafts for {month}.

{brand_block}{notes_block}
{dist_block}

{blog_block}{page_block}

Return ONLY a JSON array — no markdown, no preamble:
[
  {{
    "post_number": 1,
    "post_type": "Blog Tie-In",
    "title": "short internal title (not published)",
    "caption": "full Instagram/Facebook caption",
    "hashtags": "#tag1 #tag2 #tag3 #tag4 #tag5",
    "image_notes": "1-sentence description of what image to use",
    "source": "blog post title or page name this is based on"
  }},
  ...
]
"""


async def _write_posts(
    notion: NotionClient,
    db_id: str,
    posts: list[dict],
    month: str,
) -> None:
    for post in posts:
        caption    = post.get("caption", "")
        hashtags   = post.get("hashtags", "")
        char_count = len(caption)

        await notion._client.request(
            path="pages",
            method="POST",
            body={
                "parent":     {"database_id": db_id},
                "properties": {
                    "Post Title": {
                        "title": [{"text": {"content": post.get("title", f"Post {post.get('post_number', '')}")}}]
                    },
                    "Caption": {
                        "rich_text": [{"text": {"content": caption}}]
                    },
                    "Platform": {
                        "select": {"name": "Instagram + Facebook"}
                    },
                    "Post Type": {
                        "select": {"name": post.get("post_type", "Educational")}
                    },
                    "Status": {
                        "select": {"name": "Draft"}
                    },
                    "Month": {
                        "rich_text": [{"text": {"content": month}}]
                    },
                    "Source": {
                        "rich_text": [{"text": {"content": post.get("source", "")}}]
                    },
                    "Image Notes": {
                        "rich_text": [{"text": {"content": post.get("image_notes", "")}}]
                    },
                    "Hashtags": {
                        "rich_text": [{"text": {"content": hashtags}}]
                    },
                    "Char Count": {
                        "number": char_count
                    },
                },
            },
        )
        print(f"  ✓ Post {post.get('post_number')} [{post.get('post_type')}] — {post.get('source', '')} ({char_count} chars)")


# ── Main ───────────────────────────────────────────────────────────────────────

async def run(client_key: str, month: str, notes: str) -> None:
    cfg = CLIENTS.get(client_key)
    if not cfg:
        print(f"Client '{client_key}' not found in config.")
        sys.exit(1)

    notion      = NotionClient(api_key=settings.notion_api_key)
    client_name = cfg.get("name", client_key)

    print(f"\n{'='*60}")
    print(f"  Social Posts Generator — {client_name} — {month}")
    print(f"{'='*60}\n")

    print("Checking Social Posts DB...")
    db_id = await _ensure_social_posts_db(notion, client_key, cfg)

    print("Loading brand guidelines...")
    brand = await _load_brand(notion, cfg)
    print(f"  Business: {brand['business_name']}")

    print("Loading upcoming blog posts...")
    blog_posts = await _load_upcoming_blog_posts(notion, cfg)
    print(f"  Found {len(blog_posts)} upcoming blog posts")

    print("Loading content pages...")
    content_pages = await _load_content_pages(notion, cfg)
    print(f"  Found {len(content_pages)} content pages")

    if not blog_posts and not content_pages:
        print("\n⚠ No blog posts or content pages found.")
        print("  Run 'make content' to generate page copy, and 'make blog-ideas' to create blog posts.")
        sys.exit(1)

    services      = cfg.get("services", {})
    social_count  = int(services.get("social_posts_per_month", 8) or 8)
    social_count  = max(1, min(20, social_count))  # clamp 1–20

    print(f"\nGenerating {social_count} social posts with Claude...")
    if notes:
        print(f"  Notes: {notes}")

    ai_client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    prompt    = _build_prompt(brand, blog_posts, content_pages, month, notes, post_count=social_count)

    response = ai_client.messages.create(
        model=settings.anthropic_model,
        max_tokens=6000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    raw   = response.content[0].text.strip()
    match = re.search(r'\[.*\]', raw, re.DOTALL)
    if not match:
        print("\n⚠ Could not parse Claude response as JSON array.")
        print(raw[:500])
        sys.exit(1)

    posts = json.loads(match.group(0))
    print(f"  Generated {len(posts)} posts\n")

    print("Writing to Notion Social Posts DB...")
    await _write_posts(notion, db_id, posts, month)

    print(f"\n✓ Done. {len(posts)} of {social_count} Instagram/Facebook drafts in Notion → Social Posts DB.")
    print("  Next: team attaches creatives → flip Status to Scheduled.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Instagram/Facebook post drafts")
    parser.add_argument("--client", required=True)
    parser.add_argument("--month",  default=date.today().strftime("%B %Y"))
    parser.add_argument("--notes",  default="")
    args = parser.parse_args()
    asyncio.run(run(args.client, args.month, args.notes))


if __name__ == "__main__":
    main()
