#!/usr/bin/env python3
"""
battle_plan.py — Generate the SEO Battle Plan for a client

Reads client data from Notion, collects available automated metrics, and
uses Claude to generate the full battle plan narrative. Writes everything
back to Notion — competitor rows, keyword rows, and a Battle Plan document
page — then posts a summary to Slack.

Usage:
    make battle-plan CLIENT=summit_therapy
    make battle-plan CLIENT=summit_therapy NOTES="focus on LGBTQ+ keywords"

What gets auto-pulled:
  - Client business info, brand, and goals from Notion
  - PageSpeed scores (from existing Care Plan DB if available)

What team provides (via Notion Battle Plan Input page, created by this script):
  - Competitor list with websites
  - Search Atlas export: keyword volumes, current rankings, DA, referring domains
  - LLM visibility check (Gemini/ChatGPT/Perplexity)
  - GBP baseline metrics (until GBP API is connected)

What Claude generates:
  - Executive summary and gap analysis
  - 4-phase strategic action plan
  - Keyword cluster strategy
  - Competitor strength/weakness narrative
  - Authority gap analysis
  - Success milestones

Run with --init to create a Battle Plan Input page in Notion for the team
to fill in before the full run.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings
from src.integrations.notion import NotionClient

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

try:
    import anthropic
    _anthropic = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
except Exception:
    _anthropic = None


# ── Notion helpers ─────────────────────────────────────────────────────────────

def _rt(text: str) -> dict:
    """Rich text property."""
    return {"rich_text": [{"text": {"content": str(text)[:2000]}}]}

def _title(text: str) -> dict:
    return {"title": [{"text": {"content": str(text)[:500]}}]}

def _sel(name: str) -> dict:
    return {"select": {"name": name}}

def _num(val: float | int | None) -> dict:
    return {"number": val}

def _url(val: str) -> dict:
    return {"url": val or None}

def _check(val: bool) -> dict:
    return {"checkbox": val}

def _get_rt(prop: dict) -> str:
    if not prop:
        return ""
    return "".join(p.get("text", {}).get("content", "") for p in prop.get("rich_text", []))

def _get_title_text(prop: dict) -> str:
    if not prop:
        return ""
    return "".join(p.get("text", {}).get("content", "") for p in prop.get("title", []))

def _get_num(prop: dict) -> float | None:
    if not prop:
        return None
    return prop.get("number")

def _get_url_val(prop: dict) -> str:
    if not prop:
        return ""
    return prop.get("url", "") or ""

def _get_select(prop: dict) -> str:
    if not prop:
        return ""
    sel = prop.get("select")
    return sel.get("name", "") if sel else ""


# ── Block builders ─────────────────────────────────────────────────────────────

def _h(text: str, level: int = 2) -> dict:
    ht = f"heading_{level}"
    return {"object": "block", "type": ht, ht: {
        "rich_text": [{"type": "text", "text": {"content": text}}]
    }}

def _p(text: str) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {
        "rich_text": [{"type": "text", "text": {"content": str(text)[:2000]}}]
    }}

def _bullet(text: str) -> dict:
    return {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {
        "rich_text": [{"type": "text", "text": {"content": str(text)[:2000]}}]
    }}

def _divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}

def _callout(text: str, emoji: str = "⚠️") -> dict:
    return {"object": "block", "type": "callout", "callout": {
        "rich_text": [{"type": "text", "text": {"content": str(text)[:2000]}}],
        "icon": {"type": "emoji", "emoji": emoji},
        "color": "yellow_background",
    }}


# ── Data loading ───────────────────────────────────────────────────────────────

async def _load_client_context(notion: NotionClient, cfg: dict) -> dict:
    """Pull all relevant client data from Notion."""
    ctx: dict[str, Any] = {}

    # Client Info
    try:
        entries = await notion.query_database(cfg["client_info_db_id"])
        if entries:
            props = entries[0].get("properties", {})
            ctx["business_name"] = _get_rt(props.get("Company", {}))
            ctx["website"] = _get_url_val(props.get("Website", {}))
            ctx["pipeline_stage"] = _get_select(props.get("Pipeline Stage", {}))
    except Exception as e:
        log.warning(f"Could not load Client Info: {e}")

    # Brand Guidelines
    try:
        entries = await notion.query_database(cfg["brand_guidelines_db_id"])
        if entries:
            props = entries[0].get("properties", {})
            ctx["tone"] = _get_rt(props.get("Tone Descriptors", {}))
            ctx["raw_guidelines"] = _get_rt(props.get("Raw Guidelines", {}))
    except Exception as e:
        log.warning(f"Could not load Brand Guidelines: {e}")

    # Latest PageSpeed from Care Plan DB (if available)
    care_plan_db_id = cfg.get("care_plan_db_id", "")
    if care_plan_db_id:
        try:
            entries = await notion.query_database(care_plan_db_id)
            if entries:
                props = entries[0].get("properties", {})
                ctx["pagespeed_mobile"] = _get_num(props.get("Mobile Score", {}))
                ctx["pagespeed_desktop"] = _get_num(props.get("Desktop Score", {}))
        except Exception as e:
            log.warning(f"Could not load Care Plan data: {e}")

    # Existing competitors (if any rows already exist)
    competitors_db_id = cfg.get("competitors_db_id", "")
    if competitors_db_id:
        try:
            rows = await notion.query_database(competitors_db_id)
            ctx["existing_competitors"] = [
                {
                    "name": _get_title_text(r["properties"].get("Competitor Name", {})),
                    "website": _get_url_val(r["properties"].get("Website", {})),
                    "review_count": _get_num(r["properties"].get("Review Count", {})),
                    "rating": _get_num(r["properties"].get("Review Rating", {})),
                    "authority_score": _get_num(r["properties"].get("Authority Score", {})),
                    "referring_domains": _get_num(r["properties"].get("Referring Domains", {})),
                    "strengths": _get_rt(r["properties"].get("Strengths", {})),
                    "weaknesses": _get_rt(r["properties"].get("Weaknesses", {})),
                }
                for r in rows
                if _get_title_text(r["properties"].get("Competitor Name", {}))
            ]
        except Exception as e:
            log.warning(f"Could not load Competitors: {e}")

    # Existing keywords
    keywords_db_id = cfg.get("keywords_db_id", "")
    if keywords_db_id:
        try:
            rows = await notion.query_database(keywords_db_id)
            ctx["existing_keywords"] = [
                {
                    "keyword": _get_title_text(r["properties"].get("Keyword", {})),
                    "cluster": _get_rt(r["properties"].get("Cluster", {})),
                    "volume": _get_rt(r["properties"].get("Monthly Search Volume", {})),
                    "intent": _get_select(r["properties"].get("Intent", {})),
                    "our_position": _get_rt(r["properties"].get("Our Position", {})),
                    "priority": _get_select(r["properties"].get("Priority", {})),
                }
                for r in rows
                if _get_title_text(r["properties"].get("Keyword", {}))
            ]
        except Exception as e:
            log.warning(f"Could not load Keywords: {e}")

    return ctx


# ── Claude generation ──────────────────────────────────────────────────────────

BATTLE_PLAN_SYSTEM_PROMPT = """\
You are a senior local SEO strategist at RxMedia, a digital marketing agency
specializing in healthcare and behavioral health. You are writing a Battle Plan
for a client — an internal strategic document that will guide all SEO work for
the next 6–12 months.

Your Battle Plan must be specific, actionable, and grounded in the data
provided. Reference actual competitor names, keyword terms, and metrics.
Do not write generic advice. Every recommendation must tie to a specific gap
or opportunity visible in the data.

The Battle Plan has four sections:

1. EXECUTIVE SUMMARY — 2–3 paragraphs. Who the client is, what their current
   digital standing is, and the single most important opportunity to pursue.

2. KEY SEO GAPS — 3–5 named gaps with specific evidence. For each gap, name it
   (e.g. "The Content Depth Gap"), describe what competitors are doing, describe
   what the client is missing, and state the risk of not addressing it.

3. STRATEGIC ACTION PLAN — 4 phases:
   Phase 1: Local Dominance (GBP, reviews, map pack)
   Phase 2: Content & Service Pillars (landing pages, blog authority)
   Phase 3: Authority & Link Building (citations, backlinks, E-E-A-T)
   Phase 4: Programmatic SEO & LLM Visibility (schema, AI visibility, CMS pages)
   Each phase gets 2–4 specific named actions with rationale.

4. WHAT SUCCESS LOOKS LIKE — 3 categories of milestones:
   - Search Visibility & Rankings (specific keywords + target positions)
   - Digital Authority & Trust (E-E-A-T improvements, link targets)
   - Conversions & Business Impact (traffic %, DA growth, conversion rate)

Return ONLY this JSON structure — no markdown wrapping:
{
  "executive_summary": "string",
  "key_gaps": [
    {"name": "string", "evidence": "string", "risk": "string"}
  ],
  "action_plan": {
    "phase_1_local": [{"action": "string", "rationale": "string"}],
    "phase_2_content": [{"action": "string", "rationale": "string"}],
    "phase_3_authority": [{"action": "string", "rationale": "string"}],
    "phase_4_programmatic": [{"action": "string", "rationale": "string"}]
  },
  "success_milestones": {
    "visibility": ["string"],
    "authority": ["string"],
    "conversions": ["string"]
  },
  "review_flags": ["string"]
}

review_flags: list any data gaps, inconsistencies, or things the team should
manually verify before presenting this to the client. Be specific.
"""


async def _generate_battle_plan(ctx: dict, notes: str = "") -> dict:
    """Ask Claude to generate the full battle plan narrative."""
    if _anthropic is None:
        raise RuntimeError("Anthropic client not available — check ANTHROPIC_API_KEY")

    # Build context string for Claude
    context_parts = []

    if ctx.get("business_name"):
        context_parts.append(f"CLIENT: {ctx['business_name']}")
    if ctx.get("website"):
        context_parts.append(f"WEBSITE: {ctx['website']}")
    if ctx.get("raw_guidelines"):
        context_parts.append(f"BUSINESS OVERVIEW:\n{ctx['raw_guidelines'][:2000]}")
    if ctx.get("pagespeed_mobile") is not None:
        context_parts.append(
            f"PAGESPEED SCORES: Mobile {ctx['pagespeed_mobile']}/100 | "
            f"Desktop {ctx['pagespeed_desktop']}/100"
        )

    if ctx.get("existing_competitors"):
        comp_lines = []
        for c in ctx["existing_competitors"]:
            line = f"- {c['name']} | {c['website']}"
            if c.get("review_count"):
                line += f" | Reviews: {c['review_count']} ({c['rating']}★)"
            if c.get("authority_score"):
                line += f" | DA: {c['authority_score']} | RD: {c['referring_domains']}"
            if c.get("strengths"):
                line += f"\n  Strengths: {c['strengths'][:300]}"
            if c.get("weaknesses"):
                line += f"\n  Weaknesses: {c['weaknesses'][:300]}"
            comp_lines.append(line)
        context_parts.append("COMPETITORS:\n" + "\n".join(comp_lines))

    if ctx.get("existing_keywords"):
        kw_lines = []
        for k in ctx["existing_keywords"]:
            line = f"- [{k['cluster']}] {k['keyword']} | Vol: {k['volume']} | Intent: {k['intent']} | Our rank: {k['our_position'] or '—'}"
            kw_lines.append(line)
        context_parts.append("TARGET KEYWORDS:\n" + "\n".join(kw_lines))

    if notes:
        context_parts.append(f"STRATEGIC NOTES FROM TEAM:\n{notes}")

    context_str = "\n\n".join(context_parts)

    response = await _anthropic.messages.create(
        model=settings.anthropic_model,
        max_tokens=4096,
        system=BATTLE_PLAN_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": context_str}],
    )

    raw = response.content[0].text if response.content else "{}"
    try:
        import re
        clean = re.sub(r"```(?:json)?\n?", "", raw).strip().rstrip("```")
        return json.loads(clean)
    except json.JSONDecodeError:
        log.warning("Battle plan JSON parse failed — returning raw")
        return {"executive_summary": raw[:1000], "review_flags": ["JSON parse failed — review raw output"]}


# ── Notion write ───────────────────────────────────────────────────────────────

async def _write_battle_plan_page(
    notion: NotionClient,
    client_page_id: str,
    cfg: dict,
    ctx: dict,
    plan: dict,
    month: str,
) -> str:
    """Write the Battle Plan as a Notion page under the client root."""

    blocks: list[dict] = []

    # Header
    today_str = date.today().strftime("%B %d, %Y")
    blocks.append(_p(f"Generated: {today_str} | Client: {ctx.get('business_name', cfg['name'])}"))
    blocks.append(_divider())

    # Review flags callout (if any)
    flags = plan.get("review_flags", [])
    if flags:
        flag_text = "TEAM REVIEW NEEDED:\n" + "\n".join(f"• {f}" for f in flags)
        blocks.append(_callout(flag_text, "⚠️"))

    # Executive Summary
    blocks.append(_h("Executive Summary", 2))
    blocks.append(_p(plan.get("executive_summary", "")))

    # Key SEO Gaps
    blocks.append(_divider())
    blocks.append(_h("Key SEO Gaps & Insights", 2))
    for gap in plan.get("key_gaps", []):
        blocks.append(_h(gap.get("name", "Gap"), 3))
        blocks.append(_p(gap.get("evidence", "")))
        if gap.get("risk"):
            blocks.append(_bullet(f"Risk if unaddressed: {gap['risk']}"))

    # Action Plan
    blocks.append(_divider())
    blocks.append(_h("Strategic Action Plan", 2))

    action_plan = plan.get("action_plan", {})
    phase_labels = [
        ("phase_1_local",         "Phase 1: Local Dominance"),
        ("phase_2_content",       "Phase 2: Content & Service Pillars"),
        ("phase_3_authority",     "Phase 3: Authority & Link Building"),
        ("phase_4_programmatic",  "Phase 4: Programmatic SEO & LLM Visibility"),
    ]
    for key, label in phase_labels:
        actions = action_plan.get(key, [])
        if actions:
            blocks.append(_h(label, 3))
            for a in actions:
                blocks.append(_bullet(f"{a.get('action', '')}"))
                if a.get("rationale"):
                    blocks.append(_p(f"   → {a['rationale']}"))

    # Success Milestones
    blocks.append(_divider())
    blocks.append(_h("What Success Looks Like", 2))
    milestones = plan.get("success_milestones", {})

    milestone_sections = [
        ("visibility",   "Search Visibility & Rankings"),
        ("authority",    "Digital Authority & Trust"),
        ("conversions",  "Conversions & Business Impact"),
    ]
    for key, label in milestone_sections:
        items = milestones.get(key, [])
        if items:
            blocks.append(_h(label, 3))
            for item in items:
                blocks.append(_bullet(item))

    # Benchmark snapshot
    blocks.append(_divider())
    blocks.append(_h("Benchmark Snapshot (at time of plan)", 2))
    benchmark_lines = []
    if ctx.get("pagespeed_mobile") is not None:
        benchmark_lines.append(f"PageSpeed Mobile: {ctx['pagespeed_mobile']}/100")
    if ctx.get("pagespeed_desktop") is not None:
        benchmark_lines.append(f"PageSpeed Desktop: {ctx['pagespeed_desktop']}/100")
    if not benchmark_lines:
        benchmark_lines.append("No automated benchmark data available — fill in manually.")
    for line in benchmark_lines:
        blocks.append(_bullet(line))

    blocks.append(_callout(
        "Manually add: GBP impressions/calls/clicks (3-mo avg), Domain Authority, "
        "Referring Domains, Organic Sessions, Citation Score, keyword rankings from "
        "Search Atlas, LLM visibility (Gemini/ChatGPT/Perplexity), heatmap screenshots.",
        "📋"
    ))

    # Competitors reference
    if ctx.get("existing_competitors"):
        blocks.append(_divider())
        blocks.append(_h("Competitor Reference", 2))
        blocks.append(_p(
            f"Full competitor data is in the Competitors DB. "
            f"{len(ctx['existing_competitors'])} competitors loaded."
        ))

    # Create the page
    page_title = f"{ctx.get('business_name', cfg['name'])} — SEO Battle Plan {month}"
    page_id = await notion.create_page(
        parent_page_id=client_page_id,
        title=page_title,
    )
    await notion.append_blocks(page_id, blocks)
    return page_id


async def _ensure_battle_plan_dbs(notion: NotionClient, cfg: dict) -> tuple[str, str]:
    """
    Verify Competitors and Keywords DBs exist.
    Returns (competitors_db_id, keywords_db_id).
    Raises if either is missing — team must run make seo-init first.
    """
    comp_id = cfg.get("competitors_db_id", "")
    kw_id = cfg.get("keywords_db_id", "")

    if not comp_id or not kw_id:
        raise RuntimeError(
            "Missing competitors_db_id or keywords_db_id in client config.\n"
            "Run: make seo-init CLIENT=<client_key>\n"
            "This creates the Competitors and Keywords DBs in Notion."
        )
    return comp_id, kw_id


# ── Init (create input scaffold) ───────────────────────────────────────────────

async def _run_init(notion: NotionClient, cfg: dict, client_page_id: str) -> None:
    """
    Create a Battle Plan Input page in Notion for the team to fill in.
    This is the human review gate — team fills this before running the full plan.
    """
    print("\nCreating Battle Plan Input page in Notion...")

    blocks = [
        _callout(
            "Fill this page before running `make battle-plan`. "
            "The agent reads this data to generate the full battle plan.",
            "📋"
        ),
        _h("1. Competitor List", 2),
        _p(
            "Add competitor rows directly to the Competitors DB. "
            "For each competitor, fill in: Website, GBP URL, Primary Category, "
            "Review Count, Rating, Network Presence. "
            "Authority Score and Referring Domains come from Search Atlas."
        ),
        _h("2. Search Atlas Data", 2),
        _p("Export from Search Atlas and fill in the Keywords DB:"),
        _bullet("Keyword, Monthly Search Volume, Our Current Position, Cluster"),
        _bullet("Domain Authority and Referring Domains → paste into Competitors DB rows"),
        _h("3. GBP Benchmark Metrics (3-month average)", 2),
        _p("Pull from Google Business Profile dashboard (Jan–Mar average):"),
        _bullet("Impressions: ___"),
        _bullet("Calls: ___"),
        _bullet("Clicks: ___"),
        _bullet("GBP Score: ___"),
        _h("4. LLM Visibility Check", 2),
        _p("Search each LLM for the client's primary service + location. Note if mentioned/recommended:"),
        _bullet("Gemini: ___  (0=not visible, 1=mentioned, 2=recommended)"),
        _bullet("ChatGPT: ___"),
        _bullet("Perplexity: ___"),
        _h("5. Technical Baseline", 2),
        _p("If not already in Care Plan DB:"),
        _bullet("Domain Authority (Search Atlas): ___"),
        _bullet("Referring Domains: ___"),
        _bullet("Citation Score: ___"),
        _h("6. Heatmaps", 2),
        _p(
            "Run heatmaps in Search Atlas for 2–3 priority keywords. "
            "Attach screenshots to this page as images."
        ),
        _h("7. Strategic Notes", 2),
        _p(
            "Anything the team wants Claude to factor in — client priorities, "
            "competitive dynamics, budget constraints, specific opportunities flagged."
        ),
    ]

    page_title = f"{cfg['name']} — Battle Plan Input"
    page_id = await notion.create_page(
        parent_page_id=client_page_id,
        title=page_title,
    )
    await notion.append_blocks(page_id, blocks)
    print(f"  ✓ Battle Plan Input page created")
    print(f"  Fill it in at: https://notion.so/{page_id.replace('-', '')}")
    print("\nNext: fill in the Input page, then run `make battle-plan` to generate the plan.")


# ── Main ───────────────────────────────────────────────────────────────────────

async def main(client_key: str, notes: str = "", init_only: bool = False) -> None:
    from config.clients import CLIENTS

    cfg = CLIENTS.get(client_key)
    if not cfg:
        print(f"Client '{client_key}' not found in config/clients.py")
        sys.exit(1)

    notion = NotionClient(settings.notion_api_key)
    month = date.today().strftime("%B %Y")

    # Resolve client root page (needed for creating sub-pages)
    # We look it up from the client's Client Info DB parent
    client_page_id = None
    try:
        db = await notion._client.request(
            path=f"databases/{cfg['client_info_db_id']}", method="GET"
        )
        parent = db.get("parent", {})
        if parent.get("type") == "page_id":
            client_page_id = parent["page_id"]
    except Exception as e:
        log.warning(f"Could not resolve client page ID: {e}")

    if not client_page_id:
        print("Could not resolve client Notion page. Check client_info_db_id in config.")
        sys.exit(1)

    if init_only:
        await _run_init(notion, cfg, client_page_id)
        return

    # ── Full run ──────────────────────────────────────────────────────────────
    comp_id, kw_id = await _ensure_battle_plan_dbs(notion, cfg)

    print(f"\nLoading client context from Notion...")
    ctx = await _load_client_context(notion, cfg)
    print(f"  ✓ Loaded: {ctx.get('business_name', client_key)}")
    if ctx.get("existing_competitors"):
        print(f"  ✓ {len(ctx['existing_competitors'])} competitors")
    if ctx.get("existing_keywords"):
        print(f"  ✓ {len(ctx['existing_keywords'])} keywords")

    print("\nGenerating battle plan with Claude...")
    plan = await _generate_battle_plan(ctx, notes=notes)
    print("  ✓ Battle plan generated")

    flags = plan.get("review_flags", [])
    if flags:
        print(f"\n⚠️  {len(flags)} review flag(s) — team should verify before client presentation:")
        for f in flags:
            print(f"    • {f}")

    print("\nWriting battle plan to Notion...")
    page_id = await _write_battle_plan_page(
        notion, client_page_id, cfg, ctx, plan, month
    )
    print(f"  ✓ Battle Plan page: https://notion.so/{page_id.replace('-', '')}")

    print(f"\nDone. Battle Plan — {month} ready for team review.")

    if flags:
        print(
            "\nREMINDER: Review flags above must be addressed before presenting "
            "to the client. Check the ⚠️ callout at the top of the Notion page."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate SEO Battle Plan for a client")
    parser.add_argument("--client", required=True, help="Client key (e.g. summit_therapy)")
    parser.add_argument("--notes", default="", help="Strategic notes for Claude to factor in")
    parser.add_argument(
        "--init", action="store_true",
        help="Create Battle Plan Input page in Notion (fill before full run)"
    )
    args = parser.parse_args()

    asyncio.run(main(
        client_key=args.client,
        notes=args.notes,
        init_only=args.init,
    ))
