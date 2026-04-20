"""
business_profile.py — Load and format a client's Business Profile page as context for agents.

The Business Profile is a Notion page (not a DB) with 12 universal sections
+ vertical-specific sections. Each section has:
  - H2 heading
  - Callout with prompt/description
  - Paragraph with the filled-in content

This module extracts the H2 → paragraph content pairs and formats them as
structured text any agent prompt can inject.

Usage in a script:
    from src.integrations.business_profile import load_business_profile

    profile_text = await load_business_profile(notion, cfg)
    if profile_text:
        prompt = f"... BUSINESS PROFILE:\n{profile_text}\n..."
"""
from __future__ import annotations

from src.integrations.notion import NotionClient


def _rt_to_text(rich_text: list) -> str:
    return "".join(r.get("text", {}).get("content", "") for r in rich_text or [])


async def load_business_profile(notion: NotionClient, cfg: dict) -> str:
    """
    Read a client's Business Profile Notion page and return it as structured text.

    Skips:
    - The intro callout ("This is the Business Profile for...")
    - The per-section prompt callouts ("Accrediting bodies, state licenses...")
    - Empty paragraphs
    - Dividers

    Returns the profile as:
        ## Section Name
        <content>

        ## Another Section
        <content>
    """
    page_id = cfg.get("business_profile_page_id", "")
    if not page_id:
        return ""

    try:
        blocks = await _fetch_all_blocks(notion, page_id)
    except Exception:
        return ""

    sections: list[tuple[str, list[str]]] = []  # [(heading, [paragraphs])]
    current_heading: str | None = None
    current_paras: list[str] = []

    for block in blocks:
        btype = block.get("type", "")

        if btype in ("heading_1", "heading_2", "heading_3"):
            # Flush previous section
            if current_heading and current_paras:
                sections.append((current_heading, current_paras))
            current_heading = _rt_to_text(block.get(btype, {}).get("rich_text", []))
            current_paras = []

        elif btype == "paragraph":
            text = _rt_to_text(block.get("paragraph", {}).get("rich_text", []))
            if text.strip():
                current_paras.append(text)

        elif btype == "bulleted_list_item":
            text = _rt_to_text(block.get("bulleted_list_item", {}).get("rich_text", []))
            if text.strip():
                current_paras.append(f"- {text}")

        elif btype == "numbered_list_item":
            text = _rt_to_text(block.get("numbered_list_item", {}).get("rich_text", []))
            if text.strip():
                current_paras.append(f"- {text}")

        # Skip callouts (they're the section prompts, not content), dividers, etc.

    # Flush final section
    if current_heading and current_paras:
        sections.append((current_heading, current_paras))

    # Skip sections that are just header-level without content, and the top-level
    # "Business Profile" h1 that wraps everything
    formatted = []
    for heading, paras in sections:
        if heading.strip().lower() in ("business profile",):
            continue
        if not paras:
            continue
        content = "\n".join(paras)
        formatted.append(f"## {heading}\n{content}")

    return "\n\n".join(formatted)


async def _fetch_all_blocks(notion: NotionClient, page_id: str) -> list[dict]:
    """Paginate through all blocks on a page."""
    all_blocks = []
    cursor = None
    while True:
        url = f"blocks/{page_id}/children?page_size=100"
        if cursor:
            url += f"&start_cursor={cursor}"
        resp = await notion._client.request(path=url, method="GET")
        all_blocks.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return all_blocks


# ── Populate Business Profile from meeting transcripts ─────────────────────────

_POPULATE_SYSTEM = """\
You are extracting structured facts about a client's business from a meeting transcript
and routing each fact to the correct section of their Business Profile Notion page.

The client is a healthcare practice. The Business Profile has section headings that
act as buckets for different kinds of facts. Your job: read the transcript, then for
each section, return the facts (if any) that belong under it.

Rules:
- ONLY include facts ACTUALLY stated in the transcript. Never infer or embellish.
- Each fact should be one concise sentence or short paragraph — no filler.
- Attribute facts to speakers when relevant ("Savannah confirmed..." / "Keegan proposed...").
- Deduplicate — if two facts say the same thing, keep one.
- If a section has no facts from the transcript, omit it from output entirely.
- NO scheduling or logistics in any section ("meeting at 3pm" doesn't go anywhere).
- Capture negative statements explicitly ("Crown does NOT offer detox — services start at PHP").

Return ONLY a JSON object:
{
  "sections": {
    "Section Name Exactly": [
      "fact 1",
      "fact 2"
    ]
  }
}

The section name must match one of the provided headings exactly — case and punctuation.
"""


async def populate_from_meeting(
    notion: NotionClient,
    profile_page_id: str,
    transcript: str,
    meeting_date: str,
    meeting_type: str = "Meeting",
) -> dict:
    """Extract facts from a meeting transcript and append them under the right sections
    on a client's Business Profile page.

    Returns: {"sections_updated": N, "total_facts": N, "by_section": {name: count}}
    """
    import anthropic
    import json
    import re
    from src.config import settings

    # Step 1: discover section headings on the page
    blocks = await _fetch_all_blocks(notion, profile_page_id)
    sections: list[tuple[str, str]] = []  # [(heading, block_id), ...]
    for b in blocks:
        if b.get("type") == "heading_2":
            heading = _rt_to_text(b.get("heading_2", {}).get("rich_text", []))
            if heading:
                sections.append((heading, b["id"]))

    if not sections:
        return {"sections_updated": 0, "total_facts": 0, "by_section": {}}

    section_names = [h for h, _ in sections]
    section_list_text = "\n".join(f"- {n}" for n in section_names)

    # Step 2: Claude extracts facts per section
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    prompt = f"""\
Transcript type: {meeting_type}
Date: {meeting_date}

Available Business Profile sections (use exact names):
{section_list_text}

Transcript (truncated to 15k chars):
{transcript[:15000]}

Return the JSON object as specified in the system prompt."""

    response = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=4000,
        system=_POPULATE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return {"sections_updated": 0, "total_facts": 0, "by_section": {}}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {"sections_updated": 0, "total_facts": 0, "by_section": {}}

    facts_by_section = data.get("sections", {}) or {}

    # Step 3: for each section with facts, append them to the page right after the heading
    # Using Notion's `after` parameter to insert blocks in-place.
    heading_id_by_name = {h: bid for h, bid in sections}
    by_section: dict[str, int] = {}
    total_facts = 0

    # We insert blocks "after" the heading on the PAGE (not nested under the heading).
    # Notion's API appends one child after a target block; since we insert multiple blocks
    # per section, each subsequent insert uses the previously-inserted block's id as the
    # "after" anchor — otherwise the order reverses.
    for section_name, facts in facts_by_section.items():
        if section_name not in heading_id_by_name or not facts:
            continue
        anchor_id = heading_id_by_name[section_name]

        # First: an italic "source" paragraph
        source_block = {
            "object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{
                "type": "text",
                "text": {"content": f"From {meeting_type.lower()} on {meeting_date}:"},
                "annotations": {"italic": True, "color": "gray"},
            }]},
        }
        blocks_to_add = [source_block]
        for fact in facts:
            if isinstance(fact, str) and fact.strip():
                blocks_to_add.append({
                    "object": "block", "type": "bulleted_list_item",
                    "bulleted_list_item": {"rich_text": [{
                        "type": "text", "text": {"content": fact.strip()[:1900]},
                    }]},
                })

        # Append blocks one-at-a-time with sequential `after` anchors so the order is preserved
        try:
            for blk in blocks_to_add:
                r = await notion._client.request(
                    path=f"blocks/{profile_page_id}/children",
                    method="PATCH",
                    body={"children": [blk], "after": anchor_id},
                )
                # Next block anchors to the one we just inserted
                if r.get("results"):
                    anchor_id = r["results"][0]["id"]
            by_section[section_name] = len(blocks_to_add) - 1
            total_facts += len(blocks_to_add) - 1
        except Exception as e:
            print(f"  ⚠ Failed to populate section {section_name!r}: {e}")

    return {
        "sections_updated": len(by_section),
        "total_facts": total_facts,
        "by_section": by_section,
    }
