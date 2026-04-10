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

What gets auto-pulled and pre-seeded (make battle-plan-init):
  - Client business info, brand, and goals from Notion
  - PageSpeed scores from Care Plan DB (if available)
  - Competitors and keywords extracted from onboarding form data by Claude
    → skeleton rows created in Competitors DB and Keywords DB automatically
    → team verifies and fills in the SEO-specific data (volumes, rankings, DA)

What team fills in manually after make battle-plan-init:
  - Search Atlas: search volumes, current rankings, DA, referring domains
  - GBP baseline metrics (until GBP API is connected)
  - LLM visibility check (Gemini/ChatGPT/Perplexity)
  - Heatmap screenshots

What Claude generates (make battle-plan):
  - Executive summary and gap analysis
  - 4-phase strategic action plan
  - Keyword cluster strategy
  - Competitor strength/weakness narrative
  - Authority gap analysis
  - Success milestones

Run with --init to pre-seed the DBs and create the input checklist in Notion.
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


# ── Onboarding data parser ─────────────────────────────────────────────────────

PARSE_ONBOARDING_PROMPT = """\
You are extracting structured SEO data from a client's onboarding form submission.

Read the business overview text and extract:
1. Competitors — any businesses named as competitors, similar practices, or "websites we dislike"
2. Target keywords — any keyword phrases mentioned (SEO keywords field, services, locations)
3. The client's primary location(s)
4. Their core services

For keywords, suggest intent and keyword type based on context:
- "Local" intent = service + city/region queries
- "Transactional" intent = ready-to-book queries
- "Informational" intent = research/educational queries
- Type "GBP" = short local queries best targeted via Google Business Profile
- Type "Landing Page" = service+location pages
- Type "Blog" = informational/educational content
- Type "Home" = brand/primary terms
- Type "Service Hub" = main service category pages

Priority guidance:
- High = core service + primary location (highest traffic potential)
- Medium = service variations, secondary locations
- Low = long-tail, niche terms

Return ONLY this JSON — no markdown:
{
  "competitors": [
    {"name": "string", "website": "string or empty", "notes": "any context from the form"}
  ],
  "keywords": [
    {
      "keyword": "string",
      "cluster": "string (group name, e.g. Core Services, Mental Health, Location-Based)",
      "intent": "Local|Informational|Transactional|Commercial",
      "type": "GBP|Landing Page|Blog|Home|Service Hub",
      "location_modifier": "string or empty",
      "priority": "High|Medium|Low"
    }
  ],
  "location": "primary city, state",
  "primary_services": ["list of core services"]
}

If a field is not mentioned in the text, return an empty list or empty string.
Do not invent data — only extract what is explicitly or clearly implied in the text.
"""


async def _parse_onboarding_data(ctx: dict) -> dict:
    """
    Use Claude to extract structured competitors + keywords from onboarding form data.
    Returns {"competitors": [...], "keywords": [...], "location": "...", "primary_services": [...]}
    """
    if _anthropic is None:
        return {"competitors": [], "keywords": [], "location": "", "primary_services": []}

    raw = ctx.get("raw_guidelines", "")
    if not raw:
        return {"competitors": [], "keywords": [], "location": "", "primary_services": []}

    response = await _anthropic.messages.create(
        model=settings.anthropic_model,
        max_tokens=2048,
        system=PARSE_ONBOARDING_PROMPT,
        messages=[{"role": "user", "content": raw[:4000]}],
    )

    text = response.content[0].text if response.content else "{}"
    try:
        import re
        clean = re.sub(r"```(?:json)?\n?", "", text).strip().rstrip("```")
        return json.loads(clean)
    except json.JSONDecodeError:
        log.warning("Onboarding parse JSON failed")
        return {"competitors": [], "keywords": [], "location": "", "primary_services": []}


async def _seed_competitors_db(
    notion: NotionClient, db_id: str, competitors: list[dict]
) -> int:
    """Create skeleton competitor rows from onboarding data. Returns count created."""
    created = 0
    for comp in competitors:
        name = comp.get("name", "").strip()
        if not name:
            continue
        props: dict = {
            "Competitor Name": _title(name),
            "Notes": _rt(
                (comp.get("notes", "") + "\n\n⚠️ Pre-seeded from onboarding data — verify all fields.").strip()
            ),
        }
        website = comp.get("website", "").strip()
        if website and website.startswith("http"):
            props["Website"] = _url(website)
        try:
            await notion.create_database_entry(db_id, props)
            created += 1
        except Exception as e:
            log.warning(f"Could not create competitor row for {name}: {e}")
    return created


async def _seed_keywords_db(
    notion: NotionClient, db_id: str, keywords: list[dict]
) -> int:
    """Create skeleton keyword rows from onboarding data. Returns count created."""
    # Valid select options from our schema
    valid_intent = {"Informational", "Commercial", "Transactional", "Local", "Navigational"}
    valid_type   = {"GBP", "Landing Page", "Blog", "Home", "Service Hub"}
    valid_priority = {"High", "Medium", "Low"}

    created = 0
    for kw in keywords:
        keyword = kw.get("keyword", "").strip()
        if not keyword:
            continue
        props: dict = {
            "Keyword": _title(keyword),
            "Cluster": _rt(kw.get("cluster", "")),
            "Our Position": _rt("-"),
            "Notes": _rt("⚠️ Pre-seeded from onboarding data — add search volume + verify."),
        }
        intent = kw.get("intent", "")
        if intent in valid_intent:
            props["Intent"] = _sel(intent)

        kw_type = kw.get("type", "")
        if kw_type in valid_type:
            props["Type"] = _sel(kw_type)

        priority = kw.get("priority", "")
        if priority in valid_priority:
            props["Priority"] = _sel(priority)

        loc = kw.get("location_modifier", "").strip()
        if loc:
            props["Location Modifier"] = _rt(loc)

        try:
            await notion.create_database_entry(db_id, props)
            created += 1
        except Exception as e:
            log.warning(f"Could not create keyword row for {keyword}: {e}")
    return created


# ── Init (create input scaffold + pre-seed from onboarding) ───────────────────

async def _run_init(notion: NotionClient, cfg: dict, client_page_id: str) -> None:
    """
    Pre-seed Competitors and Keywords DBs from onboarding data, then create
    a Battle Plan Input page showing what was auto-filled vs what still needs
    manual input. This is the human review gate.
    """
    comp_id = cfg.get("competitors_db_id", "")
    kw_id   = cfg.get("keywords_db_id", "")

    if not comp_id or not kw_id:
        print("⚠️  Missing competitors_db_id or keywords_db_id. Run: make seo-init first.")
        return

    # ── Step 1: Load onboarding context from Notion ────────────────────────────
    print("\nLoading client context from Notion...")
    ctx = await _load_client_context(notion, cfg)
    business_name = ctx.get("business_name", cfg["name"])
    print(f"  ✓ Loaded: {business_name}")

    # ── Step 2: Parse competitors + keywords from onboarding data ──────────────
    print("Parsing onboarding data with Claude...")
    parsed = await _parse_onboarding_data(ctx)
    competitors_found = parsed.get("competitors", [])
    keywords_found    = parsed.get("keywords", [])
    location          = parsed.get("location", "")
    services          = parsed.get("primary_services", [])
    print(f"  ✓ Found: {len(competitors_found)} competitors, {len(keywords_found)} keywords")

    # ── Step 3: Check for existing rows (skip if already seeded) ──────────────
    existing_comps = await notion.query_database(comp_id)
    existing_kws   = await notion.query_database(kw_id)

    comp_count = 0
    kw_count   = 0

    if not existing_comps and competitors_found:
        print(f"Seeding Competitors DB ({len(competitors_found)} rows)...")
        comp_count = await _seed_competitors_db(notion, comp_id, competitors_found)
        print(f"  ✓ {comp_count} competitor rows created")
    elif existing_comps:
        comp_count = len(existing_comps)
        print(f"  — Competitors DB already has {comp_count} rows — skipping seed")

    if not existing_kws and keywords_found:
        print(f"Seeding Keywords DB ({len(keywords_found)} rows)...")
        kw_count = await _seed_keywords_db(notion, kw_id, keywords_found)
        print(f"  ✓ {kw_count} keyword rows created")
    elif existing_kws:
        kw_count = len(existing_kws)
        print(f"  — Keywords DB already has {kw_count} rows — skipping seed")

    # ── Step 4: Create the input checklist page ────────────────────────────────
    print("Creating Battle Plan Input page in Notion...")

    auto_filled: list[str] = []
    needs_manual: list[str] = []

    if comp_count:
        auto_filled.append(f"Competitors DB: {comp_count} rows pre-seeded from onboarding")
    else:
        needs_manual.append("Competitors DB: add competitor rows (name, website, GBP URL, review count, rating)")

    if kw_count:
        auto_filled.append(f"Keywords DB: {kw_count} keyword rows pre-seeded from onboarding")
    else:
        needs_manual.append("Keywords DB: add target keyword rows")

    if ctx.get("pagespeed_mobile") is not None:
        auto_filled.append(
            f"PageSpeed scores: Mobile {ctx['pagespeed_mobile']}/100, "
            f"Desktop {ctx['pagespeed_desktop']}/100 (from Care Plan DB)"
        )
    else:
        needs_manual.append("PageSpeed scores (or run make care-plan first)")

    needs_manual += [
        "Search Atlas: add search volumes + current rankings to each keyword row",
        "Search Atlas: add Authority Score + Referring Domains to each competitor row",
        "GBP benchmark metrics (3-month avg): impressions, calls, clicks, GBP score",
        "LLM visibility: search Gemini / ChatGPT / Perplexity for client's primary service + city",
        "Technical baseline: Domain Authority, Referring Domains, Citation Score (Search Atlas)",
        "Heatmaps: run 2–3 priority keywords in Search Atlas → attach screenshots here",
    ]

    blocks: list[dict] = [
        _callout(
            "Review and complete this page, then run `make battle-plan` to generate the full plan. "
            "Rows marked ⚠️ were pre-seeded from onboarding data — verify before running.",
            "📋"
        ),
    ]

    # What was auto-filled
    if auto_filled:
        blocks += [_h("✅ Auto-filled from onboarding data", 2)]
        for item in auto_filled:
            blocks.append(_bullet(item))
        blocks.append(_p(
            "These rows are in Notion now. Review them — they came from the client's intake form "
            "and may be incomplete or need clarification."
        ))

    # What still needs manual input
    blocks += [
        _h("📋 Needs manual input", 2),
        _p("Complete these before running make battle-plan:"),
    ]
    for item in needs_manual:
        blocks.append(_bullet(item))

    # Business context for reference
    if location or services:
        blocks.append(_divider())
        blocks.append(_h("Client Context (from onboarding)", 2))
        if location:
            blocks.append(_bullet(f"Primary location: {location}"))
        if services:
            blocks.append(_bullet(f"Core services: {', '.join(services)}"))

    # GBP metrics template
    blocks += [
        _divider(),
        _h("GBP Benchmark Metrics (3-month average)", 2),
        _p("Pull from Google Business Profile → Performance dashboard:"),
        _bullet("Impressions: ___"),
        _bullet("Calls: ___"),
        _bullet("Direction Requests: ___"),
        _bullet("Website Clicks: ___"),
        _bullet("GBP Completeness Score: ___"),
    ]

    # LLM visibility
    blocks += [
        _divider(),
        _h("LLM Visibility Check", 2),
        _p(
            f"Search: \"{services[0] if services else 'primary service'} in {location or 'city, state'}\" "
            f"in each LLM. Score: 0 = not visible, 1 = mentioned, 2 = recommended."
        ),
        _bullet("Gemini: ___"),
        _bullet("ChatGPT: ___"),
        _bullet("Perplexity: ___"),
    ]

    # Technical baseline
    blocks += [
        _divider(),
        _h("Technical Baseline (Search Atlas)", 2),
        _bullet("Domain Authority: ___"),
        _bullet("Referring Domains: ___"),
        _bullet("Backlinks: ___"),
        _bullet("Organic Sessions (GA4, last 3 months): ___"),
        _bullet("Citation Score: ___"),
        _bullet("Branded vs Non-Branded Clicks (GSC): ___"),
    ]

    # Heatmaps
    blocks += [
        _divider(),
        _h("Heatmaps", 2),
        _p(
            "Run heatmaps in Search Atlas for 2–3 priority keywords. "
            "Screenshot and attach to this page as images."
        ),
    ]

    # Strategic notes
    blocks += [
        _divider(),
        _h("Strategic Notes for Claude", 2),
        _p(
            "Anything the team wants factored into the battle plan: client priorities, "
            "budget constraints, timing, specific opportunities or concerns."
        ),
    ]

    page_title = f"{business_name} — Battle Plan Input"
    page_id = await notion.create_page(
        parent_page_id=client_page_id,
        title=page_title,
    )
    await notion.append_blocks(page_id, blocks)

    print(f"  ✓ Battle Plan Input page created")
    print(f"\nOpen in Notion: https://notion.so/{page_id.replace('-', '')}")
    print(f"\nWhat was auto-filled:")
    for item in auto_filled:
        print(f"  ✓ {item}")
    print(f"\nWhat still needs manual input ({len(needs_manual)} items):")
    for item in needs_manual:
        print(f"  • {item}")
    print(f"\nOnce complete, run: make battle-plan CLIENT={cfg['client_id']}")


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
