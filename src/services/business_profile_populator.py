"""
business_profile_populator.py — Multi-source populator for a client's Business Profile.

Reads from the client's public website (primary source), merges with whatever
facts already live on the Business Profile page (from earlier meeting / email
enrichment runs), asks Claude to route every distinct fact to the correct H2
section, appends the new facts inline, and flags any required section that
still isn't populated.

Why this exists:
- For existing clients (6+ months in), Business Profile is often thin because
  we never ran the Meeting Processor on their kickoff (it was pre-pipeline).
- For new clients, this fills the profile before kickoff so SEO / sitemap /
  content agents have real context instead of 4 lines of metadata.
- Running this against every client + filling the flagged gaps turns Business
  Profile from "artifact that grows over time" into "foundation you can build
  on today".

Sources read (v1): website only. v2 will layer in Google Places, existing
Client Log / email enrichment facts (already on the page — we dedup against
them), and linked Google Drive folders.

Output:
- New facts appended under matching H2 sections with a "From website scrape
  on {date}:" attribution (mirroring the meeting populator).
- A "🚨 Information Gaps" callout at the top of the page listing required
  sections that are still empty/thin, with guidance on what's needed.
- One flag per gap in the workspace Flags DB (dedup'd by the existing
  write_flags_to_db helper — safe to re-run).
"""
from __future__ import annotations

import json
import re
from datetime import datetime

import anthropic

from config.business_profile_requirements import (
    MIN_SECTION_CHARS,
    required_sections_for,
)
from src.config import settings
from src.integrations.business_profile import (
    _fetch_all_blocks,
    _rt_to_text,
    load_business_profile,
)
from src.integrations.notion import NotionClient
from src.integrations.website_scraper import scrape_site, summarize_scrape


GAP_CALLOUT_MARKER = "🚨 Information Gaps"


# ── Section fact extraction ──────────────────────────────────────────────────

_EXTRACT_SYSTEM = """\
You are extracting structured facts about a healthcare practice from their public
website, then routing each fact to the correct section of their Business Profile
Notion page.

The Business Profile has H2 section headings that act as buckets. Your job: read
the website content, then for each section, return the facts (if any) that belong
under it.

CRITICAL RULES — READ CAREFULLY:

1. Include ONLY facts EXPLICITLY STATED on the website. Never infer, never
   extrapolate, never embellish.

2. NEVER NEGATE BASED ON ABSENCE. If a topic isn't mentioned on the page,
   DO NOT write the opposite as a fact.
   - WRONG: insurance isn't discussed, so you write "does not accept insurance"
   - WRONG: residential isn't mentioned, so you write "does not offer residential"
   - RIGHT: omit the fact entirely — we only state what the site actually says
   Only write a negative statement ("does NOT offer X") if the site EXPLICITLY
   denies offering it.

3. Image-based content: the scraper injects [image: name] markers when pages
   contain logos/badges (e.g. "We are now in-network with [image: bcbs]").
   Treat these as PARTIAL signals, not definitive facts. For insurance logos
   specifically: list what you see as a tentative fact prefixed with
   "Site homepage shows in-network logo for: <provider>" — the team will
   verify. Do NOT extrapolate a full insurance panel from one logo.

4. If pages CONTRADICT each other (e.g. one page says PHP + IOP, another
   mentions residential), emit the fact from the more authoritative source
   (Services / Levels of Care page > homepage boilerplate) and append
   "(potential site inconsistency — other pages suggest otherwise)".

5. Each fact is one concise sentence or short paragraph. No filler.

6. Deduplicate — if two pages say the same thing, keep it once.

7. If a section has no facts from the site, omit it from output entirely.

8. Respect the existing facts already on the page — do NOT re-emit a fact
   that is already captured verbatim or substantively.

9. Section name in your output must match one of the provided headings
   exactly — case and punctuation.

Return ONLY a JSON object with this shape:

{
  "sections": {
    "Exact Section Name": [
      "fact 1",
      "fact 2"
    ]
  }
}
"""


async def _extract_facts_via_claude(
    website_text: str,
    section_headings: list[str],
    existing_profile_text: str,
) -> dict[str, list[str]]:
    """Ask Claude to route facts from website content into BP sections.
    Returns {section_name: [facts]}."""
    section_list = "\n".join(f"- {h}" for h in section_headings)

    existing_block = ""
    if existing_profile_text.strip():
        existing_block = (
            "\n\nEXISTING FACTS ALREADY ON THE BUSINESS PROFILE (do not duplicate):\n"
            + existing_profile_text[:10_000]
        )

    prompt = f"""\
Available Business Profile sections (match names exactly):
{section_list}

WEBSITE CONTENT ({len(website_text):,} chars, multi-page):
{website_text[:80_000]}
{existing_block}

Return the JSON object as specified in the system prompt. If a page URL is
mentioned, no need to cite it in the fact — just capture the fact.
"""

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    resp = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=6000,
        system=_EXTRACT_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = (resp.content[0].text or "").strip()
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    sections = data.get("sections", {}) or {}
    # Filter out empty / non-list values
    return {
        name: [f for f in facts if isinstance(f, str) and f.strip()]
        for name, facts in sections.items()
        if isinstance(facts, list) and facts
    }


# ── Section discovery on BP page ─────────────────────────────────────────────

async def _section_index(
    notion: NotionClient, page_id: str,
) -> tuple[list[tuple[str, str]], dict[str, list[str]]]:
    """Scan the page once; return:
      - section_headings: [(name, block_id)] in page order
      - section_content:  {name: [paragraph/list strings]} (for gap detection)
    """
    blocks = await _fetch_all_blocks(notion, page_id)
    section_headings: list[tuple[str, str]] = []
    section_content: dict[str, list[str]] = {}
    current_name: str | None = None

    for b in blocks:
        btype = b.get("type", "")
        if btype == "heading_2":
            name = _rt_to_text(b.get("heading_2", {}).get("rich_text", []))
            if name:
                section_headings.append((name, b["id"]))
                section_content.setdefault(name, [])
                current_name = name
            continue
        if current_name is None:
            continue
        if btype == "paragraph":
            txt = _rt_to_text(b.get("paragraph", {}).get("rich_text", []))
            if txt.strip():
                section_content[current_name].append(txt.strip())
        elif btype == "bulleted_list_item":
            txt = _rt_to_text(b.get("bulleted_list_item", {}).get("rich_text", []))
            if txt.strip():
                section_content[current_name].append(f"- {txt.strip()}")
        elif btype == "numbered_list_item":
            txt = _rt_to_text(b.get("numbered_list_item", {}).get("rich_text", []))
            if txt.strip():
                section_content[current_name].append(f"- {txt.strip()}")

    return section_headings, section_content


# ── Appending facts ──────────────────────────────────────────────────────────

async def _append_facts_under_headings(
    notion: NotionClient,
    page_id: str,
    section_headings: list[tuple[str, str]],
    facts_by_section: dict[str, list[str]],
    source_label: str,
    source_date: str,
) -> tuple[int, int, dict[str, int]]:
    """Append a source attribution + bulleted facts directly after each section's
    heading block. Uses the same in-order anchoring trick as populate_from_meeting.
    Returns (sections_updated, total_facts, {section: count})."""
    heading_id_by_name = {h: bid for h, bid in section_headings}
    by_section: dict[str, int] = {}
    total = 0

    for name, facts in facts_by_section.items():
        if name not in heading_id_by_name or not facts:
            continue
        anchor_id = heading_id_by_name[name]

        blocks: list[dict] = [{
            "object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{
                "type": "text",
                "text": {"content": f"From {source_label} on {source_date}:"},
                "annotations": {"italic": True, "color": "gray"},
            }]},
        }]
        for f in facts:
            blocks.append({
                "object": "block", "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [{
                    "type": "text", "text": {"content": f.strip()[:1900]},
                }]},
            })

        try:
            for blk in blocks:
                r = await notion._client.request(
                    path=f"blocks/{page_id}/children",
                    method="PATCH",
                    body={"children": [blk], "after": anchor_id},
                )
                if r.get("results"):
                    anchor_id = r["results"][0]["id"]
            by_section[name] = len(blocks) - 1
            total += len(blocks) - 1
        except Exception as e:
            print(f"    ⚠ Failed to populate section {name!r}: {e}")

    return len(by_section), total, by_section


# ── Gap detection ────────────────────────────────────────────────────────────

def detect_gaps(
    verticals: list[str],
    section_content: dict[str, list[str]],
) -> list[dict]:
    """Compare populated sections against the per-vertical required list.
    Returns a list of gap dicts: [{section, severity, description}].

    severity: 'empty' — no content at all
              'thin'  — content exists but under MIN_SECTION_CHARS"""
    required = required_sections_for(verticals)
    gaps: list[dict] = []
    for name in required:
        lines = section_content.get(name, [])
        joined = "\n".join(lines).strip()
        if not joined:
            gaps.append({
                "section": name,
                "severity": "empty",
                "description": (
                    f"Business Profile section '{name}' is empty. "
                    f"SEO pipeline needs this section populated to generate "
                    f"accurate keyword strategy for this client."
                ),
            })
        elif len(joined) < MIN_SECTION_CHARS:
            gaps.append({
                "section": name,
                "severity": "thin",
                "description": (
                    f"Business Profile section '{name}' is thin "
                    f"({len(joined)} chars). Expand with specifics — current "
                    f"content is likely too vague to drive keyword / content "
                    f"strategy decisions."
                ),
            })
    return gaps


# ── Inline gap callout on the BP page ────────────────────────────────────────

async def _find_gap_callout_block_id(
    notion: NotionClient, page_id: str,
) -> str | None:
    """Return the block_id of the existing gap callout if one exists."""
    blocks = await _fetch_all_blocks(notion, page_id)
    for b in blocks:
        if b.get("type") != "callout":
            continue
        txt = _rt_to_text(b.get("callout", {}).get("rich_text", []))
        if GAP_CALLOUT_MARKER in txt:
            return b["id"]
    return None


async def write_gap_callout(
    notion: NotionClient,
    page_id: str,
    gaps: list[dict],
) -> str:
    """Write or update the "🚨 Information Gaps" callout at the top of the
    Business Profile page. If there are no gaps, delete any existing callout.
    Returns 'created' / 'updated' / 'cleared' / 'unchanged' for logging."""
    existing_id = await _find_gap_callout_block_id(notion, page_id)

    if not gaps:
        if existing_id:
            await notion._client.request(
                path=f"blocks/{existing_id}", method="DELETE",
            )
            return "cleared"
        return "unchanged"

    # Build rich_text for the callout
    header = f"{GAP_CALLOUT_MARKER} — {len(gaps)} section(s) still need team input"
    body_lines = [header, ""]
    for g in gaps:
        marker = "•" if g["severity"] == "thin" else "◯"
        body_lines.append(f"{marker} {g['section']}  ({g['severity']})")
    body_lines.append("")
    body_lines.append(
        "Fill these sections to unblock the SEO / content pipeline for this client. "
        "Run `make populate-business-profile CLIENT=<key>` after updates to refresh."
    )
    text = "\n".join(body_lines)

    callout_block = {
        "object": "block", "type": "callout",
        "callout": {
            "rich_text": [{"type": "text", "text": {"content": text[:1990]}}],
            "icon":  {"type": "emoji", "emoji": "🚨"},
            "color": "red_background",
        },
    }

    if existing_id:
        # Notion's PATCH on block doesn't allow replacing callout icon+color cleanly.
        # Delete + recreate at the top is simpler and reliable.
        await notion._client.request(path=f"blocks/{existing_id}", method="DELETE")

    # Insert at the top — the Notion API appends children at the end by default,
    # so we anchor `after` to nothing and rely on Notion putting it after the h1.
    # Simpler: use `children` without `after` — appends at bottom. Accept that
    # the gap callout lives below the intro callout + h1 but above section h2s;
    # we put it after a known anchor = the intro callout (first callout on page).
    blocks = await _fetch_all_blocks(notion, page_id)
    anchor_id = None
    for b in blocks:
        if b.get("type") == "callout":
            anchor_id = b["id"]
            break

    body = {"children": [callout_block]}
    if anchor_id:
        body["after"] = anchor_id
    await notion._client.request(
        path=f"blocks/{page_id}/children", method="PATCH", body=body,
    )
    return "updated" if existing_id else "created"


# ── Flags DB write ───────────────────────────────────────────────────────────

async def write_gap_flags(
    notion: NotionClient,
    flags_db_id: str,
    client_name: str,
    client_key: str,
    gaps: list[dict],
) -> list[dict]:
    """Write one flag per gap. Reuses email_enrichment.write_flags_to_db for
    dedup + schema consistency."""
    if not flags_db_id or not gaps:
        return []
    # Import here to avoid circular references at module load
    from src.services.email_enrichment import write_flags_to_db

    today = datetime.now().strftime("%Y-%m-%d")
    flag_dicts = [{
        "type":        "business_profile_gap",
        "description": g["description"],
        "source_date": today,
    } for g in gaps]

    return await write_flags_to_db(
        notion, flags_db_id, client_name, client_key,
        flag_dicts, source="BusinessProfile",
    )


# ── Public entry point ───────────────────────────────────────────────────────

async def populate_from_website(
    notion: NotionClient,
    cfg: dict,
    dry_run: bool = False,
) -> dict:
    """
    End-to-end: scrape client website → Claude extracts facts → append under
    matching H2 sections → detect gaps → write gap callout + Flags DB entries.

    Returns a summary dict for the CLI to print.
    """
    client_key  = cfg.get("client_id") or cfg.get("client_key", "")
    client_name = cfg.get("name", client_key)
    page_id     = cfg.get("business_profile_page_id", "")
    verticals   = cfg.get("vertical") or []
    if isinstance(verticals, str):
        verticals = [verticals]

    website_url = cfg.get("website") or cfg.get("gsc_site_url") or ""
    if not website_url:
        return {"status": "skipped", "reason": "no website URL in config (set 'website' or 'gsc_site_url')"}

    if not page_id:
        return {"status": "skipped", "reason": "no business_profile_page_id in config"}

    print(f"  [1/6] Scrape {website_url} ...")
    pages = await scrape_site(website_url)
    print(f"        → {len(pages)} page(s) fetched, "
          f"{sum(len(t) for t in pages.values()):,} chars total")

    website_text = summarize_scrape(pages)
    if not website_text.strip():
        return {"status": "failed", "reason": "scrape returned no content"}

    print("  [2/6] Read current Business Profile sections + existing content ...")
    section_headings, section_content = await _section_index(notion, page_id)
    existing_profile_text = await load_business_profile(notion, cfg)
    print(f"        → {len(section_headings)} section headings found")

    if not section_headings:
        return {"status": "failed", "reason": "no H2 sections on Business Profile page"}

    print("  [3/6] Claude routes facts → sections ...")
    facts_by_section = await _extract_facts_via_claude(
        website_text=website_text,
        section_headings=[h for h, _ in section_headings],
        existing_profile_text=existing_profile_text,
    )
    fact_count = sum(len(v) for v in facts_by_section.values())
    print(f"        → {len(facts_by_section)} sections / {fact_count} new facts")

    if dry_run:
        print("  [DRY RUN — skipping all writes]")
        for name, facts in facts_by_section.items():
            print(f"    Section: {name}  ({len(facts)} fact(s))")
            for f in facts:
                print(f"      • {f[:140]}")
        # Still run gap detection against CURRENT state (pre-facts) for preview
        gaps_preview = detect_gaps(verticals, section_content)
        if gaps_preview:
            print(f"\n  [4/6] Gaps detected (pre-write): {len(gaps_preview)}")
            for g in gaps_preview:
                print(f"    ◯ {g['section']}  ({g['severity']})")
        return {
            "status": "dry_run",
            "pages_scraped": len(pages),
            "sections_would_update": len(facts_by_section),
            "facts_would_add": fact_count,
            "gaps_preview": gaps_preview,
        }

    print("  [4/6] Append facts under matching H2 sections ...")
    source_label = "website scrape"
    source_date  = datetime.now().strftime("%Y-%m-%d")
    sections_updated, facts_added, by_section = await _append_facts_under_headings(
        notion, page_id, section_headings, facts_by_section, source_label, source_date,
    )
    for name, count in by_section.items():
        print(f"        ✓ {name}: +{count}")

    # Re-read the page after appending so gap detection sees the new state
    print("  [5/6] Re-scan page + detect gaps against per-vertical requirements ...")
    _, section_content_post = await _section_index(notion, page_id)
    gaps = detect_gaps(verticals, section_content_post)

    print(f"  [6/6] Write gap callout + Flags DB entries ({len(gaps)} gap(s)) ...")
    callout_action = await write_gap_callout(notion, page_id, gaps)
    print(f"        Callout: {callout_action}")

    import os
    flags_db_id = os.environ.get("NOTION_FLAGS_DB_ID", "").strip()
    flags_created = await write_gap_flags(
        notion, flags_db_id, client_name, client_key, gaps,
    ) if flags_db_id else []
    print(f"        Flags DB: {len(flags_created)} new gap flag(s) written")

    return {
        "status":            "ok",
        "pages_scraped":     len(pages),
        "sections_updated":  sections_updated,
        "facts_added":       facts_added,
        "by_section":        by_section,
        "gaps_remaining":    gaps,
        "flags_created":     len(flags_created),
        "callout_action":    callout_action,
    }
