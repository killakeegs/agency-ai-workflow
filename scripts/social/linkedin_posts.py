#!/usr/bin/env python3
"""
linkedin_posts.py — Generate LinkedIn post drafts (2/month)

Written in the voice of the practice founder/clinician. Tied to upcoming blog
posts or company milestones. Thought leadership tone — not marketing copy.

Posts land in the same Social Posts DB as Instagram/Facebook, with
Platform = "LinkedIn" so the team can filter by platform in Notion.

Usage:
    make linkedin-posts CLIENT=summit_therapy
    make linkedin-posts CLIENT=summit_therapy NOTES="tie second post to team growth"
    make linkedin-posts CLIENT=summit_therapy MONTH="May 2026"
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
from src.integrations.business_profile import load_business_profile

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


# ── Social Posts DB schema (same as social_posts.py) ──────────────────────────
# LinkedIn posts share the same DB — Platform field distinguishes them.

SOCIAL_POSTS_SCHEMA = {
    "Post Title": {"title": {}},
    "Caption":    {"rich_text": {}},
    "Platform": {
        "select": {
            "options": [
                {"name": "Instagram + Facebook", "color": "blue"},
                {"name": "Instagram",            "color": "pink"},
                {"name": "Facebook",             "color": "blue"},
                {"name": "LinkedIn",             "color": "gray"},
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
                {"name": "Thought Leadership","color": "gray"},
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
    """Return existing Social Posts DB ID, or create one."""
    db_id = cfg.get("social_posts_db_id", "")
    if db_id:
        return db_id

    client_info_db = cfg.get("client_info_db_id", "")
    if not client_info_db:
        raise ValueError(f"No client_info_db_id found for {client_key}")

    db_meta        = await notion._client.request(path=f"databases/{client_info_db}", method="GET")
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
            brand["voice"]          = _rt(props.get("Voice & Tone", {}))
            brand["avoid_words"]    = _rt(props.get("Words to Avoid", {}))
            brand["blog_voice"]     = _rt(props.get("Blog Voice", {}))
            brand["author_name"]    = _rt(props.get("Blog Reviewer Name", {}))

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
                cfg.get("name", "")
            )
            brand["website_url"] = (
                _url(props.get("Website", {})) or
                _url(props.get("Current Website URL", {})) or
                _rt(props.get("Current Website URL", {})) or ""
            )

    brand.setdefault("business_name", cfg.get("name", ""))
    brand.setdefault("website_url", "")
    brand.setdefault("author_name", "")
    brand.setdefault("blog_voice", "")
    return brand


async def _load_upcoming_blog_posts(notion: NotionClient, cfg: dict) -> list[dict]:
    """Load Approved/Draft/Scheduled blog posts as LinkedIn content sources."""
    db_id = cfg.get("blog_posts_db_id", "")
    if not db_id:
        return []

    rows = await notion._client.request(
        path=f"databases/{db_id}/query",
        method="POST",
        body={
            "page_size": 10,
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
        month   = _select(props.get("Publish Month", {}))
        if title:
            posts.append({"title": title, "keyword": keyword, "month": month})
    return posts[:4]


# ── Post generation ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are writing LinkedIn posts for a healthcare practice founder/clinician.

LinkedIn is different from Instagram. The audience is:
- Physicians and pediatricians who refer patients
- School counselors and educators
- Other clinicians and therapists
- Potential hires (therapists looking for jobs)
- Parents who are also professionals doing research

WRITING RULES:
- Write in first person as the clinician/founder (not "we" — "I")
- No em dashes (—). Use commas or periods.
- No AI opener phrases: "In today's world," "It's worth noting," "Let's explore"
- No hashtag stuffing — 2-3 professional hashtags max
- Lead with a professional observation, clinical insight, or honest moment from practice
- One clear point per post — LinkedIn is not a listicle
- 150–250 words. Paragraph form. No bullet points.
- End with a genuine question or invitation to connect
- Can reference the blog post directly with a line like: "Full post at [blog URL]"
- Tone: thoughtful, experienced, direct — like a clinician you'd trust
"""

def _build_prompt(
    brand: dict,
    blog_posts: list[dict],
    month: str,
    notes: str,
    post_count: int = 2,
    business_profile: str = "",
) -> str:
    business    = brand.get("business_name", "the practice")
    author      = brand.get("author_name", "the founder")
    blog_voice  = brand.get("blog_voice", "")
    avoid       = brand.get("avoid_words", "")
    website     = brand.get("website_url", "").rstrip("/")

    brand_block = f"Practice: {business}\nAuthor: {author}\n"
    if blog_voice:  brand_block += f"Author voice: {blog_voice}\n"
    if avoid:       brand_block += f"Words to avoid: {avoid}\n"
    if website:     brand_block += f"Blog URL base: {website}/blog\n"

    profile_block = ""
    if business_profile:
        profile_block = f"""
BUSINESS PROFILE (deep clinical/business knowledge — use for credible
thought-leadership posts; reference actual specialties, populations, and
treatment philosophy. Never contradict):
{business_profile[:10000]}
"""

    blog_block = ""
    if blog_posts:
        blog_lines = "\n".join(
            f"  - \"{p['title']}\" (keyword: {p['keyword']}, {p['month']})"
            for p in blog_posts
        )
        blog_block = f"\nUPCOMING BLOG POSTS — tie each LinkedIn post to one of these:\n{blog_lines}\n"
    else:
        blog_block = "\nNo upcoming blog posts found. Generate two thought leadership posts based on the practice's area of expertise.\n"

    notes_block = f"\nRevision notes from team: {notes}\n" if notes else ""

    # Build post requirements based on count
    post_reqs = []
    for i in range(1, post_count + 1):
        if i == 1:
            post_reqs.append("- Post 1: tied directly to an upcoming blog post — tease the core insight, link to the post")
        else:
            post_reqs.append(f"- Post {i}: original thought leadership — a clinical observation, professional opinion, or honest reflection from practice")
    post_requirements = "\n".join(post_reqs)

    return f"""\
Generate {post_count} LinkedIn post draft{"s" if post_count != 1 else ""} for {month}.

{brand_block}{profile_block}{notes_block}
POST REQUIREMENTS:
{post_requirements}

{blog_block}

Return ONLY a JSON array — no markdown, no preamble:
[
  {{
    "post_number": 1,
    "post_type": "Thought Leadership",
    "title": "short internal title (not published)",
    "caption": "full LinkedIn post text",
    "hashtags": "#tag1 #tag2 #tag3",
    "image_notes": "Optional: 1-sentence image suggestion, or 'text only'",
    "source": "blog post title or topic this is based on"
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
                        "title": [{"text": {"content": post.get("title", f"LinkedIn Post {post.get('post_number', '')}")}}]
                    },
                    "Caption": {
                        "rich_text": [{"text": {"content": caption}}]
                    },
                    "Platform": {
                        "select": {"name": "LinkedIn"}
                    },
                    "Post Type": {
                        "select": {"name": "Thought Leadership"}
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
        print(f"  ✓ LinkedIn Post {post.get('post_number')} — {post.get('source', '')} ({char_count} chars)")


# ── Main ───────────────────────────────────────────────────────────────────────

async def run(client_key: str, month: str, notes: str) -> None:
    cfg = CLIENTS.get(client_key)
    if not cfg:
        print(f"Client '{client_key}' not found in config.")
        sys.exit(1)

    notion      = NotionClient(api_key=settings.notion_api_key)
    client_name = cfg.get("name", client_key)

    print(f"\n{'='*60}")
    print(f"  LinkedIn Post Generator — {client_name} — {month}")
    print(f"{'='*60}\n")

    print("Checking Social Posts DB...")
    db_id = await _ensure_social_posts_db(notion, client_key, cfg)

    print("Loading brand guidelines...")
    brand = await _load_brand(notion, cfg)
    print(f"  Business: {brand['business_name']}")
    print(f"  Author: {brand['author_name'] or '(not set — add Blog Reviewer Name to Brand Guidelines)'}")

    print("Loading upcoming blog posts...")
    blog_posts = await _load_upcoming_blog_posts(notion, cfg)
    print(f"  Found {len(blog_posts)} upcoming blog posts")

    services       = cfg.get("services", {})
    linkedin_count = int(services.get("linkedin_posts_per_month", 2) or 2)
    linkedin_count = max(1, min(8, linkedin_count))  # clamp 1–8

    print(f"\nGenerating {linkedin_count} LinkedIn post{'s' if linkedin_count != 1 else ''} with Claude...")
    if notes:
        print(f"  Notes: {notes}")

    ai_client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    print("Loading Business Profile...")
    business_profile = await load_business_profile(notion, cfg)
    if business_profile:
        print(f"  ✓ {len(business_profile):,} chars loaded")
    else:
        print("  (no Business Profile — continuing without it)")

    prompt = _build_prompt(
        brand, blog_posts, month, notes,
        post_count=linkedin_count,
        business_profile=business_profile,
    )

    response = ai_client.messages.create(
        model=settings.anthropic_model,
        max_tokens=3000,
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

    print(f"\n✓ Done. {len(posts)} of {linkedin_count} LinkedIn drafts in Notion → Social Posts DB.")
    print("  Filter by Platform = LinkedIn to find them.")
    print("  Next: founder reviews, attaches any image/banner → flip Status to Scheduled.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate LinkedIn post drafts")
    parser.add_argument("--client", required=True)
    parser.add_argument("--month",  default=date.today().strftime("%B %Y"))
    parser.add_argument("--notes",  default="")
    args = parser.parse_args()
    asyncio.run(run(args.client, args.month, args.notes))


if __name__ == "__main__":
    main()
