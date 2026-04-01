"""
ContentAgent — Stage 5a: CONTENT_DRAFT (approval gate)

Triggered after SITEMAP_APPROVED. Reads every page in the approved sitemap and:
  - AI Generated pages: writes full publish-ready copy (H1, hero, sections, CTAs,
    FAQs, title tag, meta description) to a Content DB entry in Notion.
  - Client Provided pages: creates a structured template entry with labeled slots
    and instructions, status = "Client Providing".

The Content DB is created automatically under the client's Notion root page if
`content_db_id` is not passed (first-run convenience). The printed DB ID should
then be saved to run_pipeline_stage.py for future runs.

Input kwargs:
  - client_info_db_id (str)
  - meeting_notes_db_id (str)
  - brand_guidelines_db_id (str)
  - sitemap_db_id (str)
  - content_db_id (str | ""): Notion Content DB ID. Auto-created if empty.
  - mood_board_db_id (str): optional
  - revision_notes (str): optional

Output:
  - One Content DB entry per sitemap page
  - AI Generated status: "Team Review" (team reviews before client sees copy)
  - Client Provided status: "Client Providing"
  - Returns dict: status, ai_generated_count, client_provided_count,
    total_pages, content_db_id, notion_entry_ids
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..models.pipeline import PipelineStage
from .base_agent import AgentError, BaseAgent
from .tools import CONTENT_TOOLS

logger = logging.getLogger(__name__)

# ── Content DB schema — created on first run if not provided ──────────────────

CONTENT_DB_SCHEMA: dict[str, Any] = {
    "Page Title": {"title": {}},
    "Slug": {"rich_text": {}},
    "Page Type": {
        "select": {
            "options": [
                {"name": "Static", "color": "blue"},
                {"name": "CMS", "color": "green"},
            ]
        }
    },
    "Content Mode": {
        "select": {
            "options": [
                {"name": "AI Generated", "color": "purple"},
                {"name": "Client Provided", "color": "yellow"},
            ]
        }
    },
    "Status": {
        "select": {
            "options": [
                {"name": "Draft", "color": "gray"},
                {"name": "Team Review", "color": "blue"},
                {"name": "Client Review", "color": "yellow"},
                {"name": "Client Providing", "color": "orange"},
                {"name": "Approved", "color": "green"},
                {"name": "Revision Requested", "color": "red"},
            ]
        }
    },
    "Title Tag": {"rich_text": {}},
    "Meta Description": {"rich_text": {}},
    "H1": {"rich_text": {}},
    "SEO Keywords": {"rich_text": {}},
    "Word Count": {"number": {}},
}

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a senior copywriter and SEO strategist at a digital marketing agency
(RxMedia) specializing in telehealth and medical practice websites.

Your task: generate full, publish-ready page copy for this client's website.
All client details — services, brand voice, target audience, location, booking
system, and tone — are provided in the context below. Use them precisely.

SEO rules:
- Title tags: exactly 50–60 characters. Include the brand name on homepage and About only.
- Meta descriptions: exactly 150–160 characters. Lead with primary value prop +
  target keyword.
- H1: one per page, keyword-rich, natural sounding. Different from title tag.
- H2s: 2–5 per page, support page keywords and guide UX.
- Local SEO pages: weave the city/location naturally in first paragraph.
- Virtual/national pages: target condition + "telehealth" or "online" keywords.
- Location CMS template pages: write ONE complete sample entry using the client's
  primary location. The template will be replicated for other cities.
- FAQ sections: target questions patients/customers actually Google. 3-5 per page.

Return a JSON object with this exact structure:
{
  "pages": [
    {
      "title": "Exact page title from the sitemap",
      "slug": "/url-slug",
      "title_tag": "SEO title tag (50-60 chars)",
      "meta_description": "SEO meta description (150-160 chars)",
      "h1": "Page H1 — keyword-rich, compelling, unique across all pages",
      "hero": {
        "headline": "Hero headline — can match H1 or be a punchier tagline",
        "subheadline": "1-2 sentences. Expands on headline with the key patient benefit.",
        "cta_primary": "Primary CTA button text — specific and action-oriented",
        "cta_secondary": "Secondary CTA text (empty string if not needed)"
      },
      "sections": [
        {
          "section_name": "Internal label for team reference (e.g. 'Services Overview')",
          "h2": "Section H2 — keyword-supporting, benefit-led",
          "subhead": "Short supporting line under H2 (1 sentence, or empty string)",
          "body": "Full section body. 2–4 paragraphs separated by \\n\\n. Real copy only.",
          "cta": "Section CTA button text (empty string if none)"
        }
      ],
      "faqs": [
        {
          "question": "Question a patient would actually search on Google",
          "answer": "Clear, authoritative 2–4 sentence answer. Warm but precise."
        }
      ],
      "seo_keywords": ["primary keyword", "secondary keyword", "location modifier"],
      "word_count_estimate": 600,
      "internal_notes": "Team note on copy strategy or any special considerations"
    }
  ]
}

Rules:
- Write REAL, publish-ready copy — not skeletons, not placeholders
- Lead with patient outcomes and benefits, not features or service descriptions
- Every H1 and H2 must be unique across ALL pages in the batch
- CTAs must be specific: "Book Your Free Consultation", not "Learn More" or "Click Here"
- Body paragraphs: 2–4 sentences each, conversational, no filler phrases
- Sections per page: 3–5 (not counting hero or FAQ)
- For virtual service pages: lead with convenience, accessibility, expert care from home
- For in-person pages: lead with the expertise + local community connection (use client's location from brand guidelines)
- For CMS collection template pages: write ONE real sample entry that demonstrates
  the content depth — this is the reference the developer builds the CMS template from
- Only return the JSON object — no markdown code fences, no preamble, no commentary
"""

# ── Notion helpers ────────────────────────────────────────────────────────────


def _blocks_to_text(blocks: list[dict]) -> str:
    lines = []
    for block in blocks:
        block_type = block.get("type", "")
        content = block.get(block_type, {})
        rich_text = content.get("rich_text", [])
        text = "".join(seg.get("text", {}).get("content", "") for seg in rich_text)
        if text:
            lines.append(text)
    return "\n".join(lines)


def _get_rich_text(prop: dict) -> str:
    return "".join(
        p.get("text", {}).get("content", "") for p in prop.get("rich_text", [])
    )


def _get_title(prop: dict) -> str:
    """Extract text from a Notion title-type property."""
    return "".join(
        p.get("text", {}).get("content", "") for p in prop.get("title", [])
    )


def _get_select(prop: dict) -> str:
    sel = prop.get("select")
    return sel.get("name", "") if sel else ""


# ── Block builders ────────────────────────────────────────────────────────────


def _h(text: str, level: int = 2) -> dict:
    ht = f"heading_{level}"
    return {"object": "block", "type": ht, ht: {
        "rich_text": [{"type": "text", "text": {"content": text[:1900]}}]
    }}


def _p(text: str) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {
        "rich_text": [{"type": "text", "text": {"content": text[:1900]}}]
    }}


def _bullet(text: str) -> dict:
    return {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {
        "rich_text": [{"type": "text", "text": {"content": text[:1900]}}]
    }}


def _divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def _callout(text: str, emoji: str = "📝") -> dict:
    return {"object": "block", "type": "callout", "callout": {
        "rich_text": [{"type": "text", "text": {"content": text[:1900]}}],
        "icon": {"emoji": emoji},
    }}


def _page_blocks(page: dict) -> list[dict]:
    """Build rich Notion blocks for a fully generated page's copy."""
    blocks: list[dict] = []

    # ── SEO Summary ──────────────────────────────────────────────────────────
    blocks.append(_h("── SEO ──", 2))
    blocks.append(_bullet(f"Title Tag ({len(page.get('title_tag', ''))} chars): "
                          f"{page.get('title_tag', '')}"))
    blocks.append(_bullet(f"Meta Description ({len(page.get('meta_description', ''))} chars): "
                          f"{page.get('meta_description', '')}"))
    blocks.append(_bullet(f"H1: {page.get('h1', '')}"))
    kw = ", ".join(page.get("seo_keywords", []))
    if kw:
        blocks.append(_bullet(f"Keywords: {kw}"))
    blocks.append(_divider())

    # ── Hero ─────────────────────────────────────────────────────────────────
    hero = page.get("hero", {})
    if hero:
        blocks.append(_h("── Hero Section ──", 2))
        blocks.append(_h(f'Headline: "{hero.get("headline", "")}"', 3))
        blocks.append(_p(f'Subheadline: {hero.get("subheadline", "")}'))
        blocks.append(_bullet(f"Primary CTA: {hero.get('cta_primary', '')}"))
        if hero.get("cta_secondary"):
            blocks.append(_bullet(f"Secondary CTA: {hero.get('cta_secondary', '')}"))
        blocks.append(_divider())

    # ── Sections ─────────────────────────────────────────────────────────────
    sections = page.get("sections", [])
    if sections:
        blocks.append(_h("── Page Sections ──", 2))
        for section in sections:
            label = section.get("section_name", "Section")
            h2 = section.get("h2", "")
            blocks.append(_h(f"[{label}]  {h2}", 3))
            subhead = section.get("subhead", "")
            if subhead:
                blocks.append(_p(f"Subhead: {subhead}"))
            body = section.get("body", "")
            for para in body.split("\n\n"):
                if para.strip():
                    # Chunk long paragraphs
                    para = para.strip()
                    for i in range(0, len(para), 1900):
                        blocks.append(_p(para[i:i + 1900]))
            cta = section.get("cta", "")
            if cta:
                blocks.append(_bullet(f"CTA: {cta}"))
        blocks.append(_divider())

    # ── FAQs ─────────────────────────────────────────────────────────────────
    faqs = page.get("faqs", [])
    if faqs:
        blocks.append(_h("── FAQs (SEO Schema Markup) ──", 2))
        for faq in faqs:
            blocks.append(_h(faq.get("question", ""), 3))
            blocks.append(_p(faq.get("answer", "")))
        blocks.append(_divider())

    # ── Internal notes ────────────────────────────────────────────────────────
    notes = page.get("internal_notes", "")
    if notes:
        blocks.append(_h("── Internal Notes ──", 2))
        blocks.append(_p(notes))

    return blocks


def _client_provided_blocks(page_title: str, key_sections_text: str) -> list[dict]:
    """Build template blocks for a page where the client writes the copy."""
    blocks: list[dict] = [
        _callout(
            f"This page requires content from the client. Fill in each labeled "
            f"section below, then change Status to 'Client Review' when complete.",
            "📝"
        ),
        _h("Required Content", 2),

        _h("Title Tag (50-60 characters)", 3),
        _p("[Your SEO title tag — include your primary keyword]"),

        _h("Meta Description (150-160 characters)", 3),
        _p("[Brief description of this page for Google search results]"),

        _h("Page Headline (H1)", 3),
        _p("[The main headline for this page — keyword-rich and compelling]"),

        _h("Hero Section", 3),
        _p("[Main headline, supporting sentence, and CTA button text]"),
    ]

    # Parse key sections from the sitemap entry
    if key_sections_text:
        blocks.append(_h("Page Sections", 2))
        for line in key_sections_text.split("\n"):
            line = line.strip().lstrip("•–- ").strip()
            if line:
                blocks.append(_h(line, 3))
                blocks.append(_p(f"[Your content for: {line}]"))

    blocks += [
        _h("FAQs (3-5 Q&A pairs for SEO)", 3),
        _p("[Q: ...]\n[A: ...]"),
    ]

    return blocks


# ── Agent ─────────────────────────────────────────────────────────────────────


class ContentAgent(BaseAgent):
    """
    Generates full page copy for all AI Generated sitemap pages, and creates
    structured template entries for Client Provided pages.

    Output lives in a 'Page Content' Notion database. Status is 'Team Review'
    for AI copy (team reviews before client sees it) and 'Client Providing' for
    pages the client must write.
    """

    name = "content"
    tools = CONTENT_TOOLS

    async def _ensure_content_db(
        self,
        content_db_id: str | None,
        sitemap_db_id: str,
    ) -> str:
        """
        Return content_db_id if already set. Otherwise find the sitemap DB's
        parent page and create a new 'Page Content' database there.
        Prints the new ID so the user can persist it in run_pipeline_stage.py.
        """
        if content_db_id:
            return content_db_id

        self.log.info(
            "content_db_id not configured — locating parent page via sitemap DB"
        )
        try:
            db_info = await self.notion._client.databases.retrieve(
                database_id=sitemap_db_id
            )
            parent = db_info.get("parent", {})
            parent_page_id = parent.get("page_id") or parent.get("block_id")
            if not parent_page_id:
                raise AgentError(
                    "Cannot determine client Notion page from sitemap DB parent"
                )
        except Exception as exc:
            raise AgentError(
                f"Failed to retrieve sitemap DB parent: {exc}"
            ) from exc

        self.log.info(f"Creating Page Content DB under page {parent_page_id}")
        new_db_id = await self.notion.create_database(
            parent_page_id=parent_page_id,
            title="Page Content",
            properties_schema=CONTENT_DB_SCHEMA,
        )
        self.log.info(f"Content DB created: {new_db_id}")
        print(
            f"\n{'=' * 60}\n"
            f"Content DB created — ID: {new_db_id}\n"
            f"Add this to CLIENTS['wellwell']['content_db_id'] in\n"
            f"scripts/run_pipeline_stage.py and re-run to avoid recreating.\n"
            f"{'=' * 60}\n"
        )
        return new_db_id

    async def run(self, client_id: str, **kwargs: Any) -> dict:
        """
        Generate content for all pages in the approved sitemap.

        Required kwargs:
          - client_info_db_id
          - meeting_notes_db_id
          - brand_guidelines_db_id
          - sitemap_db_id

        Optional kwargs:
          - content_db_id (str): auto-created if empty
          - mood_board_db_id (str): reads approved creative direction
          - revision_notes (str): feedback to guide regeneration
        """
        client_info_db_id = kwargs["client_info_db_id"]
        meeting_notes_db_id = kwargs["meeting_notes_db_id"]
        brand_guidelines_db_id = kwargs["brand_guidelines_db_id"]
        sitemap_db_id = kwargs["sitemap_db_id"]
        content_db_id_arg = kwargs.get("content_db_id") or ""
        mood_board_db_id = kwargs.get("mood_board_db_id")
        revision_notes = kwargs.get("revision_notes", "")

        self.log.info(f"ContentAgent starting | client={client_id}")

        # ── Step 0: Ensure Content DB exists ──────────────────────────────────
        content_db_id = await self._ensure_content_db(
            content_db_id_arg or None,
            sitemap_db_id,
        )

        # ── Step 1: Gather client context ─────────────────────────────────────

        # Client info
        client_entries = await self.notion.query_database(client_info_db_id)
        client_props = client_entries[0]["properties"] if client_entries else {}
        company = _get_rich_text(client_props.get("Company", {})) or client_id
        business_type = _get_select(client_props.get("Business Type", {}))
        client_notes = _get_rich_text(client_props.get("Notes", {}))

        # Brand guidelines
        brand_context = ""
        brand_entries = await self.notion.query_database(brand_guidelines_db_id)
        if brand_entries:
            bp = brand_entries[0]["properties"]
            brand_page_id = brand_entries[0]["id"]
            tone = _get_rich_text(bp.get("Tone Descriptors", {}))
            raw = _get_rich_text(bp.get("Raw Guidelines", {}))
            brand_context = f"Tone: {tone}\n{raw[:3000]}"
            brand_blocks = await self.notion.get_block_children(brand_page_id)
            brand_body = _blocks_to_text(brand_blocks)
            if brand_body:
                brand_context += f"\n\nBrand Document:\n{brand_body[:3000]}"

        # Meeting notes
        meeting_context = ""
        meeting_entries = await self.notion.query_database(meeting_notes_db_id)
        if meeting_entries:
            parsed = [
                e for e in meeting_entries
                if e["properties"].get("Parsed", {}).get("checkbox", False)
            ]
            target = parsed[0] if parsed else meeting_entries[0]
            mp = target["properties"]
            meeting_page_id = target["id"]
            key_decisions = _get_rich_text(mp.get("Key Decisions", {}))
            meeting_context = f"Key Decisions:\n{key_decisions}"
            meeting_blocks = await self.notion.get_block_children(meeting_page_id)
            meeting_body = _blocks_to_text(meeting_blocks)
            analysis_start = meeting_body.find("AI-Parsed Meeting Analysis")
            if analysis_start != -1:
                meeting_context += (
                    f"\n\nFull Meeting Analysis:\n"
                    f"{meeting_body[analysis_start:analysis_start + 4000]}"
                )

        # Approved mood board direction
        mood_context = ""
        if mood_board_db_id:
            mood_entries = await self.notion.query_database(mood_board_db_id)
            if mood_entries:
                approved = next(
                    (
                        e for e in mood_entries
                        if _get_select(e["properties"].get("Status", {}))
                        in ("Approved", "Pending Review")
                    ),
                    mood_entries[0],
                )
                mp2 = approved["properties"]
                mood_context = (
                    f"Approved creative direction: "
                    f"{_get_rich_text(mp2.get('Style Keywords', {}))}\n"
                    f"Color palette: "
                    f"{_get_rich_text(mp2.get('Color Palette Description', {}))}"
                )

        self.log.info("Context loaded — reading sitemap...")

        # ── Step 2: Read all sitemap pages ────────────────────────────────────
        sitemap_entries = await self.notion.query_database(
            sitemap_db_id,
            sorts=[{"property": "Order", "direction": "ascending"}],
        )

        if not sitemap_entries:
            raise AgentError(
                "No sitemap pages found. Run the sitemap stage first."
            )

        self.log.info(f"Found {len(sitemap_entries)} sitemap entries")

        # Parse each entry — handle both "Name" and "Page Title" title fields
        sitemap_pages: list[dict] = []
        for entry in sitemap_entries:
            pp = entry["properties"]
            title = (
                _get_title(pp.get("Name", {}))
                or _get_title(pp.get("Page Title", {}))
                or _get_rich_text(pp.get("Name", {}))
                or "Untitled"
            )
            sitemap_pages.append({
                "title": title,
                "slug": _get_rich_text(pp.get("Slug", {})),
                "page_type": _get_select(pp.get("Page Type", {})) or "Static",
                "content_mode": (
                    _get_select(pp.get("Content Mode", {})) or "AI Generated"
                ),
                "purpose": _get_rich_text(pp.get("Purpose", {})),
                "key_sections": _get_rich_text(pp.get("Key Sections", {})),
                "order": pp.get("Order", {}).get("number", 99),
            })

        # ── Check which pages already exist in the Content DB (resume support) ──
        existing_entries = await self.notion.query_database(content_db_id)
        already_done: set[str] = set()
        for entry in existing_entries:
            ep = entry["properties"]
            slug = _get_rich_text(ep.get("Slug", {}))
            title = (
                _get_title(ep.get("Page Title", {}))
                or _get_title(ep.get("Name", {}))
                or ""
            )
            if slug:
                already_done.add(slug)
            if title:
                already_done.add(title)

        if already_done:
            self.log.info(
                f"Skipping {len(already_done)} pages already in Content DB"
            )

        ai_pages = [
            p for p in sitemap_pages
            if p["content_mode"] == "AI Generated"
            and p["slug"] not in already_done
            and p["title"] not in already_done
        ]
        client_pages = [
            p for p in sitemap_pages
            if p["content_mode"] == "Client Provided"
            and p["slug"] not in already_done
            and p["title"] not in already_done
        ]

        self.log.info(
            f"AI Generated: {len(ai_pages)} to write | "
            f"Client Provided: {len(client_pages)} to write"
        )

        # ── Step 3: Generate AI copy (batches of 8 pages per Claude call) ─────
        generated_pages: list[dict] = []
        batch_size = 4

        for batch_idx in range(0, len(ai_pages), batch_size):
            batch = ai_pages[batch_idx:batch_idx + batch_size]
            batch_num = batch_idx // batch_size + 1
            self.log.info(
                f"Batch {batch_num}: generating copy for "
                f"{[p['title'] for p in batch]}"
            )

            page_list = ""
            for pg in batch:
                page_list += (
                    f"\n---\n"
                    f"Page: {pg['title']}\n"
                    f"Slug: {pg['slug']}\n"
                    f"Type: {pg['page_type']}\n"
                    f"Purpose: {pg['purpose']}\n"
                    f"Key Sections from Sitemap:\n{pg['key_sections']}\n"
                )

            user_message = f"""CLIENT: {company}
BUSINESS TYPE: {business_type}

BRAND GUIDELINES:
{brand_context[:4000]}

MEETING CONTEXT:
{meeting_context[:3000]}

{f'CREATIVE DIRECTION: {mood_context}' if mood_context else ''}

ONBOARDING CONTEXT:
{client_notes[:1000]}

Generate full, publish-ready page copy for the following {len(batch)} pages:
{page_list}

{f'''REVISION REQUEST — This is a regeneration. Apply this feedback:

{revision_notes}

Do NOT reproduce previous copy. Use the feedback to meaningfully improve it.
''' if revision_notes else ''}Write real copy — not skeleton templates. Return the JSON as specified."""

            response = await self.anthropic.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )

            raw_output = response.content[0].text if response.content else ""
            self.log.info(
                f"  Batch {batch_num} — Claude: {response.usage.output_tokens} tokens"
            )

            try:
                clean = re.sub(r"```(?:json)?\n?", "", raw_output).strip()
                batch_data: dict = json.loads(clean)
                generated_pages.extend(batch_data.get("pages", []))
            except json.JSONDecodeError as exc:
                self.log.error(
                    f"JSON parse failed for batch {batch_num}: {exc}\n"
                    f"{raw_output[:400]}"
                )
                raise AgentError(
                    f"ContentAgent: JSON parse failed in batch {batch_num} — {exc}"
                ) from exc

        # ── Step 4: Write AI-generated pages to Content DB ────────────────────
        created_ids: list[str] = []

        for page_data in generated_pages:
            title = page_data.get("title", "Untitled")
            slug = page_data.get("slug", "/")

            # Look up page_type from sitemap (Claude may not preserve it exactly)
            matching = next(
                (p for p in ai_pages
                 if p["title"] == title or p["slug"] == slug),
                None,
            )
            page_type = matching["page_type"] if matching else "Static"

            title_tag = page_data.get("title_tag", "")[:2000]
            meta_desc = page_data.get("meta_description", "")[:2000]
            h1 = page_data.get("h1", "")[:2000]
            seo_kw = ", ".join(page_data.get("seo_keywords", []))[:2000]
            word_count = page_data.get("word_count_estimate", 0)
            if not isinstance(word_count, int):
                word_count = 0

            entry_id = await self.notion.create_database_entry(content_db_id, {
                "Page Title": self.notion.title_property(title),
                "Slug": self.notion.text_property(slug),
                "Page Type": self.notion.select_property(page_type),
                "Content Mode": self.notion.select_property("AI Generated"),
                "Status": self.notion.select_property("Team Review"),
                "Title Tag": self.notion.text_property(title_tag),
                "Meta Description": self.notion.text_property(meta_desc),
                "H1": self.notion.text_property(h1),
                "SEO Keywords": self.notion.text_property(seo_kw),
                "Word Count": {"number": word_count},
            })

            content_blocks = _page_blocks(page_data)
            for i in range(0, len(content_blocks), 90):
                await self.notion.append_blocks(entry_id, content_blocks[i:i + 90])

            created_ids.append(entry_id)
            self.log.info(f"  ✓ [{page_type}/AI] {title} ({slug}) → {entry_id}")

        # ── Step 5: Create client-provided template entries ───────────────────
        client_ids: list[str] = []

        for pg in client_pages:
            entry_id = await self.notion.create_database_entry(content_db_id, {
                "Page Title": self.notion.title_property(pg["title"]),
                "Slug": self.notion.text_property(pg["slug"]),
                "Page Type": self.notion.select_property(pg["page_type"]),
                "Content Mode": self.notion.select_property("Client Provided"),
                "Status": self.notion.select_property("Client Providing"),
                "Title Tag": self.notion.text_property(""),
                "Meta Description": self.notion.text_property(""),
                "H1": self.notion.text_property(""),
                "SEO Keywords": self.notion.text_property(""),
                "Word Count": {"number": 0},
            })

            template_blocks = _client_provided_blocks(
                pg["title"], pg["key_sections"]
            )
            for i in range(0, len(template_blocks), 90):
                await self.notion.append_blocks(entry_id, template_blocks[i:i + 90])

            client_ids.append(entry_id)
            self.log.info(
                f"  ✓ [Client Template] {pg['title']} ({pg['slug']}) → {entry_id}"
            )

        total = len(created_ids) + len(client_ids)
        self.log.info(
            f"ContentAgent complete | AI: {len(created_ids)} | "
            f"Client: {len(client_ids)} | Total: {total}"
        )

        return {
            "status": "success",
            "stage": PipelineStage.CONTENT_DRAFT.value,
            "ai_generated_count": len(created_ids),
            "client_provided_count": len(client_ids),
            "total_pages": total,
            "content_db_id": content_db_id,
            "notion_entry_ids": created_ids + client_ids,
        }

    async def _execute_tool(self, tool_name: str, tool_input: dict) -> str:
        raise NotImplementedError(
            f"ContentAgent uses direct API calls. Tool {tool_name} not dispatched."
        )
