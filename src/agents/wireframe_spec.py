"""
WireframeSpecAgent — Stage 5b: WIREFRAME_DRAFT (approval gate)

Reads the approved sitemap and content, then maps every page section to a
specific Relume component. The output is a developer-ready build blueprint:
open Relume, search the component name, drop it in, paste into Webflow.

Relume preserves native Webflow editability — no custom code, no lock-in.

Input kwargs:
  - client_info_db_id (str)
  - brand_guidelines_db_id (str)
  - sitemap_db_id (str)
  - wireframes_db_id (str): Notion Wireframes DB where specs are written
  - content_db_id (str): optional — reads H1s + section structure to inform
    component choices
  - mood_board_db_id (str): optional — reads approved creative direction

Output:
  - One Wireframes DB entry per page
  - Each entry: ordered Relume component list with customization notes
  - Status: "Draft"
  - Returns dict: status, pages_created, wireframes_db_id, notion_entry_ids
"""
from __future__ import annotations

import json
import json_repair
import logging
import re
from typing import Any

from ..models.pipeline import PipelineStage
from .base_agent import AgentError, BaseAgent
from .tools import WIREFRAME_TOOLS

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a senior UX architect at a digital marketing agency (RxMedia)
specializing in telehealth and medical practice websites built on Webflow.

Your task: generate a Relume component map for each page of the client's website.

RELUME OVERVIEW
Relume is a pre-built component library with Webflow-native components. The
developer searches the component name in Relume, drops it onto the page, then
customizes it. Components paste into Webflow as native elements — fully editable
without code. This is the bridge between the sitemap/content and the Webflow build.

RELUME COMPONENT LIBRARY (use these exact names):

Navigation:
  Navbar - 1   (logo left, links center, CTA right — standard)
  Navbar - 2   (logo left, links right — minimal)
  Navbar - 5   (logo center — editorial)

Heroes (homepage / service pages):
  Hero - 1     (centered text, no image — strong headline focus)
  Hero - 2     (left text, right image — split layout)
  Hero - 3     (left text, full-height right image — premium feel)
  Hero - 4     (centered text, background image — immersive)
  Hero - 5     (centered text, image below — clean medical)
  Hero - 14    (two images flanking centered text — boutique wellness)
  Hero - 17    (large image top, text below — editorial)

Page Headers (interior pages):
  Header - 1   (page title + breadcrumb — standard interior)
  Header - 2   (large page title, centered — bold interior)
  Header - 6   (title + subtitle + CTA — service sub-pages)

Features / Services:
  Features - 1  (3-col icon grid — services overview)
  Features - 2  (left text + right image — two-up alternating)
  Features - 3  (alternating image/text rows — detail pages)
  Features - 4  (icon list — bullets with icons)
  Features - 5  (card grid — service cards with image)
  Features - 7  (large image left, text right — hero-style feature)
  Features - 9  (numbered list — step-by-step process)
  Features - 10 (tab-based — multiple service categories)

Social Proof:
  Testimonial - 1  (single large quote — featured)
  Testimonial - 2  (quote carousel — multiple reviews)
  Testimonial - 6  (card grid — 3-col testimonial cards)
  Logo - 1         (logo/badge bar — trust signals, certifications)
  Stats - 1        (large number statistics — e.g., "500+ patients")
  Stats - 6        (stats with icons)

Content Sections:
  Layout - 1   (text left, image right — general content)
  Layout - 4   (image left, text right — general content)
  Layout - 7   (centered text — standalone text section)
  Layout - 10  (two-column text — about/info)

CTA Banners:
  CTA - 1      (centered headline + button — standard)
  CTA - 6      (left text, right button — inline)
  CTA - 26     (image background, centered — premium feel)
  CTA - 41     (card style CTA — embedded)

FAQ:
  FAQ - 1      (single column accordion)
  FAQ - 4      (two column accordion — more compact)

Team:
  Team - 1     (card grid with photo + name + title)
  Team - 3     (featured single team member — founder spotlight)
  Team - 6     (horizontal card — name, title, bio excerpt)

Blog / Content Hub:
  Blog - 1     (featured post + 3-col grid)
  Blog - 2     (3-col grid only)
  Blog - 14    (list layout — good for category pages)

Contact:
  Contact - 1  (form + contact info side by side)
  Contact - 7  (centered form — minimal)
  Contact - 11 (form + map — good for in-person locations)

Pricing:
  Pricing - 1  (3-col pricing cards)
  Pricing - 6  (comparison table)

Gallery:
  Gallery - 1  (masonry grid — before/after, lifestyle)
  Gallery - 5  (carousel)

Conditions / CMS Lists:
  CMS List - 1  (card grid from CMS collection)
  CMS List - 3  (list with image thumbnails)

Footer:
  Footer - 1   (4-col links + logo + social icons — full)
  Footer - 2   (2-col + logo — simplified)
  Footer - 4   (centered — minimal)

IMPORTANT RULES FOR COMPONENT SELECTION:
- Every page starts with Navbar and ends with Footer (don't skip these)
- Interior pages use Header components, not Hero components
- Homepage gets the most premium Hero variant
- Telehealth conversion requires: social proof near the top, clear CTA after
  every 2-3 content sections, FAQ for SEO and trust
- "Client Provided" content sections should still have components assigned —
  note what content the client needs to supply in that slot
- Keep Webflow native editability in mind — every component must be editable
  in Webflow's visual editor without code

Return a JSON object with this exact structure:
{
  "pages": [
    {
      "title": "Exact page title from sitemap",
      "slug": "/url-slug",
      "total_components": 12,
      "components": [
        {
          "order": 1,
          "component": "Navbar - 1",
          "section_name": "Navigation",
          "content_notes": "Logo left, nav links: About / Services / Blog. CTA button: 'Book Consultation'. Sticky on scroll.",
          "webflow_notes": "Set navbar background to transparent on hero, switches to white on scroll via Webflow interaction."
        }
      ],
      "page_notes": "Any special build notes or decisions the developer needs to know for this specific page"
    }
  ]
}

Rules:
- content_notes: what goes IN this component (copy, images, links). Be specific.
- webflow_notes: HOW to build it in Webflow (interactions, CMS binding, special config).
- Every page must have Navbar first and Footer last.
- component order must be sequential starting at 1.
- Only return the JSON — no markdown fences, no commentary.
"""


def _blocks_to_text(blocks: list[dict]) -> str:
    lines = []
    for block in blocks:
        block_type = block.get("type", "")
        content = block.get(block_type, {})
        rich_text = content.get("rich_text", [])
        text = "".join(
            seg.get("text", {}).get("content", "") for seg in rich_text
        )
        if text:
            lines.append(text)
    return "\n".join(lines)


def _get_rich_text(prop: dict) -> str:
    return "".join(
        p.get("text", {}).get("content", "") for p in prop.get("rich_text", [])
    )


def _get_title(prop: dict) -> str:
    return "".join(
        p.get("text", {}).get("content", "") for p in prop.get("title", [])
    )


def _get_select(prop: dict) -> str:
    sel = prop.get("select")
    return sel.get("name", "") if sel else ""


def _wireframe_blocks(page: dict) -> list[dict]:
    """Build Notion page blocks for one page's wireframe component map."""

    def h(text: str, level: int = 2) -> dict:
        ht = f"heading_{level}"
        return {"object": "block", "type": ht, ht: {
            "rich_text": [{"type": "text", "text": {"content": text[:1900]}}]
        }}

    def p(text: str) -> dict:
        return {"object": "block", "type": "paragraph", "paragraph": {
            "rich_text": [{"type": "text", "text": {"content": text[:1900]}}]
        }}

    def bullet(text: str) -> dict:
        return {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {
            "rich_text": [{"type": "text", "text": {"content": text[:1900]}}]
        }}

    def divider() -> dict:
        return {"object": "block", "type": "divider", "divider": {}}

    def callout(text: str, emoji: str = "🧱") -> dict:
        return {"object": "block", "type": "callout", "callout": {
            "rich_text": [{"type": "text", "text": {"content": text[:1900]}}],
            "icon": {"emoji": emoji},
        }}

    blocks: list[dict] = []

    # Page summary
    blocks.append(callout(
        f"{page.get('total_components', 0)} components  ·  "
        f"Slug: {page.get('slug', '/')}  ·  "
        f"Open relume.io/component-library to search each component.",
        "🧱"
    ))

    if page.get("page_notes"):
        blocks.append(h("Build Notes", 2))
        blocks.append(p(page["page_notes"]))

    blocks.append(h("Component Map", 2))
    blocks.append(divider())

    for comp in page.get("components", []):
        order = comp.get("order", "?")
        name = comp.get("component", "")
        section = comp.get("section_name", "")
        content_notes = comp.get("content_notes", "")
        webflow_notes = comp.get("webflow_notes", "")

        blocks.append(h(f"{order}. {name}  —  {section}", 3))
        if content_notes:
            blocks.append(bullet(f"Content: {content_notes}"))
        if webflow_notes:
            blocks.append(bullet(f"Webflow: {webflow_notes}"))
        blocks.append(divider())

    return blocks


class WireframeSpecAgent(BaseAgent):
    """
    Generates Relume component maps for every page in the approved sitemap.
    Output is a developer-ready build blueprint: component name + content notes
    + Webflow implementation notes, written to the Wireframes DB in Notion.
    """

    name = "wireframe_spec"
    tools = WIREFRAME_TOOLS

    async def run(self, client_id: str, **kwargs: Any) -> dict:
        """
        Generate wireframe component maps for all sitemap pages.

        Required kwargs:
          - client_info_db_id
          - brand_guidelines_db_id
          - sitemap_db_id
          - wireframes_db_id

        Optional kwargs:
          - content_db_id: reads H1s + section structure to sharpen component picks
          - mood_board_db_id: reads approved creative direction
          - revision_notes (str): feedback from previous run
        """
        client_info_db_id = kwargs["client_info_db_id"]
        brand_guidelines_db_id = kwargs["brand_guidelines_db_id"]
        sitemap_db_id = kwargs["sitemap_db_id"]
        wireframes_db_id = kwargs["wireframes_db_id"]
        content_db_id = kwargs.get("content_db_id", "")
        mood_board_db_id = kwargs.get("mood_board_db_id")
        revision_notes = kwargs.get("revision_notes", "")

        self.log.info(f"WireframeSpecAgent starting | client={client_id}")

        # ── Step 1: Gather context ─────────────────────────────────────────────

        # Client info
        client_entries = await self.notion.query_database(client_info_db_id)
        client_props = client_entries[0]["properties"] if client_entries else {}
        company = _get_rich_text(client_props.get("Company", {})) or client_id

        # Brand guidelines
        brand_context = ""
        brand_entries = await self.notion.query_database(brand_guidelines_db_id)
        if brand_entries:
            bp = brand_entries[0]["properties"]
            tone = _get_rich_text(bp.get("Tone Descriptors", {}))
            raw = _get_rich_text(bp.get("Raw Guidelines", {}))
            brand_context = f"Tone: {tone}\n{raw[:2000]}"

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
                mp = approved["properties"]
                mood_context = (
                    f"Creative direction: "
                    f"{_get_rich_text(mp.get('Style Keywords', {}))}\n"
                    f"Palette: "
                    f"{_get_rich_text(mp.get('Color Palette Description', {}))}"
                )

        # ── Step 2: Read sitemap ───────────────────────────────────────────────
        sitemap_entries = await self.notion.query_database(
            sitemap_db_id,
            sorts=[{"property": "Order", "direction": "ascending"}],
        )

        if not sitemap_entries:
            raise AgentError(
                "No sitemap pages found. Run the sitemap stage first."
            )

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
            })

        self.log.info(f"Found {len(sitemap_pages)} sitemap pages")

        # ── Step 3: Read Content DB for H1s + section headings (optional) ─────
        content_map: dict[str, dict] = {}  # slug → {h1, sections}
        if content_db_id:
            try:
                content_entries = await self.notion.query_database(content_db_id)
                for entry in content_entries:
                    ep = entry["properties"]
                    slug = _get_rich_text(ep.get("Slug", {}))
                    h1 = _get_rich_text(ep.get("H1", {}))
                    if slug:
                        content_map[slug] = {"h1": h1}
                self.log.info(
                    f"Loaded {len(content_map)} content entries for component context"
                )
            except Exception as exc:
                self.log.warning(f"Could not read Content DB (non-fatal): {exc}")

        # ── Step 4: Skip pages already in Wireframes DB ───────────────────────
        existing_entries = await self.notion.query_database(wireframes_db_id)
        already_done: set[str] = set()
        for entry in existing_entries:
            ep = entry["properties"]
            title = (
                _get_title(ep.get("Page Title", {}))
                or _get_title(ep.get("Name", {}))
                or ""
            )
            if title:
                already_done.add(title)

        pages_to_process = [
            p for p in sitemap_pages if p["title"] not in already_done
        ]

        if already_done:
            self.log.info(
                f"Skipping {len(already_done)} pages already in Wireframes DB"
            )
        self.log.info(f"Processing {len(pages_to_process)} pages")

        # ── Step 5: Generate component maps one page at a time ────────────────
        created_ids: list[str] = []
        batch_size = 1

        for batch_idx in range(0, len(pages_to_process), batch_size):
            batch = pages_to_process[batch_idx:batch_idx + batch_size]
            batch_num = batch_idx // batch_size + 1
            self.log.info(
                f"Batch {batch_num}: mapping components for "
                f"{[p['title'] for p in batch]}"
            )

            page_list = ""
            for pg in batch:
                h1 = content_map.get(pg["slug"], {}).get("h1", "")
                page_list += (
                    f"\n---\n"
                    f"Page: {pg['title']}\n"
                    f"Slug: {pg['slug']}\n"
                    f"Type: {pg['page_type']} / {pg['content_mode']}\n"
                    f"Purpose: {pg['purpose']}\n"
                    f"Sections: {pg['key_sections']}\n"
                    + (f"H1: {h1}\n" if h1 else "")
                )

            user_message = f"""CLIENT: {company}

BRAND GUIDELINES:
{brand_context[:2000]}

{f'CREATIVE DIRECTION: {mood_context}' if mood_context else ''}

Generate Relume component maps for these {len(batch)} pages:
{page_list}

{f'''REVISION REQUEST — Apply this feedback to the component choices:
{revision_notes}

''' if revision_notes else ''}Map every section to the most appropriate Relume component. Include Navbar first
and Footer last on every page. Return the JSON as specified."""

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
                try:
                    batch_data: dict = json.loads(clean)
                except json.JSONDecodeError:
                    batch_data = json_repair.repair_json(clean, return_objects=True)
            except Exception as exc:
                self.log.error(
                    f"JSON parse failed for batch {batch_num}: {exc}\n"
                    f"{raw_output[:400]}"
                )
                raise AgentError(
                    f"WireframeSpecAgent: JSON parse failed in batch {batch_num} "
                    f"— {exc}"
                ) from exc

            # ── Save each page immediately (so progress survives failures) ────
            for page_data in batch_data.get("pages", []):
                title = page_data.get("title", "Untitled")
                total_components = page_data.get("total_components", 0)
                if not isinstance(total_components, int):
                    total_components = len(page_data.get("components", []))

                entry_id = await self.notion.create_database_entry(
                    wireframes_db_id,
                    {
                        "Name": self.notion.title_property(title),
                        "Status": self.notion.select_property("Draft"),
                        "Component Count": {"number": total_components},
                    },
                )

                spec_blocks = _wireframe_blocks(page_data)
                for i in range(0, len(spec_blocks), 90):
                    await self.notion.append_blocks(
                        entry_id, spec_blocks[i:i + 90]
                    )

                created_ids.append(entry_id)
                self.log.info(
                    f"  ✓ {title} — {total_components} components → {entry_id}"
                )

        self.log.info(
            f"WireframeSpecAgent complete | {len(created_ids)} pages written to Notion"
        )

        return {
            "status": "success",
            "stage": PipelineStage.WIREFRAME_DRAFT.value,
            "pages_created": len(created_ids),
            "notion_entry_ids": created_ids,
        }

    async def _execute_tool(self, tool_name: str, tool_input: dict) -> str:
        raise NotImplementedError(
            f"WireframeSpecAgent uses direct API calls. "
            f"Tool {tool_name} not dispatched."
        )
