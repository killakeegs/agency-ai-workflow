"""
TranscriptParserAgent — Stage 2: MEETING_COMPLETE

Reads the raw Gemini transcript from Notion, calls Claude to extract structured
client intelligence, then writes the results back to Notion and creates action
items in the Action Items database.

Input (kwargs):
  - meeting_notes_entry_id (str): Notion page ID of the Meeting Notes entry
  - client_info_db_id (str): Notion database ID of the Client Info DB
  - action_items_db_id (str): Notion database ID for writing action items

Output:
  - Meeting Notes entry: Key Decisions + Action Items Count + Parsed=True updated
  - Meeting Notes page body: structured analysis appended as blocks
  - Action Items DB: one entry per extracted action item
  - Returns dict with "status", "stage", "action_items_count", "notion_page_id"
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..models.pipeline import PipelineStage
from .base_agent import AgentError, BaseAgent
from .tools import TRANSCRIPT_PARSER_TOOLS

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are an expert meeting analyst for a digital marketing agency (RxMedia).
Your job is to read a kickoff or review meeting transcript and extract structured
client intelligence that will drive the rest of the project.

You MUST return a single valid JSON object with exactly this structure:
{
  "key_decisions": [
    "Short, specific decision (e.g., 'Font: Quicksand selected')",
    ...
  ],
  "design_preferences": {
    "likes": ["Things the client explicitly likes"],
    "dislikes": ["Things the client explicitly does NOT want"],
    "fonts": ["Font names mentioned"],
    "colors": ["Specific colors / color descriptions mentioned"],
    "style_direction": "One sentence describing the visual direction"
  },
  "brand_signals": {
    "tone": "Adjectives describing the brand tone of voice",
    "target_audience": "Who the client wants to reach",
    "competitive_positioning": "How they want to stand out vs competitors",
    "key_differentiators": ["Unique selling points mentioned"]
  },
  "open_questions": [
    "Unresolved questions that need follow-up"
  ],
  "action_items": [
    {
      "task": "Clear, actionable task description",
      "assignee": "Agency" or "Client",
      "priority": "High" or "Normal"
    }
  ],
  "meeting_summary": "2-3 sentence summary of what was decided and what comes next"
}

Rules:
- Be specific and concrete — avoid vague statements
- action items must be actual next steps, not generic
- key_decisions should be things that directly affect the website build
- Only include information explicitly stated in the transcript
- Return ONLY the JSON object — no markdown, no explanation, no preamble
"""


def _blocks_to_text(blocks: list[dict]) -> str:
    """Extract plain text from Notion block objects."""
    lines = []
    for block in blocks:
        block_type = block.get("type", "")
        content = block.get(block_type, {})
        rich_text = content.get("rich_text", [])
        text = "".join(segment.get("text", {}).get("content", "") for segment in rich_text)
        if text:
            lines.append(text)
    return "\n".join(lines)


def _build_parsed_blocks(parsed: dict) -> list[dict]:
    """Convert parsed JSON into Notion blocks for appending to the meeting page."""

    def heading(text: str, level: int = 2) -> dict:
        ht = f"heading_{level}"
        return {"object": "block", "type": ht, ht: {
            "rich_text": [{"type": "text", "text": {"content": text}}]
        }}

    def bullet(text: str) -> dict:
        return {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {
            "rich_text": [{"type": "text", "text": {"content": text}}]
        }}

    def paragraph(text: str) -> dict:
        return {"object": "block", "type": "paragraph", "paragraph": {
            "rich_text": [{"type": "text", "text": {"content": text}}]
        }}

    blocks = [
        heading("── AI-Parsed Meeting Analysis ──", level=2),
        paragraph(parsed.get("meeting_summary", "")),

        heading("Key Decisions", level=3),
        *[bullet(d) for d in parsed.get("key_decisions", [])],

        heading("Design Preferences", level=3),
        paragraph("Likes:"),
        *[bullet(f"✓ {item}") for item in parsed.get("design_preferences", {}).get("likes", [])],
        paragraph("Dislikes:"),
        *[bullet(f"✗ {item}") for item in parsed.get("design_preferences", {}).get("dislikes", [])],
    ]

    dp = parsed.get("design_preferences", {})
    if dp.get("fonts"):
        blocks.append(bullet(f"Fonts: {', '.join(dp['fonts'])}"))
    if dp.get("colors"):
        blocks.append(bullet(f"Colors: {', '.join(dp['colors'])}"))
    if dp.get("style_direction"):
        blocks.append(bullet(f"Style direction: {dp['style_direction']}"))

    bs = parsed.get("brand_signals", {})
    blocks += [
        heading("Brand Signals", level=3),
        bullet(f"Tone: {bs.get('tone', '')}"),
        bullet(f"Target audience: {bs.get('target_audience', '')}"),
        bullet(f"Positioning: {bs.get('competitive_positioning', '')}"),
        *[bullet(f"Differentiator: {d}") for d in bs.get("key_differentiators", [])],
    ]

    oq = parsed.get("open_questions", [])
    if oq:
        blocks += [
            heading("Open Questions", level=3),
            *[bullet(q) for q in oq],
        ]

    ai = parsed.get("action_items", [])
    if ai:
        blocks += [
            heading("Action Items", level=3),
            *[bullet(f"[{item.get('assignee', '?')}] {item.get('task', '')}") for item in ai],
        ]

    return blocks


class TranscriptParserAgent(BaseAgent):
    """Parses kickoff meeting transcripts into structured client data."""

    name = "transcript_parser"
    tools = TRANSCRIPT_PARSER_TOOLS

    async def run(self, client_id: str, **kwargs: Any) -> dict:
        """
        Parse a meeting transcript entry in Notion.

        Required kwargs:
          - meeting_notes_entry_id: Notion page ID of the Meeting Notes entry
          - action_items_db_id: Notion DB ID to write action items into

        Optional kwargs:
          - client_info_db_id: Notion DB ID to pull client context from
        """
        meeting_id = kwargs["meeting_notes_entry_id"]
        action_items_db_id = kwargs["action_items_db_id"]
        client_info_db_id = kwargs.get("client_info_db_id")

        self.log.info(f"TranscriptParserAgent starting | client={client_id} | meeting={meeting_id}")

        # ── Step 1: Fetch transcript from Notion ──────────────────────────────
        self.log.info("Fetching transcript blocks from Notion...")
        blocks = await self.notion.get_block_children(meeting_id)
        transcript_text = _blocks_to_text(blocks)

        if not transcript_text.strip():
            raise AgentError(f"No transcript text found in Notion page {meeting_id}")

        self.log.info(f"Transcript loaded: {len(transcript_text):,} chars, {len(blocks)} blocks")

        # ── Step 2: Fetch client context ──────────────────────────────────────
        client_context = ""
        if client_info_db_id:
            entries = await self.notion.query_database(client_info_db_id)
            if entries:
                props = entries[0].get("properties", {})

                def get_rich_text(prop: dict) -> str:
                    parts = prop.get("rich_text", [])
                    return "".join(p.get("text", {}).get("content", "") for p in parts)

                def get_select(prop: dict) -> str:
                    sel = prop.get("select")
                    return sel.get("name", "") if sel else ""

                company = get_rich_text(props.get("Company", {}))
                notes = get_rich_text(props.get("Notes", {}))
                business_type = get_select(props.get("Business Type", {}))
                client_context = f"Client: {company} ({business_type})\n{notes}"

        # ── Step 3: Call Claude to parse the transcript ───────────────────────
        self.log.info("Sending transcript to Claude for analysis...")

        user_message = (
            f"CLIENT CONTEXT:\n{client_context}\n\n"
            f"MEETING TRANSCRIPT:\n{transcript_text[:60000]}"  # Safety cap for context
        )

        response = await self.anthropic.messages.create(
            model=self.model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        raw_output = response.content[0].text if response.content else ""
        self.log.info(f"Claude response received | output_tokens={response.usage.output_tokens}")

        # ── Step 4: Parse JSON response ───────────────────────────────────────
        try:
            # Strip any accidental markdown fences
            clean = re.sub(r"```(?:json)?\n?", "", raw_output).strip()
            parsed: dict = json.loads(clean)
        except json.JSONDecodeError as e:
            self.log.error(f"Failed to parse Claude response as JSON: {e}\n{raw_output[:500]}")
            raise AgentError(f"Transcript parser: JSON parse failed — {e}") from e

        self.log.info(
            f"Parsed: {len(parsed.get('key_decisions', []))} decisions, "
            f"{len(parsed.get('action_items', []))} action items"
        )

        # ── Step 5: Update Meeting Notes entry properties ─────────────────────
        key_decisions_text = "\n".join(f"• {d}" for d in parsed.get("key_decisions", []))
        await self.notion.update_database_entry(meeting_id, {
            "Key Decisions": self.notion.text_property(key_decisions_text[:2000]),
            "Action Items Count": {"number": len(parsed.get("action_items", []))},
            "Parsed": self.notion.checkbox_property(True),
        })
        self.log.info("Meeting Notes entry properties updated")

        # ── Step 6: Append parsed analysis as page blocks ─────────────────────
        analysis_blocks = _build_parsed_blocks(parsed)
        # Append in chunks (Notion limit: 100 blocks per call)
        for i in range(0, len(analysis_blocks), 90):
            await self.notion.append_blocks(meeting_id, analysis_blocks[i:i + 90])
        self.log.info(f"Appended {len(analysis_blocks)} analysis blocks to meeting page")

        # ── Step 7: Create Action Items in Notion ─────────────────────────────
        created_count = 0
        for item in parsed.get("action_items", []):
            assignee = item.get("assignee", "Agency")
            if assignee not in ("Agency", "Client"):
                assignee = "Agency"

            await self.notion.create_database_entry(action_items_db_id, {
                "Name": self.notion.title_property(item.get("task", "Untitled")),
                "Assigned To": self.notion.select_property(assignee),
                "Status": self.notion.select_property("To Do"),
                "Source Meeting": self.notion.text_property(
                    kwargs.get("meeting_title", "Parsed meeting")
                ),
            })
            created_count += 1

        self.log.info(f"Created {created_count} action items in Notion")

        return {
            "status": "success",
            "stage": PipelineStage.MEETING_COMPLETE.value,
            "notion_page_id": meeting_id,
            "action_items_count": created_count,
            "key_decisions_count": len(parsed.get("key_decisions", [])),
            "parsed_data": parsed,
        }

    async def _execute_tool(self, tool_name: str, tool_input: dict) -> str:
        """
        Not used in the current direct implementation — the run() method
        fetches data directly rather than via the tool-use loop.
        Implemented here to satisfy the abstract interface.
        """
        raise NotImplementedError(
            f"TranscriptParserAgent uses direct API calls, not tool dispatch. "
            f"Tool {tool_name} was unexpectedly called."
        )
