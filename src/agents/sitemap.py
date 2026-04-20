"""
SitemapAgent — Stage 4: SITEMAP_DRAFT (approval gate)

Reads client context and the approved creative direction, then generates a
complete sitemap: every page with its type (Static/CMS), content mode
(AI Generated/Client Provided), purpose, key sections, URL slug, and order.

Also produces an SEO strategy note and CMS collection schema recommendations
for Webflow.

Input kwargs:
  - client_info_db_id (str)
  - meeting_notes_db_id (str)
  - brand_guidelines_db_id (str)
  - sitemap_db_id (str): Notion DB where pages will be created
  - mood_board_db_id (str): optional, reads approved direction if available

Output:
  - One entry per page in Sitemap DB (Page Title, Slug, Page Type, Content Mode,
    Status, Purpose, Key Sections, Order)
  - Returns dict with status, page count, and Notion entry IDs
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..models.pipeline import PipelineStage
from .base_agent import AgentError, BaseAgent
from .tools import SITEMAP_TOOLS

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a senior information architect and SEO strategist at a digital marketing
agency specializing in telehealth and medical practice websites.

Your task: generate a complete sitemap for a telehealth website.

For each page you must decide:
- Page Type: "Static" (fixed content) or "CMS" (Webflow CMS collection — for
  blog posts, conditions, location pages, team members)
- Content Mode: "AI Generated" (Claude will write the copy) or
  "Client Provided" (client must supply the content — use sparingly, only for
  content only the client can write: founder story, pricing, team bios)
- SEO considerations: which pages need location sub-pages for local SEO

Return a single JSON object with this exact structure:

{
  "pages": [
    {
      "title": "Exact page title as it will appear in navigation",
      "slug": "/url-slug",
      "parent_slug": "/url-of-parent-page or null",
      "page_type": "Static" or "CMS",
      "content_mode": "AI Generated" or "Client Provided",
      "section": "Core" or "Services" or "Service Subcategories" or "Who We Serve" or "Locations" or "Programs" or "Patient Resources" or "Blog" or "Legal",
      "order": 1,
      "purpose": "One sentence: what is the primary goal of this page?",
      "key_sections": [
        "Section name — brief description of content",
        "Section name — brief description"
      ],
      "seo_notes": "Any SEO-specific notes for this page (target keywords, schema markup, etc.)",
      "webflow_notes": "Any Webflow-specific build notes (CMS collection name, dynamic vs static, etc.)"
    }
  ],
  "cms_collections": [
    {
      "collection_name": "Blog Posts",
      "fields": ["Title", "Slug", "Body", "Category", "Published Date", "Author", "Featured Image"],
      "notes": "Used for patient education articles and recipes"
    }
  ],
  "seo_strategy": {
    "primary_keywords": ["keyword 1", "keyword 2"],
    "location_seo_approach": "Describe the approach for local/regional SEO with sub-pages",
    "content_pillar_strategy": "How blog/conditions content will build domain authority",
    "schema_markup_recommendations": ["LocalBusiness schema", "MedicalOrganization schema", "FAQPage schema"]
  },
  "total_pages": 42,
  "notes": "Any important build notes or decisions the developer needs to know"
}

Rules:
- Every page must have a clear purpose and at least 3 key sections
- "Client Provided" should only be used for content truly unique to the client
  (founder story, specific pricing, team photos/bios)
- Include ALL pages: main nav, legal pages, CMS collection templates
- Order numbers should reflect nav priority (1 = homepage, highest = legal/utility)
- Slugs must be clean: no stop words (a, the, and, of, for), no dates, no trailing slashes.
  e.g. /speech-therapy not /the-best-speech-therapy or /speech-therapy-services-2026
- Keep the total page list to the core navigation pages + one CMS template
  entry per repeating pattern (blog posts, location pages, service subcategories)
- Only return the JSON object — no markdown, no commentary

PAGES TO EXCLUDE BY DEFAULT — do not include these unless the client explicitly requests them:
- Careers page — most clients don't need it; add only if client specifically asks
- Standalone Testimonials page — testimonials are embedded on Home and all service pages; a dedicated page is redundant

MANDATORY CMS RULES — no exceptions:
- Service subcategory pages (e.g. /services/speech-therapy/fluency) → always "CMS"
  The service hub page (e.g. /services/speech-therapy) stays "Static"
- Blog posts → always "CMS". The blog hub/index page (/blog) stays "Static"
- Individual location pages (e.g. /locations/frisco) → always "CMS"
  The locations hub/overview page (/locations) stays "Static"
- Any page that is one instance of a repeating template → "CMS"
- Hub/index/overview pages that list CMS items → "Static"

PARENT PAGE RULES (for topical architecture + SEO internal linking):
- Every page must have a `parent_slug` field that points to the slug of its parent page, or null if it's a top-level page.
- Top-level pages have parent_slug = null. Examples: /, /about, /services, /locations, /blog, /contact, /faq, /terms.
- Subpages have parent_slug = the slug of their direct parent (one level up).
  - /services/speech-therapy → parent_slug: "/services"
  - /services/speech-therapy/fluency → parent_slug: "/services/speech-therapy"
  - /locations/frisco → parent_slug: "/locations"
  - /blog/how-to-talk-to-kids → parent_slug: "/blog"
- Legal pages (privacy, terms, accessibility) have parent_slug = null (they live in the footer, topically standalone).
- The parent_slug you emit MUST exist as another page's slug in the same response. Never reference a slug that doesn't exist in the sitemap.
- Downstream agents use parent_slug to (1) build nav dropdowns, (2) auto-suggest internal links between related pages, (3) group pages into topical clusters for SEO.
"""


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
    return "".join(p.get("text", {}).get("content", "") for p in prop.get("rich_text", []))


def _get_select(prop: dict) -> str:
    sel = prop.get("select")
    return sel.get("name", "") if sel else ""


def _summary_blocks(seo: dict, cms: list[dict], notes: str) -> list[dict]:
    """Build summary blocks appended to the first Sitemap entry."""

    def h(text: str, level: int = 2) -> dict:
        ht = f"heading_{level}"
        return {"object": "block", "type": ht, ht: {
            "rich_text": [{"type": "text", "text": {"content": text}}]
        }}

    def p(text: str) -> dict:
        return {"object": "block", "type": "paragraph", "paragraph": {
            "rich_text": [{"type": "text", "text": {"content": text[:1900]}}]
        }}

    def bullet(text: str) -> dict:
        return {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {
            "rich_text": [{"type": "text", "text": {"content": text[:1900]}}]
        }}

    blocks: list[dict] = [
        h("── SEO Strategy ──", 2),
        h("Primary Keywords", 3),
        *[bullet(kw) for kw in seo.get("primary_keywords", [])],
        h("Location SEO Approach", 3),
        p(seo.get("location_seo_approach", "")),
        h("Content Pillar Strategy", 3),
        p(seo.get("content_pillar_strategy", "")),
        h("Schema Markup", 3),
        *[bullet(s) for s in seo.get("schema_markup_recommendations", [])],
        h("── Webflow CMS Collections ──", 2),
    ]

    for col in cms:
        blocks += [
            h(col.get("collection_name", "Collection"), 3),
            bullet(f"Fields: {', '.join(col.get('fields', []))}"),
            p(col.get("notes", "")),
        ]

    if notes:
        blocks += [
            h("── Build Notes ──", 2),
            p(notes),
        ]

    return blocks


class SitemapAgent(BaseAgent):
    """Generates full sitemap with SEO strategy and CMS collection schemas."""

    name = "sitemap"
    tools = SITEMAP_TOOLS

    async def _patch_missing_fields(self, sitemap_db_id: str) -> None:
        """
        Add any self-healing fields to the Sitemap DB that aren't there yet.

        Currently heals: Parent Page (self-referential relation for topical
        architecture + SEO internal linking). Safe to re-run.
        """
        db_info = await self.notion._client.request(
            path=f"databases/{sitemap_db_id}", method="GET",
        )
        existing = db_info.get("properties", {})
        to_add: dict[str, Any] = {}

        if "Parent Page" not in existing:
            to_add["Parent Page"] = {
                "relation": {
                    "database_id": sitemap_db_id,
                    "type": "single_property",
                    "single_property": {},
                }
            }

        if "Section" not in existing:
            to_add["Section"] = {
                "select": {
                    "options": [
                        {"name": "Core",                 "color": "blue"},
                        {"name": "Services",             "color": "green"},
                        {"name": "Service Subcategories","color": "purple"},
                        {"name": "Who We Serve",         "color": "yellow"},
                        {"name": "Locations",            "color": "orange"},
                        {"name": "Programs",             "color": "pink"},
                        {"name": "Patient Resources",    "color": "brown"},
                        {"name": "Blog",                 "color": "gray"},
                        {"name": "Legal",                "color": "red"},
                    ]
                }
            }

        if to_add:
            await self.notion._client.request(
                path=f"databases/{sitemap_db_id}",
                method="PATCH",
                body={"properties": to_add},
            )
            self.log.info(f"Patched Sitemap DB — added: {', '.join(to_add.keys())}")

    async def run(self, client_id: str, **kwargs: Any) -> dict:
        """
        Generate sitemap pages for a client.

        Required kwargs:
          - client_info_db_id
          - brand_guidelines_db_id
          - sitemap_db_id

        Optional kwargs:
          - client_log_db_id: queried for meeting context (preferred)
          - meeting_notes_db_id: legacy alias for client_log_db_id
          - business_profile_page_id: populated automatically by meeting processor
          - mood_board_db_id: legacy — mood board stage was removed
          - revision_notes (str): feedback from previous run to guide regeneration
        """
        client_info_db_id = kwargs["client_info_db_id"]
        # Accept either client_log_db_id (new) or meeting_notes_db_id (legacy alias)
        meeting_notes_db_id = kwargs.get("client_log_db_id") or kwargs.get("meeting_notes_db_id", "")
        brand_guidelines_db_id = kwargs["brand_guidelines_db_id"]
        sitemap_db_id = kwargs["sitemap_db_id"]
        business_profile_page_id = kwargs.get("business_profile_page_id", "")
        mood_board_db_id = kwargs.get("mood_board_db_id")
        revision_notes = kwargs.get("revision_notes", "")

        self.log.info(f"SitemapAgent starting | client={client_id}")

        # ── Step 0a: Self-heal Sitemap DB schema (Parent Page, etc) ──────────
        await self._patch_missing_fields(sitemap_db_id)

        # ── Step 0b: Clear existing Draft pages to prevent duplicates ────────
        existing = await self.notion.query_database(sitemap_db_id)
        draft_pages = [
            e for e in existing
            if _get_select(e["properties"].get("Status", {})) == "Draft"
        ]
        if draft_pages:
            self.log.info(f"Clearing {len(draft_pages)} existing Draft pages...")
            for page in draft_pages:
                await self.notion._client.request(
                    path=f"pages/{page['id']}", method="PATCH",
                    body={"in_trash": True},
                )
            self.log.info("  ✓ Draft pages cleared")

        # ── Step 1: Gather context ────────────────────────────────────────────

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
            tone = _get_rich_text(bp.get("Tone Descriptors", {}))
            raw = _get_rich_text(bp.get("Raw Guidelines", {}))
            brand_context = f"Tone: {tone}\n{raw[:2000]}"

        # Business Profile — the richest source of client context (populated by
        # meeting processor + email enrichment). Load this first; if present, it
        # usually beats digging through raw meeting notes.
        business_profile_context = ""
        if business_profile_page_id:
            try:
                from src.integrations.business_profile import load_business_profile
                business_profile_context = await load_business_profile(
                    self.notion, {"business_profile_page_id": business_profile_page_id}
                )
            except Exception as e:
                self.log.warning(f"Could not load Business Profile: {e}")

        # Meeting notes — pulls from Client Log DB (new schema) or legacy Meeting Notes DB
        meeting_context = ""
        if meeting_notes_db_id:
            meeting_entries = await self.notion.query_database(meeting_notes_db_id)
            if meeting_entries:
                # Prefer Meeting-type entries; fall back to whatever's newest
                meeting_rows = [
                    e for e in meeting_entries
                    if _get_select(e["properties"].get("Type", {})) == "Meeting"
                ]
                # Legacy fallback: Parsed checkbox on the old Meeting Notes DB
                if not meeting_rows:
                    meeting_rows = [
                        e for e in meeting_entries
                        if e["properties"].get("Parsed", {}).get("checkbox", False)
                    ]
                target = meeting_rows[0] if meeting_rows else meeting_entries[0]
                mp = target["properties"]

                # Client Log DB stores recap as structured fields
                summary = _get_rich_text(mp.get("Summary", {}))
                key_decisions = _get_rich_text(mp.get("Key Decisions", {}))
                action_items = _get_rich_text(mp.get("Action Items", {}))
                next_steps = _get_rich_text(mp.get("Next Steps", {}))
                parts = []
                if summary:       parts.append(f"SUMMARY:\n{summary}")
                if key_decisions: parts.append(f"KEY DECISIONS:\n{key_decisions}")
                if action_items:  parts.append(f"ACTION ITEMS:\n{action_items}")
                if next_steps:    parts.append(f"NEXT STEPS:\n{next_steps}")
                if parts:
                    meeting_context = "\n\n".join(parts)

                # Legacy body fallback: look for "AI-Parsed Meeting Analysis" block
                if not meeting_context:
                    meeting_blocks = await self.notion.get_block_children(target["id"])
                    meeting_body = _blocks_to_text(meeting_blocks)
                    analysis_start = meeting_body.find("AI-Parsed Meeting Analysis")
                    if analysis_start != -1:
                        meeting_context = f"FULL ANALYSIS:\n{meeting_body[analysis_start:analysis_start+5000]}"

        # Combine Business Profile + meeting context (BP first, it's the primary source)
        if business_profile_context:
            meeting_context = f"BUSINESS PROFILE (the canonical source — trust this over everything else):\n{business_profile_context[:8000]}\n\n" + (meeting_context or "")

        # Mood board direction (if available)
        mood_context = ""
        if mood_board_db_id:
            mood_entries = await self.notion.query_database(mood_board_db_id)
            if mood_entries:
                approved = next(
                    (e for e in mood_entries
                     if _get_select(e["properties"].get("Status", {})) in ("Approved", "Pending Review")),
                    mood_entries[0]
                )
                mp2 = approved["properties"]
                style = _get_rich_text(mp2.get("Style Keywords", {}))
                palette = _get_rich_text(mp2.get("Color Palette Description", {}))
                mood_context = f"Creative direction: {style}\nColor palette: {palette}"

        self.log.info("All context loaded, calling Claude for sitemap generation...")

        # ── Step 2: Call Claude ───────────────────────────────────────────────
        user_message = f"""CLIENT: {company}
BUSINESS TYPE: {business_type}

ONBOARDING NOTES:
{client_notes}

BRAND/TONE:
{brand_context[:2000]}

{meeting_context[:5000]}

CREATIVE DIRECTION:
{mood_context}

Generate the complete sitemap based entirely on the client context above —
their services, business type, goals, meeting decisions, and brand guidelines.
Include all core pages, CMS collections, SEO sub-pages, and legal pages
appropriate for this client's business model and scale.
{f'''
REVISION REQUEST — This is a regeneration based on feedback from the previous run.
Apply this feedback when generating the new sitemap:

{revision_notes}

Do NOT simply reproduce the previous sitemap. Use the feedback above to meaningfully
revise the page structure, types, or scope.
''' if revision_notes else ''}"""

        response = await self.anthropic.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        raw_output = response.content[0].text if response.content else ""
        self.log.info(f"Claude response: {response.usage.output_tokens} output tokens")

        # ── Step 3: Parse JSON ────────────────────────────────────────────────
        try:
            clean = re.sub(r"```(?:json)?\n?", "", raw_output).strip()
            data: dict = json.loads(clean)
        except json.JSONDecodeError as e:
            self.log.error(f"JSON parse failed: {e}\n{raw_output[:500]}")
            raise AgentError(f"SitemapAgent: JSON parse failed — {e}") from e

        pages = data.get("pages", [])
        self.log.info(f"Generated {len(pages)} pages")

        # ── Step 4: Write each page to Sitemap DB (Pass 1 — create pages) ────
        # Collect parent_slug per page; Parent Page relation is set in Pass 2
        # once every page exists and we can resolve slugs → notion IDs.
        created_ids: list[str] = []
        first_entry_id: str | None = None
        slug_to_id: dict[str, str] = {}
        pending_parents: list[tuple[str, str]] = []  # (child_entry_id, parent_slug)

        for page in pages:
            title = page.get("title", "Untitled")
            slug = page.get("slug", "/")
            parent_slug = page.get("parent_slug")
            page_type = page.get("page_type", "Static")
            content_mode = page.get("content_mode", "AI Generated")
            section = page.get("section", "Core")
            order = page.get("order", 99)
            purpose = page.get("purpose", "")
            key_sections = page.get("key_sections", [])
            seo_notes = page.get("seo_notes", "")
            webflow_notes = page.get("webflow_notes", "")

            if page_type not in ("Static", "CMS"):
                page_type = "Static"
            if content_mode not in ("AI Generated", "Client Provided"):
                content_mode = "AI Generated"
            valid_sections = {"Core", "Services", "Service Subcategories", "Who We Serve",
                              "Locations", "Programs", "Patient Resources", "Blog", "Legal"}
            if section not in valid_sections:
                section = "Core"

            key_sections_text = "\n".join(f"• {s}" for s in key_sections)
            purpose_full = purpose
            if seo_notes:
                purpose_full += f"\n\nSEO: {seo_notes}"
            if webflow_notes:
                purpose_full += f"\n\nWebflow: {webflow_notes}"

            entry_id = await self.notion.create_database_entry(sitemap_db_id, {
                "Page Title": self.notion.title_property(title),
                "Slug": self.notion.text_property(slug),
                "Page Type": self.notion.select_property(page_type),
                "Content Mode": self.notion.select_property(content_mode),
                "Section": self.notion.select_property(section),
                "Status": self.notion.select_property("Draft"),
                "Purpose": self.notion.text_property(purpose_full[:2000]),
                "Key Sections": self.notion.text_property(key_sections_text[:2000]),
                "Order": {"number": order},
            })

            if first_entry_id is None:
                first_entry_id = entry_id

            created_ids.append(entry_id)
            slug_to_id[slug] = entry_id
            if parent_slug:
                pending_parents.append((entry_id, parent_slug))
            self.log.info(f"  ✓ [{page_type}/{content_mode[:2]}] {title} ({slug})")

        # ── Step 4b: Pass 2 — resolve Parent Page relations ──────────────────
        if pending_parents:
            resolved = 0
            unresolved: list[str] = []
            for child_id, parent_slug in pending_parents:
                parent_id = slug_to_id.get(parent_slug)
                if not parent_id:
                    unresolved.append(parent_slug)
                    continue
                await self.notion._client.request(
                    path=f"pages/{child_id}", method="PATCH",
                    body={"properties": {
                        "Parent Page": {"relation": [{"id": parent_id}]}
                    }},
                )
                resolved += 1
            self.log.info(
                f"  ✓ Parent Page relations set: {resolved}/{len(pending_parents)}"
            )
            if unresolved:
                self.log.warning(
                    f"  ⚠ Unresolved parent slugs (no matching page): {unresolved}"
                )

        # ── Step 5: Append SEO strategy + CMS collections to first entry ──────
        if first_entry_id:
            seo = data.get("seo_strategy", {})
            cms = data.get("cms_collections", [])
            notes = data.get("notes", "")
            summary_blocks = _summary_blocks(seo, cms, notes)
            for i in range(0, len(summary_blocks), 90):
                await self.notion.append_blocks(first_entry_id, summary_blocks[i:i + 90])
            self.log.info("Appended SEO strategy + CMS schemas to sitemap")

        return {
            "status": "success",
            "stage": PipelineStage.SITEMAP_DRAFT.value,
            "pages_created": len(created_ids),
            "notion_entry_ids": created_ids,
            "cms_collections": len(data.get("cms_collections", [])),
        }

    async def _execute_tool(self, tool_name: str, tool_input: dict) -> str:
        raise NotImplementedError(
            f"SitemapAgent uses direct API calls. Tool {tool_name} not dispatched."
        )
