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
