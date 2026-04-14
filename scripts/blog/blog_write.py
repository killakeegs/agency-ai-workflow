#!/usr/bin/env python3
"""
blog_write.py — Write full blog posts for all Approved ideas

Reads Blog Posts DB for ideas with Status = "Approved", then writes a full
post for each one. Posts are grounded in:
  - The client's Blog Voice & Author Setup (style brief, author identity)
  - Brand guidelines (voice, tone, power words, words to avoid)
  - Validated keyword research (target keyword, search intent)
  - Approved sitemap pages (internal link structure)

Each post is written as Notion page blocks (body copy) + property fields
(title tag, meta description, H1, word count). Status moves to "Draft".

Usage:
    make blog-write CLIENT=summit_therapy
    make blog-write CLIENT=summit_therapy NOTES="make tone warmer, fewer clinical terms"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import anthropic

from config.clients import CLIENTS
from src.config import settings
from src.integrations.notion import NotionClient

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

def _date_val(prop: dict) -> str:
    if not prop:
        return ""
    d = prop.get("date")
    return d.get("start", "") if d else ""


def _blocks_to_text(blocks: list[dict]) -> str:
    """Extract plain text from Notion page blocks."""
    parts = []
    for block in blocks:
        btype = block.get("type", "")
        content = block.get(btype, {})
        rich = content.get("rich_text", [])
        text = "".join(r.get("text", {}).get("content", "") for r in rich)
        if text.strip():
            parts.append(text.strip())
    return "\n".join(parts)


def _text_to_notion_blocks(text: str) -> list[dict]:
    """
    Convert a plain-text post (with markdown-ish headings) to Notion blocks.
    ## Heading → heading_2
    ### Heading → heading_3
    - Bullet → bulleted_list_item
    Everything else → paragraph
    Splits on double newlines for paragraph breaks.
    """
    blocks = []
    paragraphs = re.split(r'\n{2,}', text.strip())

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        # H2
        if para.startswith("## "):
            heading = para[3:].strip()
            blocks.append({
                "object": "block",
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [{"type": "text", "text": {"content": heading[:500]}}]
                },
            })

        # H3
        elif para.startswith("### "):
            heading = para[4:].strip()
            blocks.append({
                "object": "block",
                "type": "heading_3",
                "heading_3": {
                    "rich_text": [{"type": "text", "text": {"content": heading[:500]}}]
                },
            })

        # Bullet list (handle multi-line bullet blocks)
        elif para.startswith("- ") or "\n- " in para:
            for line in para.split("\n"):
                line = line.strip()
                if line.startswith("- "):
                    bullet = line[2:].strip()
                    blocks.append({
                        "object": "block",
                        "type": "bulleted_list_item",
                        "bulleted_list_item": {
                            "rich_text": [{"type": "text", "text": {"content": bullet[:2000]}}]
                        },
                    })
                elif line:
                    blocks.append({
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {
                            "rich_text": [{"type": "text", "text": {"content": line[:2000]}}]
                        },
                    })

        # Paragraph (handle single newlines within a paragraph as soft breaks)
        else:
            # Split on single newlines and create separate paragraphs for readability
            lines = [l.strip() for l in para.split("\n") if l.strip()]
            for line in lines:
                blocks.append({
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": line[:2000]}}]
                    },
                })

    return blocks


# ── Ensure reviewer fields in Brand Guidelines ─────────────────────────────────

async def _ensure_reviewer_fields(notion: NotionClient, bg_db_id: str) -> None:
    """Self-heal: add blog reviewer fields to Brand Guidelines DB if missing."""
    try:
        meta = await notion._client.request(path=f"databases/{bg_db_id}", method="GET")
        existing = set(meta.get("properties", {}).keys())
        to_add = {}
        if "Blog Reviewer Name" not in existing:
            to_add["Blog Reviewer Name"] = {"rich_text": {}}
        if "Blog Reviewer Credentials" not in existing:
            to_add["Blog Reviewer Credentials"] = {"rich_text": {}}
        if "Blog Reviewer Bio" not in existing:
            to_add["Blog Reviewer Bio"] = {"rich_text": {}}
        if "Blog Voice" not in existing:
            to_add["Blog Voice"] = {"rich_text": {}}
        if to_add:
            await notion._client.request(
                path=f"databases/{bg_db_id}",
                method="PATCH",
                body={"properties": to_add},
            )
            print(f"  ✓ Added {', '.join(to_add.keys())} to Brand Guidelines DB")
    except Exception as e:
        print(f"  ⚠ Could not patch Brand Guidelines DB: {e}")


# ── Data loaders ───────────────────────────────────────────────────────────────

async def _load_brand_and_reviewer(notion: NotionClient, cfg: dict) -> dict:
    """Load brand guidelines + reviewer info from Notion."""
    brand: dict = {}

    bg_db = cfg.get("brand_guidelines_db_id", "")
    if bg_db:
        await _ensure_reviewer_fields(notion, bg_db)
        rows = await notion._client.request(
            path=f"databases/{bg_db}/query", method="POST", body={"page_size": 1}
        )
        if rows.get("results"):
            props = rows["results"][0].get("properties", {})
            brand["voice"]               = _rt(props.get("Voice & Tone", {}))
            brand["power_words"]         = _rt(props.get("Power Words", {}))
            brand["avoid_words"]         = _rt(props.get("Words to Avoid", {}))
            brand["cta_style"]           = _rt(props.get("CTA Style", {}))
            brand["blog_voice"]          = _rt(props.get("Blog Voice", {}))
            brand["reviewer_name"]       = _rt(props.get("Blog Reviewer Name", {}))
            brand["reviewer_credentials"]= _rt(props.get("Blog Reviewer Credentials", {}))
            brand["reviewer_bio"]        = _rt(props.get("Blog Reviewer Bio", {}))

    # Business name from client info
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
                props.get("Website", {}).get("url", "") or
                _rt(props.get("Website", {})) or ""
            )

    brand.setdefault("business_name", cfg.get("name", ""))
    brand.setdefault("website_url", "")
    return brand


async def _load_style_brief(notion: NotionClient, setup_page_id: str) -> str:
    """Read section 9 (Style Brief) from the setup page."""
    if not setup_page_id:
        return ""
    try:
        resp = await notion._client.request(path=f"blocks/{setup_page_id}/children", method="GET")
        blocks = resp.get("results", [])
        # Find section 9 — everything after the "9." heading
        in_section_9 = False
        text_parts = []
        for block in blocks:
            btype = block.get("type", "")
            if btype == "heading_2":
                heading = "".join(
                    p.get("text", {}).get("content", "")
                    for p in block.get("heading_2", {}).get("rich_text", [])
                )
                if heading.strip().startswith("9."):
                    in_section_9 = True
                    continue
                elif in_section_9:
                    break  # Next heading ends section 9
            elif in_section_9 and btype == "paragraph":
                text = "".join(
                    p.get("text", {}).get("content", "")
                    for p in block.get("paragraph", {}).get("rich_text", [])
                )
                if text.strip():
                    text_parts.append(text.strip())
        return "\n\n".join(text_parts)
    except Exception:
        return ""


async def _load_approved_ideas(notion: NotionClient, blog_posts_db_id: str) -> list[dict]:
    """Return all ideas with Status = Approved."""
    rows = await notion._client.request(
        path=f"databases/{blog_posts_db_id}/query",
        method="POST",
        body={
            "filter": {"property": "Status", "select": {"equals": "Approved"}},
            "page_size": 50,
        },
    )
    ideas = []
    for row in rows.get("results", []):
        props = row.get("properties", {})
        ideas.append({
            "page_id":            row["id"],
            "title":              _title_text(props.get("Title", {})),
            "target_keyword":     _rt(props.get("Target Keyword", {})),
            "search_intent":      _rt(props.get("Search Intent", {})),
            "internal_link_target": _rt(props.get("Internal Link Target", {})),
            "internal_link_slug": _rt(props.get("Feedback", {})),  # angle/format stored here
            "publish_month":      _select(props.get("Publish Month", {})),
            "reviewer_name":      _rt(props.get("Reviewer Name", {})),
            "reviewer_creds":     _rt(props.get("Reviewer Credentials", {})),
            "cross_client_link":  _rt(props.get("Cross-Client Link Suggestion", {})),
            "feedback":           _rt(props.get("Feedback", {})),
        })
    return ideas


# ── Post writing ───────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a healthcare content writer producing blog posts for a digital marketing agency.

You write in the specific voice of the author as described in the Style Brief.
You are NOT writing for the agency — you are writing AS the clinician, in their voice.

WRITING RESTRICTIONS (apply to every post, no exceptions):
- No em dashes (—). Use commas, periods, or restructure the sentence.
- No AI opener phrases: "In today's world," "It's worth noting," "Let's explore,"
  "In conclusion," "At the end of the day," "It goes without saying"
- No filler words: "delve," "navigate," "comprehensive," "foster," "holistic,"
  "multifaceted," "groundbreaking," "robust," "leverage," "utilize"
- No hedge-stacking: "It's important to note that research suggests that..."
- No passive voice as a default — active voice, specific subjects
- Lead with something real: a question, a specific observation, a moment — not a definition
- Paragraphs: max 3 sentences each. This is a blog, not a journal article.
- No listicle padding: "First and foremost," "Last but not least," "Without further ado"
- One clear opinion or argument per post. Blogs can take a stance. Take it.
- Medical reviewer attribution block at the end — format exactly as instructed.

STRUCTURE:
- Title (already provided — use it verbatim as the H1)
- Introduction: 2–3 short paragraphs, no header
- 3–5 H2 sections with body copy
- Optional H3 sub-points if genuinely needed (not for padding)
- Internal link CTA before or within the final section (natural, not salesy)
- Reviewer attribution block at the very end
- ~900–1,100 words total body copy (not including attribution)

CONTENT QUALITY:
- Every claim must be accurate and specific — no vague generalizations
- If citing research, be specific ("A 2022 study in ASHA journals found..." not "Studies show...")
- If the study isn't certain, don't invent it — make the point without the false citation
- Write to inform, not to convert — the CTA handles conversion
"""


def _build_write_prompt(
    idea: dict,
    brand: dict,
    style_brief: str,
    notes: str,
    website_url: str,
) -> str:
    title       = idea["title"]
    keyword     = idea["target_keyword"]
    intent      = idea["search_intent"]
    link_target = idea["internal_link_target"]
    link_slug   = idea.get("internal_link_slug", "")
    feedback    = idea.get("feedback", "")
    cross_link  = idea.get("cross_client_link", "")

    reviewer    = idea.get("reviewer_name") or brand.get("reviewer_name", "")
    reviewer_creds = idea.get("reviewer_creds") or brand.get("reviewer_credentials", "")
    reviewer_bio = brand.get("reviewer_bio", "")

    business    = brand.get("business_name", "the practice")
    voice       = brand.get("voice", "")
    power       = brand.get("power_words", "")
    avoid       = brand.get("avoid_words", "")

    # Build internal link
    slug = link_slug.strip().lstrip("/") if link_slug else ""
    if slug and website_url:
        internal_url = f"{website_url.rstrip('/')}/{slug}"
    else:
        internal_url = website_url

    # Reviewer block
    if reviewer:
        reviewer_block = f"""
REVIEWER ATTRIBUTION:
At the very end of the post, after all body copy, include this block exactly:
---
Medically reviewed by {reviewer}{', ' + reviewer_creds if reviewer_creds else ''}
{reviewer_bio if reviewer_bio else ''}
---
"""
    else:
        reviewer_block = "\n(No reviewer set — omit the attribution block)\n"

    notes_block = f"\nREVISION NOTES FROM TEAM: {notes}\n" if notes else ""

    cross_block = ""
    if cross_link and "⚠️ TEAM REVIEW REQUIRED" in cross_link:
        cross_block = f"\n(Cross-client link suggestion on file — do NOT include it; team decides separately)\n"

    return f"""Write a full blog post for {business}.

TITLE (use verbatim as H1): {title}

TARGET KEYWORD: {keyword}
(Use naturally throughout — lead paragraph and 2–3 H2 sections. Not stuffed.)

READER'S SEARCH INTENT: {intent}
(This is why they're reading — make sure the post actually answers this.)

INTERNAL LINK TARGET: Link to "{link_target}" at {internal_url}
(Include once, naturally, within the body — not just in the CTA)

CONTEXT / ANGLE: {feedback}

STYLE BRIEF (defines the voice — follow this exactly):
{style_brief if style_brief else "Warm, direct, non-clinical. Speaks to the reader as a fellow human, not a patient."}

BRAND VOICE: {voice}
POWER WORDS: {power}
WORDS TO AVOID: {avoid}
{notes_block}{cross_block}{reviewer_block}
Return ONLY the post body — no JSON, no preamble, no labels.
Use ## for H2 headings, ### for H3 if needed, - for bullet points.
Double newlines between paragraphs.
"""


async def _write_post_to_notion(
    notion: NotionClient,
    page_id: str,
    post_body: str,
    title: str,
    keyword: str,
    reviewer_name: str,
    reviewer_creds: str,
) -> None:
    """Write post body as Notion blocks + update SEO properties."""
    word_count = len(post_body.split())

    # Generate SEO fields from post body (quick extraction)
    # H1 = title (always same as working title)
    h1 = title[:70]

    # Basic title tag from title
    title_tag = title[:60]
    if len(title) > 60:
        title_tag = title[:57] + "..."

    # Meta description: first non-heading paragraph
    first_para = ""
    for line in post_body.split("\n"):
        line = line.strip()
        if line and not line.startswith("#") and not line.startswith("-"):
            first_para = line
            break
    meta_desc = first_para[:155] if first_para else ""

    # Update properties
    await notion._client.request(
        path=f"pages/{page_id}",
        method="PATCH",
        body={
            "properties": {
                "Status":       {"select": {"name": "Draft"}},
                "Word Count":   {"number": word_count},
                "H1":           {"rich_text": [{"text": {"content": h1}}]},
                "Title Tag":    {"rich_text": [{"text": {"content": title_tag}}]},
                "Meta Description": {"rich_text": [{"text": {"content": meta_desc[:2000]}}]},
                "Primary Keyword": {"rich_text": [{"text": {"content": keyword[:500]}}]},
                "Review Date":  {"date": {"start": date.today().isoformat()}},
            }
        },
    )

    # Write body as blocks — Notion allows max 100 blocks per request
    blocks = _text_to_notion_blocks(post_body)
    chunk_size = 90
    for i in range(0, len(blocks), chunk_size):
        chunk = blocks[i:i + chunk_size]
        await notion._client.request(
            path=f"blocks/{page_id}/children",
            method="PATCH",
            body={"children": chunk},
        )


# ── Main ───────────────────────────────────────────────────────────────────────

async def run(client_key: str, notes: str = "") -> None:
    cfg = CLIENTS.get(client_key)
    if not cfg:
        print(f"Client '{client_key}' not found in config.")
        sys.exit(1)

    notion      = NotionClient(api_key=settings.notion_api_key)
    ai_client   = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    client_name = cfg.get("name", client_key)

    print(f"\n{'='*60}")
    print(f"  Blog Writer — {client_name}")
    print(f"{'='*60}\n")

    blog_posts_db_id = cfg.get("blog_posts_db_id", "")
    if not blog_posts_db_id:
        print("⚠ No blog_posts_db_id found. Run `make blog-ideas` first.")
        sys.exit(1)

    # Load brand + reviewer info
    print("Loading brand guidelines and reviewer info...")
    brand = await _load_brand_and_reviewer(notion, cfg)
    print(f"  Business: {brand.get('business_name', '(not set)')}")
    reviewer = brand.get("reviewer_name", "")
    print(f"  Reviewer: {reviewer or '(not set — add Blog Reviewer Name to Brand Guidelines)'}")

    # Load style brief from setup page
    setup_page_id = cfg.get("blog_voice_setup_page_id", "")
    style_brief = await _load_style_brief(notion, setup_page_id)
    if style_brief:
        print(f"  ✓ Style Brief loaded ({len(style_brief)} chars)")
    else:
        print("  ⚠ No Style Brief found — run `make blog-ideas` first to generate it")

    # Load approved ideas
    print("\nLoading approved ideas from Blog Posts DB...")
    ideas = await _load_approved_ideas(notion, blog_posts_db_id)

    if not ideas:
        print("⚠ No approved ideas found. Review the Blog Posts DB and set Status → 'Approved'.")
        sys.exit(0)

    print(f"  {len(ideas)} approved ideas ready to write\n")

    website_url = brand.get("website_url", "").rstrip("/")

    for i, idea in enumerate(ideas, 1):
        title = idea["title"]
        keyword = idea["target_keyword"]
        print(f"[{i}/{len(ideas)}] Writing: {title}")

        prompt = _build_write_prompt(idea, brand, style_brief, notes, website_url)

        response = ai_client.messages.create(
            model=settings.anthropic_model,
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )

        post_body = response.content[0].text.strip()
        word_count = len(post_body.split())
        print(f"  Generated: {word_count} words")

        await _write_post_to_notion(
            notion,
            idea["page_id"],
            post_body,
            title,
            keyword,
            reviewer_name=idea.get("reviewer_name") or brand.get("reviewer_name", ""),
            reviewer_creds=idea.get("reviewer_creds") or brand.get("reviewer_credentials", ""),
        )
        print(f"  ✓ Written to Notion → Status: Draft\n")

    print(f"✓ Done. {len(ideas)} posts written.")
    print("Next steps:")
    print("  1. Review drafts in Blog Posts DB")
    print("  2. Team sets imagery → flip Status to 'Image Needed' → 'Scheduled'")
    print("  3. Set Suggested Publish Date for each post")
    print("  4. Run: make blog-publish CLIENT=" + client_key)


def main() -> None:
    parser = argparse.ArgumentParser(description="Write full blog posts for Approved ideas")
    parser.add_argument("--client", required=True, help="Client key (e.g. summit_therapy)")
    parser.add_argument("--notes",  default="", help="Revision notes for this run")
    args = parser.parse_args()
    asyncio.run(run(args.client, notes=args.notes))


if __name__ == "__main__":
    main()
