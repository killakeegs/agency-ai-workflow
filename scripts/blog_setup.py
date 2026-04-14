#!/usr/bin/env python3
"""
blog_setup.py — Create the Blog Voice & Author Setup page in Notion

Run this once per client before generating blog ideas.
The setup page has guided prompts the team fills out (ideally with client input)
to define the writing voice, author identity, and style preferences.

`make blog-ideas` reads this page, synthesizes a Style Brief into section 9,
and refuses to run until the key sections are filled in by the team.

Usage:
    make blog-setup CLIENT=summit_therapy
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.clients import CLIENTS
from src.config import settings
from src.integrations.notion import NotionClient

CLIENTS_JSON_PATH = Path(__file__).parent.parent / "config" / "clients.json"


def _save_to_json(client_key: str, field: str, value: str) -> None:
    try:
        data = json.loads(CLIENTS_JSON_PATH.read_text()) if CLIENTS_JSON_PATH.exists() else {}
        if client_key not in data:
            data[client_key] = {}
        data[client_key][field] = value
        CLIENTS_JSON_PATH.write_text(json.dumps(data, indent=2))
    except Exception as e:
        print(f"  ⚠ Could not save {field} to clients.json: {e}")


def _heading(text: str, level: int = 2) -> dict:
    htype = {1: "heading_1", 2: "heading_2", 3: "heading_3"}.get(level, "heading_2")
    return {
        "object": "block",
        "type": htype,
        htype: {"rich_text": [{"type": "text", "text": {"content": text}}]},
    }


def _paragraph(text: str) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": [{"type": "text", "text": {"content": text}}]
        },
    }


def _divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def _callout(text: str, emoji: str = "📝") -> dict:
    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "icon": {"type": "emoji", "emoji": emoji},
            "rich_text": [{"type": "text", "text": {"content": text}}],
        },
    }


def _build_setup_blocks(client_key: str) -> list[dict]:
    return [
        _callout(
            f"Fill out each section below before running `make blog-ideas CLIENT={client_key}`. "
            "Ideally do this with the client — especially sections 3 (writing samples) and 7 (passion topics). "
            "Section 9 (Style Brief) is filled automatically by Claude — leave it blank.",
            "📋",
        ),
        _divider(),

        # 1. Author name & credentials
        _heading("1. Author Name & Credentials", 2),
        _callout(
            "Full name + credentials and role at the practice.\n"
            "Example: \"Sarah Chen, M.S., CCC-SLP, Founder & Lead Therapist\"\n"
            "This appears as the byline on every blog post.",
            "✍️",
        ),
        _paragraph(""),

        # 2. Author bio
        _heading("2. Author Bio", 2),
        _callout(
            "2–3 sentences written in first person. "
            "Used as the author card at the bottom of each post.\n\n"
            "Example: \"I'm Sarah, founder of Summit Therapy. I've spent 12 years helping children "
            "find their voice — literally and figuratively. When I'm not in session I'm probably "
            "reading research papers and pretending my dog is a co-therapist.\"",
            "🪪",
        ),
        _paragraph(""),

        # 3. Writing samples
        _heading("3. Writing Samples They Admire", 2),
        _callout(
            "Paste 3–5 URLs or short excerpts of content the author wishes they'd written. "
            "Could be a blog post, newsletter, book passage, podcast transcript — anything.\n\n"
            "For each one, add a note on what they like: the tone, structure, honesty, pacing, etc.\n\n"
            "This is the most important section. Real examples beat any description.\n"
            "Think: Brené Brown, Ezra Klein, Adam Grant, local practitioners they follow, newsletters "
            "they actually read — not just what sounds impressive.",
            "📖",
        ),
        _paragraph("Sample 1 (URL or excerpt):\nWhat I like about it:"),
        _paragraph("Sample 2 (URL or excerpt):\nWhat I like about it:"),
        _paragraph("Sample 3 (URL or excerpt):\nWhat I like about it:"),

        # 4. Voice in 5 words
        _heading("4. Voice in 5 Words", 2),
        _callout(
            "Five adjectives that describe the desired blog voice. Be specific — not vague.\n\n"
            "Not: \"professional and engaging\"\n"
            "Yes: \"warm, direct, non-clinical, curious, honest\"\n\n"
            "Not: \"informative and approachable\"\n"
            "Yes: \"grounded, plain-spoken, empathetic, specific, a little irreverent\"",
            "🎯",
        ),
        _paragraph(""),

        # 5. Primary audience
        _heading("5. Primary Audience", 2),
        _callout(
            "Who is the primary reader? Describe them specifically:\n"
            "• What do they know going in?\n"
            "• What are they afraid of?\n"
            "• What brought them to Google at 11pm?\n"
            "• What decision are they trying to make?\n\n"
            "Example: \"A parent of a 4-year-old who's been told to 'wait and see' but their gut says "
            "something is wrong. They're scared, overwhelmed, and don't know what questions to ask. "
            "They've been Googling at night instead of sleeping.\"",
            "👤",
        ),
        _paragraph(""),

        # 6. Refuse to sound like
        _heading("6. What They Refuse to Sound Like", 2),
        _callout(
            "List phrases, tones, or styles that absolutely don't fit this practice.\n\n"
            "Examples:\n"
            "• \"Not a textbook. Not a hospital website.\"\n"
            "• \"No 'holistic, evidence-based solutions.'\"\n"
            "• \"Not a wellness influencer. No toxic positivity.\"\n"
            "• \"Don't talk down to parents like they've never Googled anything.\"",
            "🚫",
        ),
        _paragraph(""),

        # 7. Passion topics
        _heading("7. Passion Topics", 2),
        _callout(
            "What would the author write about if no one was paying them?\n"
            "What aspects of their field do they feel most strongly about?\n"
            "What would they argue with a colleague about at a conference?\n"
            "What frustrates them about how their field is talked about publicly?\n\n"
            "These become the posts that feel most alive — prioritize them.",
            "🔥",
        ),
        _paragraph(""),

        # 8. Clinical language stance
        _heading("8. Stance on Clinical Language", 2),
        _callout(
            "How should clinical or technical terms be handled?\n\n"
            "Options:\n"
            "• Use them and define them (builds authority, shows expertise)\n"
            "• Avoid them entirely (more accessible, warmer tone)\n"
            "• Mix based on context (define once, then use naturally)\n\n"
            "Also note: are there terms they actively hate or find reductive? "
            "Terms the field overuses that have lost meaning?",
            "🔬",
        ),
        _paragraph(""),

        _divider(),

        # 9. Style Brief (Claude fills)
        _heading("9. Style Brief — Claude Fills This In", 2),
        _callout(
            "Leave this blank. Before the first blog run, Claude synthesizes sections 1–8 "
            "into a style brief paragraph the team can review and refine. "
            "Once you're happy with it, it drives every post going forward.\n\n"
            "To regenerate it: clear this section and run `make blog-ideas` again.",
            "🤖",
        ),
        _paragraph(""),
    ]


async def run(client_key: str) -> None:
    cfg = CLIENTS.get(client_key)
    if not cfg:
        print(f"Client '{client_key}' not found in config.")
        sys.exit(1)

    notion = NotionClient(api_key=settings.notion_api_key)
    client_name = cfg.get("name", client_key)

    # Check if already created
    existing_page_id = cfg.get("blog_voice_setup_page_id", "")
    if existing_page_id:
        print(f"\nBlog Voice Setup page already exists for {client_name}.")
        print(f"Open it in Notion: https://notion.so/{existing_page_id.replace('-', '')}")
        print("\nTo recreate, remove 'blog_voice_setup_page_id' from clients.json first.")
        return

    print(f"\n{'='*60}")
    print(f"  Blog Voice Setup — {client_name}")
    print(f"{'='*60}\n")

    # Resolve client root page from client_info_db parent
    client_info_db = cfg.get("client_info_db_id", "")
    if not client_info_db:
        print("⚠ No client_info_db_id found. Cannot determine parent page.")
        sys.exit(1)

    db_meta = await notion._client.request(
        path=f"databases/{client_info_db}",
        method="GET",
    )
    parent = db_meta.get("parent", {})
    parent_page_id = parent.get("page_id", "")
    if not parent_page_id:
        print("⚠ Could not determine client root page.")
        sys.exit(1)

    print("Creating Blog Voice & Author Setup page in Notion...")
    blocks = _build_setup_blocks(client_key)

    result = await notion._client.request(
        path="pages",
        method="POST",
        body={
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "properties": {
                "title": {"title": [{"text": {"content": "Blog Voice & Author Setup"}}]}
            },
            "children": blocks,
        },
    )

    page_id = result["id"]
    print(f"  ✓ Page created: {page_id}")

    _save_to_json(client_key, "blog_voice_setup_page_id", page_id)
    print("  ✓ Saved to clients.json")

    print(f"\n✓ Done. Open the setup page in Notion and fill out sections 1–8 with the client.")
    print(f"  https://notion.so/{page_id.replace('-', '')}")
    print(f"\nOnce filled, run:")
    print(f"  make blog-ideas CLIENT={client_key}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create Blog Voice & Author Setup page in Notion")
    parser.add_argument("--client", required=True, help="Client key (e.g. summit_therapy)")
    args = parser.parse_args()
    asyncio.run(run(args.client))


if __name__ == "__main__":
    main()
