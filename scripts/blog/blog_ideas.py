#!/usr/bin/env python3
"""
blog_ideas.py — Generate 20 blog ideas for the next 3 months

Reads the Blog Voice & Author Setup page, synthesizes a Style Brief if needed,
then generates 20 blog ideas grounded in validated keywords, approved sitemap pages,
and (optionally) published posts from sister clients in the same vertical.

Ideas land in the Blog Posts DB with Status = "Idea" for team review.
Team flips Status to "Approved" → run `make blog-write` to generate posts.

Gate: Refuses to run if blog_voice_setup_page_id is missing or sections 1/4/5/7
are still showing the default placeholder text.

Usage:
    make blog-ideas CLIENT=summit_therapy
    make blog-ideas CLIENT=summit_therapy FORCE=1   # regenerate even if ideas exist
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import anthropic

from config.clients import CLIENTS
from src.config import settings
from src.integrations.notion import NotionClient

CLIENTS_JSON_PATH = Path(__file__).parent.parent / "config" / "clients.json"

# ── Notion helpers ─────────────────────────────────────────────────────────────

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

def _blocks_to_sections(blocks: list[dict]) -> dict[str, str]:
    """
    Parse setup page blocks into a dict keyed by section number.
    Sections are H2 blocks starting with "1.", "2.", etc.
    Content is the paragraph text following each heading.
    """
    sections: dict[str, str] = {}
    current_key: str | None = None
    buffer: list[str] = []

    for block in blocks:
        btype = block.get("type", "")

        if btype == "heading_2":
            # Save previous section
            if current_key:
                sections[current_key] = "\n".join(buffer).strip()
            # Start new section
            heading_text = "".join(
                p.get("text", {}).get("content", "")
                for p in block.get("heading_2", {}).get("rich_text", [])
            )
            # Extract section number from "1. Author Name..." → "1"
            m = re.match(r"^(\d+)\.", heading_text.strip())
            current_key = m.group(1) if m else heading_text
            buffer = []

        elif btype in ("paragraph", "callout") and current_key:
            content_key = btype
            rich = block.get(content_key, {}).get("rich_text", [])
            text = "".join(p.get("text", {}).get("content", "") for p in rich)
            # Skip callout instruction blocks (they contain the prompt text, not team input)
            if btype == "callout":
                continue
            if text.strip():
                buffer.append(text.strip())

    # Save last section
    if current_key:
        sections[current_key] = "\n".join(buffer).strip()

    return sections


def _is_filled(text: str) -> bool:
    """Check if a section has real content (not just placeholder or empty)."""
    if not text:
        return False
    placeholder_signals = [
        "sample 1", "sample 2", "sample 3",
        "what i like about it",
        "leave this blank",
        "claude fills",
    ]
    lower = text.lower()
    if all(s in lower for s in ["sample 1", "what i like"]):
        return False  # still showing template
    return len(text.strip()) > 10


def _save_to_json(client_key: str, field: str, value: str) -> None:
    try:
        data = json.loads(CLIENTS_JSON_PATH.read_text()) if CLIENTS_JSON_PATH.exists() else {}
        if client_key not in data:
            data[client_key] = {}
        data[client_key][field] = value
        CLIENTS_JSON_PATH.write_text(json.dumps(data, indent=2))
    except Exception as e:
        print(f"  ⚠ Could not save {field} to clients.json: {e}")


# ── Blog Posts DB ──────────────────────────────────────────────────────────────

BLOG_POSTS_SCHEMA = {
    "Title":                    {"title": {}},
    "Status": {
        "select": {
            "options": [
                {"name": "Idea",         "color": "gray"},
                {"name": "Approved",     "color": "green"},
                {"name": "Draft",        "color": "blue"},
                {"name": "Under Review", "color": "yellow"},
                {"name": "Image Needed", "color": "orange"},
                {"name": "Scheduled",    "color": "purple"},
                {"name": "Published",    "color": "pink"},
            ]
        }
    },
    "Target Keyword":           {"rich_text": {}},
    "Search Intent":            {"rich_text": {}},
    "Internal Link Target":     {"rich_text": {}},
    "Publish Month": {
        "select": {
            "options": [
                {"name": "Month 1", "color": "blue"},
                {"name": "Month 2", "color": "green"},
                {"name": "Month 3", "color": "yellow"},
            ]
        }
    },
    "Suggested Publish Date":   {"date": {}},
    "Author Name":              {"rich_text": {}},
    "Reviewer Name":            {"rich_text": {}},
    "Reviewer Credentials":     {"rich_text": {}},
    "Review Date":              {"date": {}},
    "Published URL":            {"url": {}},
    "Cross-Client Link Suggestion": {"rich_text": {}},
    "Word Count":               {"number": {}},
    "Title Tag":                {"rich_text": {}},
    "Meta Description":         {"rich_text": {}},
    "H1":                       {"rich_text": {}},
    "Primary Keyword":          {"rich_text": {}},
    "Feedback":                 {"rich_text": {}},
}


async def _ensure_blog_posts_db(notion: NotionClient, client_key: str, cfg: dict) -> str:
    db_id = cfg.get("blog_posts_db_id", "")
    if db_id:
        return db_id

    client_info_db = cfg.get("client_info_db_id", "")
    if not client_info_db:
        raise ValueError(f"No client_info_db_id for {client_key}")

    db_meta = await notion._client.request(path=f"databases/{client_info_db}", method="GET")
    parent_page_id = db_meta.get("parent", {}).get("page_id", "")
    if not parent_page_id:
        raise ValueError("Cannot determine client root page for Blog Posts DB")

    result = await notion._client.request(
        path="databases",
        method="POST",
        body={
            "parent":     {"type": "page_id", "page_id": parent_page_id},
            "title":      [{"type": "text", "text": {"content": "Blog Posts"}}],
            "properties": BLOG_POSTS_SCHEMA,
        },
    )
    new_db_id = result["id"]
    print(f"  ✓ Blog Posts DB created: {new_db_id}")
    _save_to_json(client_key, "blog_posts_db_id", new_db_id)
    return new_db_id


# ── Data loaders ───────────────────────────────────────────────────────────────

async def _read_setup_page(notion: NotionClient, page_id: str) -> dict[str, str]:
    """Read and parse the Blog Voice & Author Setup page into sections."""
    resp = await notion._client.request(path=f"blocks/{page_id}/children", method="GET")
    blocks = resp.get("results", [])
    return _blocks_to_sections(blocks)


async def _load_keywords(notion: NotionClient, cfg: dict) -> list[dict]:
    """Load informational/blog-intent keywords with High or Medium priority."""
    db_id = cfg.get("keywords_db_id", "")
    if not db_id:
        return []

    rows = await notion._client.request(
        path=f"databases/{db_id}/query",
        method="POST",
        body={"page_size": 100},
    )

    keywords = []
    for row in rows.get("results", []):
        props   = row.get("properties", {})
        keyword = _title_text(props.get("Keyword", {}))
        intent  = _select(props.get("Intent", {}))
        ktype   = _select(props.get("Type", {}))
        priority = _select(props.get("Priority", {}))
        volume  = _rt(props.get("Monthly Search Volume", {}))
        cluster = _rt(props.get("Cluster", {}))

        if not keyword:
            continue
        if priority not in ("High", "Medium"):
            continue
        # Include if intent=Informational OR type=Blog
        if intent not in ("Informational",) and ktype not in ("Blog",):
            continue

        keywords.append({
            "keyword": keyword,
            "intent":  intent,
            "type":    ktype,
            "priority": priority,
            "volume":  volume,
            "cluster": cluster,
        })

    return keywords


async def _load_sitemap_pages(notion: NotionClient, cfg: dict) -> list[dict]:
    """Load approved sitemap pages as internal link targets."""
    db_id = cfg.get("sitemap_db_id", "")
    if not db_id:
        return []

    rows = await notion._client.request(
        path=f"databases/{db_id}/query",
        method="POST",
        body={"page_size": 100},
    )

    skip = {"privacy", "terms", "accessibility", "404", "thank you"}
    pages = []
    for row in rows.get("results", []):
        props  = row.get("properties", {})
        title  = _title_text(props.get("Page Title", {}))
        status = _select(props.get("Status", {}))
        slug   = _rt(props.get("Slug", {}))
        purpose = _rt(props.get("Purpose", {}))

        if not title or status != "Approved":
            continue
        if any(s in title.lower() for s in skip):
            continue

        pages.append({"title": title, "slug": slug, "purpose": purpose})

    return pages


async def _load_existing_ideas(notion: NotionClient, blog_posts_db_id: str) -> list[str]:
    """Return list of existing blog post titles (to avoid duplicates)."""
    rows = await notion._client.request(
        path=f"databases/{blog_posts_db_id}/query",
        method="POST",
        body={"page_size": 100},
    )
    titles = []
    for row in rows.get("results", []):
        title = _title_text(row.get("properties", {}).get("Title", {}))
        if title:
            titles.append(title)
    return titles


async def _load_cross_client_posts(notion: NotionClient, client_key: str, cfg: dict) -> list[dict]:
    """
    Find published blog posts from sister clients in the same vertical.
    Returns list of {client_name, title, url} for Claude to flag as suggestions.
    """
    vertical = cfg.get("vertical", "")
    if not vertical:
        return []

    suggestions = []
    for key, other_cfg in CLIENTS.items():
        if key == client_key:
            continue
        if other_cfg.get("vertical", "") != vertical:
            continue
        other_db = other_cfg.get("blog_posts_db_id", "")
        if not other_db:
            continue

        try:
            rows = await notion._client.request(
                path=f"databases/{other_db}/query",
                method="POST",
                body={
                    "filter": {"property": "Status", "select": {"equals": "Published"}},
                    "page_size": 20,
                },
            )
            for row in rows.get("results", []):
                props = row.get("properties", {})
                title = _title_text(props.get("Title", {}))
                url   = props.get("Published URL", {}).get("url", "") or ""
                if title and url:
                    suggestions.append({
                        "client_name": other_cfg.get("name", key),
                        "title": title,
                        "url": url,
                    })
        except Exception:
            pass

    return suggestions


async def _synthesize_style_brief(
    client: anthropic.Anthropic,
    sections: dict[str, str],
    client_name: str,
) -> str:
    """Use Claude to synthesize sections 1–8 into a style brief paragraph."""
    prompt = f"""You are synthesizing blog writing style notes for {client_name}.

Based on the following inputs from the team and client, write a clear, specific Style Brief paragraph.
The Style Brief will be injected into every blog post prompt — it must be concrete enough to
actually shape Claude's writing, not just describe the desired outcome in vague terms.

Focus on: sentence rhythm, what gets led with, emotional register, relationship to clinical language,
how the reader is addressed, what never appears. Ground it in the actual samples and inputs provided.

AUTHOR: {sections.get("1", "(not provided)")}

WRITING SAMPLES THEY ADMIRE:
{sections.get("3", "(not provided)")}

VOICE IN 5 WORDS: {sections.get("4", "(not provided)")}

PRIMARY AUDIENCE: {sections.get("5", "(not provided)")}

WHAT THEY REFUSE TO SOUND LIKE: {sections.get("6", "(not provided)")}

PASSION TOPICS: {sections.get("7", "(not provided)")}

STANCE ON CLINICAL LANGUAGE: {sections.get("8", "(not provided)")}

Write 2–3 paragraphs. Be specific and concrete. Don't say "warm and approachable" — show what that
means structurally. Include specific things Claude should and should not do.
Return only the Style Brief text — no preamble, no labels."""

    response = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


async def _save_style_brief(notion: NotionClient, page_id: str, brief: str) -> None:
    """Append the synthesized Style Brief to section 9 of the setup page."""
    # Append a new paragraph block with the style brief text
    await notion._client.request(
        path=f"blocks/{page_id}/children",
        method="PATCH",
        body={
            "children": [
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": brief[:2000]}}]
                    },
                }
            ]
        },
    )


def _build_ideas_prompt(
    client_name: str,
    style_brief: str,
    keywords: list[dict],
    sitemap_pages: list[dict],
    existing_titles: list[str],
    cross_client: list[dict],
    vertical: str,
    idea_count: int = 20,
) -> str:
    # Build the 3-month date windows
    today = date.today()
    month1_start = today.replace(day=1)
    month2_start = (month1_start + timedelta(days=32)).replace(day=1)
    month3_start = (month2_start + timedelta(days=32)).replace(day=1)

    keywords_block = "\n".join(
        f"  - {k['keyword']} (Priority: {k['priority']}, Volume: {k['volume'] or 'unknown'}, Cluster: {k['cluster'] or 'n/a'})"
        for k in keywords[:40]
    ) or "  (no validated keywords loaded — use broad informational topics for this vertical)"

    sitemap_block = "\n".join(
        f"  - {p['title']} (/{p['slug']})"
        for p in sitemap_pages[:30]
    ) or "  (no approved sitemap pages found)"

    existing_block = "\n".join(f"  - {t}" for t in existing_titles[:20]) or "  (none yet)"

    cross_block = ""
    if cross_client:
        lines = "\n".join(
            f"  - [{c['client_name']}] {c['title']} — {c['url']}"
            for c in cross_client[:10]
        )
        cross_block = f"""
PUBLISHED POSTS FROM SISTER CLIENTS IN THE SAME VERTICAL ({vertical}):
(These are potential cross-linking opportunities — flag them in Cross-Client Link Suggestion only, never auto-include)
{lines}
"""

    return f"""Generate {idea_count} blog post ideas for {client_name} — spread across 3 months.

STYLE BRIEF (defines the writing voice for all posts):
{style_brief}

VALIDATED KEYWORDS (informational intent — these are the best blog targets):
{keywords_block}

APPROVED SITEMAP PAGES (use these as internal link targets — every post must link to one):
{sitemap_block}

EXISTING BLOG POST TITLES (avoid duplicates):
{existing_block}
{cross_block}
PLANNING WINDOW:
- Month 1: {month1_start.strftime("%B %Y")}
- Month 2: {month2_start.strftime("%B %Y")}
- Month 3: {month3_start.strftime("%B %Y")}

RULES:
1. Each idea must target a real informational search query, not a brand/navigational one
2. Every post must have a clear internal link target (one of the approved sitemap pages)
3. Spread ideas across Month 1 / Month 2 / Month 3 (~7 per month)
4. Vary the format: how-to, myth-bust, explainer, personal story angle, comparison, FAQ
5. Prioritize keywords from the validated list — these have real search volume
6. Do not duplicate existing titles
7. If a sister client post is relevant, note the URL in cross_client_link_suggestion — never include it automatically
8. Title should be the actual published title (reader-facing), not a brief description

Return ONLY a JSON array — no markdown, no preamble:
[
  {{
    "title": "The actual post title (reader-facing, ~8–12 words)",
    "target_keyword": "the primary keyword this post targets",
    "search_intent": "what the reader is actually asking / what brought them to Google",
    "internal_link_target": "Page Title from sitemap (e.g. 'Speech Therapy Services')",
    "internal_link_slug": "/slug",
    "publish_month": "Month 1" | "Month 2" | "Month 3",
    "suggested_publish_date": "YYYY-MM-DD",
    "format": "How-to" | "Myth-bust" | "Explainer" | "Story angle" | "Comparison" | "FAQ",
    "angle": "1–2 sentences on the specific angle or hook that makes this post stand out",
    "cross_client_link_suggestion": "URL from sister client if relevant, else empty string"
  }},
  ...
]
"""


SYSTEM_PROMPT = """\
You are a senior SEO content strategist for a healthcare digital marketing agency.
You generate blog ideas that are strategically grounded in validated keyword data and
designed to rank for real informational queries that potential patients are searching.

Your ideas are not generic. They are specific, search-intent-matched, and linked to the
client's existing service pages. Every idea you generate has a clear reason to exist:
it targets a query the service pages don't cover, supports a specific service page via
internal link, and fits the client's writing voice.

You do not generate fluffy, vague, or traffic-bait ideas. You generate content that
a credentialed clinician would be proud to publish under their name.
"""


async def run(client_key: str, force: bool = False) -> None:
    cfg = CLIENTS.get(client_key)
    if not cfg:
        print(f"Client '{client_key}' not found in config.")
        sys.exit(1)

    notion = NotionClient(api_key=settings.notion_api_key)
    client_name = cfg.get("name", client_key)

    print(f"\n{'='*60}")
    print(f"  Blog Ideas Generator — {client_name}")
    print(f"{'='*60}\n")

    # Gate: setup page must exist
    setup_page_id = cfg.get("blog_voice_setup_page_id", "")
    if not setup_page_id:
        print("⚠ Blog Voice & Author Setup page not found.")
        print(f"  Run: make blog-setup CLIENT={client_key}")
        print("  Fill out the setup page with the client, then come back.")
        sys.exit(1)

    # Read setup page
    print("Reading Blog Voice & Author Setup page...")
    sections = await _read_setup_page(notion, setup_page_id)

    # Gate: check required sections are filled
    required = {"1": "Author Name & Credentials", "4": "Voice in 5 Words", "5": "Primary Audience", "7": "Passion Topics"}
    missing = [label for num, label in required.items() if not _is_filled(sections.get(num, ""))]
    if missing:
        print(f"\n⚠ Setup page incomplete. Fill in these sections before running blog-ideas:")
        for label in missing:
            print(f"  • {label}")
        print(f"\n  Open: https://notion.so/{setup_page_id.replace('-', '')}")
        sys.exit(1)

    print("  ✓ Setup page looks complete")

    # Synthesize style brief if section 9 is empty
    style_brief = sections.get("9", "").strip()
    ai_client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    if not style_brief or len(style_brief) < 50:
        print("\nSynthesizing Style Brief from setup page (section 9 is blank)...")
        style_brief = await _synthesize_style_brief(ai_client, sections, client_name)
        print("  ✓ Style Brief generated:")
        print(f"\n  {style_brief[:300]}...\n" if len(style_brief) > 300 else f"\n  {style_brief}\n")
        await _save_style_brief(notion, setup_page_id, style_brief)
        print("  ✓ Style Brief saved to setup page (section 9)")
        print("  Review it in Notion and refine if needed before running blog-write.")
    else:
        print("  ✓ Using existing Style Brief from setup page")

    # Ensure Blog Posts DB exists
    print("\nChecking Blog Posts DB...")
    blog_posts_db_id = await _ensure_blog_posts_db(notion, client_key, cfg)
    # Reload cfg after potential clients.json update
    from config.clients import CLIENTS as CLIENTS_FRESH
    cfg = CLIENTS_FRESH.get(client_key) or cfg

    # Load data
    print("\nLoading keyword research...")
    keywords = await _load_keywords(notion, cfg)
    print(f"  {len(keywords)} informational/blog keywords found")

    print("Loading approved sitemap pages...")
    sitemap_pages = await _load_sitemap_pages(notion, cfg)
    print(f"  {len(sitemap_pages)} approved pages found (internal link targets)")

    print("Loading existing blog posts (dedupe)...")
    existing_titles = await _load_existing_ideas(notion, blog_posts_db_id) if not force else []
    print(f"  {len(existing_titles)} existing posts found")

    print("Checking sister clients for cross-link opportunities...")
    vertical = cfg.get("vertical", "")
    cross_client = await _load_cross_client_posts(notion, client_key, cfg)
    if cross_client:
        print(f"  {len(cross_client)} potential cross-client links found ({vertical})")
    else:
        print("  (no sister clients in same vertical, or none with published posts)")

    # Determine idea count from services config (3 months × posts/month, capped at 36)
    services = cfg.get("services", {})
    posts_per_month = 4  # default
    if isinstance(services, dict):
        posts_per_month = int(services.get("blog_posts_per_month", 4) or 4)
    posts_per_month = max(1, min(12, posts_per_month))  # clamp 1–12
    idea_count = min(posts_per_month * 3, 36)  # 3-month batch, max 36

    # Generate ideas
    print(f"\nGenerating {idea_count} blog ideas ({posts_per_month}/month × 3 months) with Claude...")
    prompt = _build_ideas_prompt(
        client_name, style_brief, keywords, sitemap_pages,
        existing_titles, cross_client, vertical,
        idea_count=idea_count,
    )

    response = ai_client.messages.create(
        model=settings.anthropic_model,
        max_tokens=6000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    match = re.search(r'\[.*\]', raw, re.DOTALL)
    if not match:
        print("\n⚠ Could not parse Claude response as JSON array.")
        print(raw[:500])
        sys.exit(1)

    ideas = json.loads(match.group(0))
    print(f"  ✓ {len(ideas)} ideas generated\n")

    # Load reviewer info from Brand Guidelines (if available)
    reviewer_name = ""
    reviewer_creds = ""
    bg_db = cfg.get("brand_guidelines_db_id", "")
    if bg_db:
        try:
            bg_rows = await notion._client.request(
                path=f"databases/{bg_db}/query", method="POST", body={"page_size": 1}
            )
            if bg_rows.get("results"):
                props = bg_rows["results"][0].get("properties", {})
                reviewer_name  = _rt(props.get("Blog Reviewer Name", {}))
                reviewer_creds = _rt(props.get("Blog Reviewer Credentials", {}))
        except Exception:
            pass

    # Write ideas to Notion
    print("Writing ideas to Notion Blog Posts DB...")
    for idea in ideas:
        title = idea.get("title", "")
        if not title:
            continue

        publish_date = idea.get("suggested_publish_date", "")
        date_prop = {"date": {"start": publish_date}} if publish_date else {"date": None}

        cross_link = idea.get("cross_client_link_suggestion", "")

        properties: dict = {
            "Title": {"title": [{"text": {"content": title[:500]}}]},
            "Status": {"select": {"name": "Idea"}},
            "Target Keyword": {"rich_text": [{"text": {"content": idea.get("target_keyword", "")[:500]}}]},
            "Search Intent": {"rich_text": [{"text": {"content": idea.get("search_intent", "")[:500]}}]},
            "Internal Link Target": {"rich_text": [{"text": {"content": idea.get("internal_link_target", "")[:500]}}]},
            "Publish Month": {"select": {"name": idea.get("publish_month", "Month 1")}},
            "Suggested Publish Date": date_prop,
            "Primary Keyword": {"rich_text": [{"text": {"content": idea.get("target_keyword", "")[:500]}}]},
            "Feedback": {"rich_text": [{"text": {"content": f"Format: {idea.get('format', '')}\nAngle: {idea.get('angle', '')}"[:2000]}}]},
        }

        if cross_link:
            properties["Cross-Client Link Suggestion"] = {
                "rich_text": [{"text": {"content": f"⚠️ TEAM REVIEW REQUIRED before including:\n{cross_link}"[:2000]}}]
            }
        if reviewer_name:
            properties["Reviewer Name"] = {"rich_text": [{"text": {"content": reviewer_name}}]}
        if reviewer_creds:
            properties["Reviewer Credentials"] = {"rich_text": [{"text": {"content": reviewer_creds}}]}

        await notion._client.request(
            path="pages",
            method="POST",
            body={"parent": {"database_id": blog_posts_db_id}, "properties": properties},
        )
        month = idea.get("publish_month", "")
        fmt   = idea.get("format", "")
        print(f"  ✓ [{month}] {title} ({fmt})")

    print(f"\n✓ {len(ideas)} ideas added to Blog Posts DB.")
    print("Next steps:")
    print("  1. Review ideas in Notion Blog Posts DB")
    print("  2. Flip Status → 'Approved' for the ones you want written")
    print("  3. Run: make blog-write CLIENT=" + client_key)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate 20 blog ideas → Notion Blog Posts DB")
    parser.add_argument("--client", required=True, help="Client key (e.g. summit_therapy)")
    parser.add_argument("--force",  action="store_true", help="Regenerate even if ideas already exist")
    args = parser.parse_args()
    asyncio.run(run(args.client, force=args.force))


if __name__ == "__main__":
    main()
