"""
MoodBoardAgent — DEPRECATED (2026-04-08)

This agent is no longer part of the standard pipeline. Visual direction is now
handled via Relume's built-in Style Guide (colors, fonts, spacing), which applies
directly to components with no translation gap.

Brand voice and tone data continues to flow through:
  TranscriptParser → Brand Guidelines DB → ContentAgent

The mood board step remains available as an optional step for clients who need
early visual alignment before committing to a direction, but is no longer a
required pipeline stage.

Code is kept for reference. Do not call this agent in new pipeline runs.

--- Original docstring below ---

MoodBoardAgent — Stage 3: MOOD_BOARD_DRAFT (approval gate)

Reads all client context from Notion, generates 4 distinct mood board variation
briefs using Claude, runs each through a best-practices review for telehealth/
medical web design, and writes everything to Notion.

Input kwargs:
  - client_info_db_id (str): Notion DB ID for Client Info
  - meeting_notes_db_id (str): Notion DB ID for Meeting Notes
  - brand_guidelines_db_id (str): Notion DB ID for Brand Guidelines
  - mood_board_db_id (str): Notion DB ID where variations will be created

Output:
  - 4 entries in Mood Board DB (Option A–D), each with style keywords,
    color palette description, and client feedback field ready for approval
  - Rich page body appended to each entry with full brief + best-practices notes
  - Returns dict with status, variation IDs, and recommendation
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..models.pipeline import PipelineStage
from .base_agent import AgentError, BaseAgent
from .tools import MOOD_BOARD_TOOLS

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a senior brand strategist and UX director at a digital marketing agency
(RxMedia) specializing in telehealth and medical practices.

Your task: generate 4 distinct mood board variation briefs for a telehealth
website. Each variation should be meaningfully different — not just color swaps
— representing a different creative strategy.

You have deep knowledge of:
- Healthcare and telehealth web design best practices (trust, credibility,
  accessibility, patient journey)
- Female-skewing wellness/beauty brands (Hims & Hers feminine line, Curology,
  Honeydew, Everlywell, Ro, Parsley Health)
- Conversion-optimized design for appointment booking
- WCAG accessibility standards for medical sites
- Current web typography trends (variable fonts, pairing strategies)
- Color psychology in healthcare (teal = trust + calm, blush = approachable,
  deep navy = authority)

For each variation return a JSON object in this exact structure:

{
  "variations": [
    {
      "option": "Option A",
      "concept_name": "Short evocative name (e.g. 'Boutique Clinical')",
      "concept_description": "2 sentences max. Core creative idea and the emotion it evokes.",
      "color_palette": {
        "primary": "#HEX — name and usage",
        "secondary": "#HEX — name and usage",
        "accent": "#HEX — name and usage",
        "background": "#HEX — name and usage",
        "text_primary": "#HEX",
        "text_secondary": "#HEX"
      },
      "typography": {
        "primary_font": "Font name (Google Fonts preferred) — usage",
        "secondary_font": "Font name — usage",
        "pairing_rationale": "One sentence on why these fonts work together"
      },
      "imagery_style": "1-2 sentences. Lighting, subject matter, mood.",
      "layout_principles": "2 key layout principles for this variation",
      "sample_headline": "A real, usable homepage hero headline",
      "reference_aesthetics": [
        "Site name — specific element to reference",
        "Site name — specific element to reference"
      ],
      "target_audience_fit": "One sentence on why this appeals to the client's target demographic",
      "best_practices_score": {
        "trust_credibility": "N/10 — 5 words",
        "conversion_optimization": "N/10 — 5 words",
        "accessibility": "N/10 — 5 words",
        "brand_alignment": "N/10 — 5 words",
        "differentiation": "N/10 — 5 words"
      },
      "strengths": ["Strength 1", "Strength 2"],
      "risks": ["Risk 1"],
      "recommended_for": "One sentence on best client/situation fit"
    }
  ],
  "recommendation": {
    "top_pick": "Option X",
    "rationale": "1-2 sentences on why this is strongest for this client",
    "runner_up": "Option Y",
    "runner_up_rationale": "One sentence on why worth presenting",
    "synthesis_opportunity": "One sentence on what could be combined from different options"
  },
  "best_practices_summary": {
    "telehealth_trust_signals": "1-2 sentences on must-have trust elements for telehealth conversion",
    "female_audience_design": "1-2 sentences on design principles for female-skewing wellness brands",
    "booking_conversion": "1-2 sentences on what drives appointment bookings",
    "competitive_gap": "1-2 sentences on what this client can do better than competitors"
  }
}

Rules:
- Make the 4 variations genuinely distinct strategies, not just palette swaps
- All hex codes must be real, specific, and purposeful — no generic placeholders
- The recommendation section must be decisive and justified
- Only return the JSON object — no markdown fences, no commentary
"""


def _blocks_to_text(blocks: list[dict]) -> str:
    """Extract plain text from Notion block objects."""
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


def _variation_blocks(var: dict, bps_summary: dict) -> list[dict]:
    """Build Notion page blocks for one mood board variation."""

    def h(text: str, level: int = 2) -> dict:
        ht = f"heading_{level}"
        return {"object": "block", "type": ht, ht: {
            "rich_text": [{"type": "text", "text": {"content": text}}]
        }}

    def p(text: str) -> dict:
        # Chunk if over 2000 chars
        text = text[:1900]
        return {"object": "block", "type": "paragraph", "paragraph": {
            "rich_text": [{"type": "text", "text": {"content": text}}]
        }}

    def bullet(text: str) -> dict:
        text = text[:1900]
        return {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {
            "rich_text": [{"type": "text", "text": {"content": text}}]
        }}

    cp = var.get("color_palette", {})
    ty = var.get("typography", {})
    bps = var.get("best_practices_score", {})

    blocks = [
        h(f"{var.get('option')} — {var.get('concept_name')}", 2),
        p(var.get("concept_description", "")),

        h("Color Palette", 3),
        bullet(f"Primary: {cp.get('primary', '')}"),
        bullet(f"Secondary: {cp.get('secondary', '')}"),
        bullet(f"Accent: {cp.get('accent', '')}"),
        bullet(f"Background: {cp.get('background', '')}"),
        bullet(f"Text: {cp.get('text_primary', '')} / {cp.get('text_secondary', '')}"),

        h("Typography", 3),
        bullet(f"Primary: {ty.get('primary_font', '')}"),
        bullet(f"Secondary: {ty.get('secondary_font', '')}"),
        p(ty.get("pairing_rationale", "")),

        h("Imagery & Layout", 3),
        p(var.get("imagery_style", "")),
        p(var.get("layout_principles", "")),

        h("Sample Hero Headline", 3),
        p(f'"{var.get("sample_headline", "")}"'),

        h("Reference Aesthetics", 3),
        *[bullet(ref) for ref in var.get("reference_aesthetics", [])],

        h("Target Audience Fit", 3),
        p(var.get("target_audience_fit", "")),

        h("Best Practices Scores", 3),
        bullet(f"Trust & Credibility: {bps.get('trust_credibility', '')}"),
        bullet(f"Conversion Optimization: {bps.get('conversion_optimization', '')}"),
        bullet(f"Accessibility: {bps.get('accessibility', '')}"),
        bullet(f"Brand Alignment: {bps.get('brand_alignment', '')}"),
        bullet(f"Differentiation: {bps.get('differentiation', '')}"),

        h("Strengths", 3),
        *[bullet(f"✓ {s}") for s in var.get("strengths", [])],

        h("Risks", 3),
        *[bullet(f"⚠ {r}") for r in var.get("risks", [])],

        h("Best Suited For", 3),
        p(var.get("recommended_for", "")),
    ]

    return blocks


def _recommendation_blocks(rec: dict, bps: dict) -> list[dict]:
    """Build Notion blocks for the recommendation + best practices summary."""

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

    return [
        h("── Agency Recommendation ──", 2),
        bullet(f"Top Pick: {rec.get('top_pick', '')}"),
        p(rec.get("rationale", "")),
        bullet(f"Runner-Up: {rec.get('runner_up', '')}"),
        p(rec.get("runner_up_rationale", "")),

        h("Hybrid Synthesis Opportunity", 3),
        p(rec.get("synthesis_opportunity", "")),

        h("── Best Practices Reference ──", 2),

        h("Telehealth Trust Signals", 3),
        p(bps.get("telehealth_trust_signals", "")),

        h("Female Audience Design Principles", 3),
        p(bps.get("female_audience_design", "")),

        h("Booking Conversion Drivers", 3),
        p(bps.get("booking_conversion", "")),

        h("Competitive Gap vs. Key Competitors", 3),
        p(bps.get("competitive_gap", "")),
    ]


class MoodBoardAgent(BaseAgent):
    """Generates 4 mood board variations with best-practices review."""

    name = "mood_board"
    tools = MOOD_BOARD_TOOLS

    async def run(self, client_id: str, **kwargs: Any) -> dict:
        """
        Generate mood board variations for a client.

        Required kwargs:
          - client_info_db_id
          - meeting_notes_db_id
          - brand_guidelines_db_id
          - mood_board_db_id

        Optional kwargs:
          - revision_notes (str): feedback from previous run to guide regeneration
        """
        client_info_db_id = kwargs["client_info_db_id"]
        meeting_notes_db_id = kwargs["meeting_notes_db_id"]
        brand_guidelines_db_id = kwargs["brand_guidelines_db_id"]
        mood_board_db_id = kwargs["mood_board_db_id"]
        revision_notes = kwargs.get("revision_notes", "")

        self.log.info(f"MoodBoardAgent starting | client={client_id}")

        # ── Step 1: Gather all client context from Notion ─────────────────────

        # Client Info
        client_entries = await self.notion.query_database(client_info_db_id)
        client_props = client_entries[0]["properties"] if client_entries else {}
        client_id_str = _get_rich_text(client_props.get("Company", {})) or client_id
        business_type = _get_select(client_props.get("Business Type", {}))
        client_notes = _get_rich_text(client_props.get("Notes", {}))
        pipeline_stage = _get_select(client_props.get("Pipeline Stage", {}))

        self.log.info(f"Client context loaded: {client_id_str} ({business_type})")

        # Brand Guidelines
        brand_entries = await self.notion.query_database(brand_guidelines_db_id)
        brand_context = ""
        if brand_entries:
            bp = brand_entries[0]["properties"]
            brand_page_id = brand_entries[0]["id"]
            parts = []
            for field in ["Primary Color", "Secondary Color", "Accent Color",
                          "Primary Font", "Tone Descriptors", "Inspiration URLs", "Raw Guidelines"]:
                val = _get_rich_text(bp.get(field, {}))
                if val:
                    parts.append(f"{field}: {val}")
            brand_context = "\n".join(parts)

            # Also get the full brand doc from page blocks
            brand_blocks = await self.notion.get_block_children(brand_page_id)
            brand_body = _blocks_to_text(brand_blocks)
            if brand_body:
                brand_context += f"\n\nFULL BRAND DOCUMENT:\n{brand_body[:8000]}"

        self.log.info("Brand guidelines loaded")

        # Meeting Notes — find most recent parsed entry
        meeting_entries = await self.notion.query_database(meeting_notes_db_id)
        meeting_context = ""
        if meeting_entries:
            # Get the most recently created parsed entry
            parsed_entries = [
                e for e in meeting_entries
                if e["properties"].get("Parsed", {}).get("checkbox", False)
            ]
            target = parsed_entries[0] if parsed_entries else meeting_entries[0]
            mp = target["properties"]
            meeting_page_id = target["id"]

            key_decisions = _get_rich_text(mp.get("Key Decisions", {}))
            meeting_context = f"KEY DECISIONS FROM MEETING:\n{key_decisions}"

            # Get the full AI-parsed analysis from page blocks
            meeting_blocks = await self.notion.get_block_children(meeting_page_id)
            meeting_body = _blocks_to_text(meeting_blocks)
            if meeting_body:
                # Find the "AI-Parsed Meeting Analysis" section
                analysis_start = meeting_body.find("AI-Parsed Meeting Analysis")
                if analysis_start != -1:
                    meeting_context += f"\n\nFULL PARSED ANALYSIS:\n{meeting_body[analysis_start:analysis_start+6000]}"
                else:
                    meeting_context += f"\n\nMEETING NOTES:\n{meeting_body[:4000]}"

        self.log.info("Meeting notes loaded")

        # ── Step 2: Build prompt and call Claude ──────────────────────────────
        user_message = f"""CLIENT: {client_id_str}
BUSINESS TYPE: {business_type}
PIPELINE STAGE: {pipeline_stage}

ONBOARDING NOTES:
{client_notes}

BRAND GUIDELINES:
{brand_context[:6000]}

{meeting_context[:5000]}

Generate 4 mood board variation briefs for this client's website.
Use ALL of the brand guidelines, meeting notes, and onboarding context above
to inform the creative direction — colors, fonts, tone, target audience,
inspiration brands, and any stated preferences or rejections are all there.

Make the 4 options meaningfully distinct — give the client a real creative
choice, not just color variations of the same layout.
{f'''
REVISION REQUEST — This is a regeneration based on feedback from the previous run.
Apply this feedback when generating the new variations:

{revision_notes}

Do NOT simply reproduce the previous options. Use the feedback above to meaningfully
improve or redirect the creative direction.
''' if revision_notes else ''}"""

        self.log.info("Sending context to Claude for mood board generation...")
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
            # Sometimes Claude wraps in outer braces differently — try direct parse
            data: dict = json.loads(clean)
        except json.JSONDecodeError as e:
            self.log.error(f"JSON parse failed: {e}\nRaw output (first 500):\n{raw_output[:500]}")
            raise AgentError(f"MoodBoardAgent: JSON parse failed — {e}") from e

        variations = data.get("variations", [])
        recommendation = data.get("recommendation", {})
        bps_summary = data.get("best_practices_summary", {})

        self.log.info(f"Generated {len(variations)} mood board variations")

        # ── Step 4: Write each variation to Notion Mood Board DB ──────────────
        option_map = {"Option A": "Option A", "Option B": "Option B",
                      "Option C": "Option C", "Option D": "Option D",
                      "Option E": "Option E", "Option F": "Option F"}

        created_ids: list[str] = []
        top_pick = recommendation.get("top_pick", "")

        for var in variations:
            option = var.get("option", "Option A")
            concept = var.get("concept_name", "")
            cp = var.get("color_palette", {})

            # Summary of color palette for the DB field
            palette_desc = (
                f"Primary: {cp.get('primary', '')} | "
                f"Secondary: {cp.get('secondary', '')} | "
                f"Accent: {cp.get('accent', '')} | "
                f"BG: {cp.get('background', '')}"
            )[:2000]

            style_keywords = ", ".join([
                var.get("typography", {}).get("primary_font", ""),
                var.get("concept_name", ""),
                *var.get("strengths", [])[:2],
            ])[:2000]

            # Mark the recommended option as "Pending Review", others as "Draft"
            status = "Pending Review" if option == top_pick else "Draft"
            notion_option = option_map.get(option, "Option A")

            entry_id = await self.notion.create_database_entry(mood_board_db_id, {
                "Name": self.notion.title_property(f"{option} — {concept}"),
                "Variation": self.notion.select_property(notion_option),
                "Status": self.notion.select_property(status),
                "Style Keywords": self.notion.text_property(style_keywords),
                "Color Palette Description": self.notion.text_property(palette_desc),
                "Visual References": self.notion.text_property(
                    " | ".join(var.get("reference_aesthetics", []))[:2000]
                ),
            })

            # Append full brief as page content
            var_blocks = _variation_blocks(var, bps_summary)
            for i in range(0, len(var_blocks), 90):
                await self.notion.append_blocks(entry_id, var_blocks[i:i + 90])

            created_ids.append(entry_id)
            self.log.info(f"  ✓ {option} ({concept}) → {entry_id} [{status}]")

        # ── Step 5: Append recommendation + best practices to first variation ─
        # (or we could create a separate summary page — attaching to Option A for now)
        if created_ids:
            rec_blocks = _recommendation_blocks(recommendation, bps_summary)
            await self.notion.append_blocks(created_ids[0], rec_blocks)
            self.log.info("Appended recommendation + best practices summary")

        return {
            "status": "success",
            "stage": PipelineStage.MOOD_BOARD_DRAFT.value,
            "variations_created": len(created_ids),
            "notion_entry_ids": created_ids,
            "top_pick": top_pick,
            "runner_up": recommendation.get("runner_up", ""),
            "recommendation": recommendation.get("rationale", ""),
        }

    async def _execute_tool(self, tool_name: str, tool_input: dict) -> str:
        raise NotImplementedError(
            f"MoodBoardAgent uses direct API calls. Tool {tool_name} not dispatched."
        )
