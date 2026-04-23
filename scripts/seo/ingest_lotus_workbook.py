#!/usr/bin/env python3
"""
ingest_lotus_workbook.py — Ingest Andrea's Lotus SEO workbook into Notion.

Source: https://docs.google.com/spreadsheets/d/100VgAkwF4TM_HqL4bAWbuGK0QPEADlUfH5ge35Me_Ho/edit
Built by: RxMedia (lead contributor: Andrea Tamayo)

Parallels the Cielo ingestion pattern. For future clients with existing
Andrea workbooks, clone this script (swap the KEYWORDS / COMPETITORS /
BATTLE_PLAN_SECTIONS blocks with the new client's data), or — eventually —
use the generic ingest script that takes a SHEET_ID.

Writes (idempotent, dedup by title):
  1. 19 keyword cluster rows → Lotus Keywords DB (Status=Target)
  2. 4 competitor deep-dives → Lotus Competitors DB (Status=Active)
  3. "SEO Battle Plan 2026" page under Lotus Notion root with Andrea's
     executive summary + gap analysis + 4-phase action plan + success criteria
  4. battle_plan_page_id → clients.json

Zero Claude credits, zero DataForSEO credits. Pure data migration.

Usage:
    python3 scripts/seo/ingest_lotus_workbook.py --dry-run
    python3 scripts/seo/ingest_lotus_workbook.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.config import settings
from src.integrations.notion import NotionClient


CLIENT_KEY = "lotus_recovery"
CLIENTS_JSON_PATH = Path(__file__).parent.parent.parent / "config" / "clients.json"


# ── Andrea's Lotus keyword cluster table (19 rows) ────────────────────────────

KEYWORDS: list[dict] = [
    # IOP cluster — Create gap, high priority (27K volume untapped)
    {"cluster": "IOP", "keyword": "iop programs", "volume": "27,100", "intent": "Commercial",
     "our_position": "-", "competitor_positions": "Rebound: 16", "gap_type": "Create", "priority": "High"},
    {"cluster": "IOP", "keyword": "iop programming", "volume": "27,100", "intent": "Commercial",
     "our_position": "-", "competitor_positions": "Rebound: 32", "gap_type": "Create", "priority": "High"},
    {"cluster": "IOP", "keyword": "intensive outpatient program therapy", "volume": "3,600", "intent": "Commercial",
     "our_position": "-", "competitor_positions": "Rebound: 20", "gap_type": "Create", "priority": "High"},
    {"cluster": "IOP", "keyword": "iop charleston", "volume": "320", "intent": "Commercial",
     "our_position": "-", "competitor_positions": "Lantana: 59", "gap_type": "Create", "priority": "High"},
    {"cluster": "IOP", "keyword": "iop counselor", "volume": "210", "intent": "Commercial",
     "our_position": "-", "competitor_positions": "Rebound: 98", "gap_type": "Create", "priority": "High"},

    # Local SC cluster — Optimize; already partially ranking on page 2
    {"cluster": "Local", "keyword": "rehabs in south carolina", "volume": "1,000", "intent": "Local",
     "our_position": "18", "competitor_positions": "Rebound: 7, Southern Sky: 11, Lantana: 76", "gap_type": "Optimize", "priority": "High"},
    {"cluster": "Local", "keyword": "rehab south carolina", "volume": "1,000", "intent": "Local",
     "our_position": "21", "competitor_positions": "Rebound: 9, Southern Sky: 13, Lantana: 79", "gap_type": "Optimize", "priority": "High"},
    {"cluster": "Local", "keyword": "rehabs south carolina", "volume": "1,000", "intent": "Local",
     "our_position": "15", "competitor_positions": "Rebound: 10, Southern Sky: 12, Lantana: 74", "gap_type": "Optimize", "priority": "High"},
    {"cluster": "Local", "keyword": "south carolina rehabs", "volume": "1,000", "intent": "Local",
     "our_position": "13", "competitor_positions": "Rebound: 78, Southern Sky: 11, Lantana: 89", "gap_type": "Optimize", "priority": "High"},
    {"cluster": "Local", "keyword": "south carolina rehab centers", "volume": "480", "intent": "Local",
     "our_position": "14", "competitor_positions": "Rebound: 11, Southern Sky: 9, Lantana: 53", "gap_type": "Optimize", "priority": "High"},
    {"cluster": "Local", "keyword": "rehab centers in south carolina", "volume": "480", "intent": "Local",
     "our_position": "21", "competitor_positions": "Rebound: 10, Southern Sky: 14, Lantana: 53", "gap_type": "Optimize", "priority": "High"},
    {"cluster": "Local", "keyword": "rehab columbia south carolina", "volume": "480", "intent": "Local",
     "our_position": "93", "competitor_positions": "Rebound: 50, Lantana: 76", "gap_type": "Optimize", "priority": "High"},
    {"cluster": "Local", "keyword": "rehab florence sc", "volume": "210", "intent": "Local",
     "our_position": "12", "competitor_positions": "", "gap_type": "Optimize", "priority": "High"},
    {"cluster": "Local", "keyword": "florence rehab florence sc", "volume": "90", "intent": "Local",
     "our_position": "19", "competitor_positions": "", "gap_type": "Optimize", "priority": "High"},
    {"cluster": "Local", "keyword": "florence rehabilitation center", "volume": "90", "intent": "Local",
     "our_position": "31", "competitor_positions": "", "gap_type": "Optimize", "priority": "High"},
    {"cluster": "Local", "keyword": "rehabilitation centers in florence sc", "volume": "50", "intent": "Local",
     "our_position": "10", "competitor_positions": "", "gap_type": "Defend", "priority": "High"},

    # PHP cluster — Create gap, differentiator opportunity
    {"cluster": "PHP", "keyword": "php with housing near me", "volume": "50", "intent": "Commercial",
     "our_position": "-", "competitor_positions": "Rebound: 32", "gap_type": "Create", "priority": "High"},

    # Addiction cluster (medium priority — geo expansion)
    {"cluster": "Addiction", "keyword": "rehab greenville sc", "volume": "720", "intent": "Local",
     "our_position": "-", "competitor_positions": "Lantana: 52", "gap_type": "Create", "priority": "Medium"},
    {"cluster": "Addiction", "keyword": "charleston sc rehab", "volume": "590", "intent": "Local",
     "our_position": "59", "competitor_positions": "Rebound: 53, Lantana: 67", "gap_type": "Optimize", "priority": "Medium"},
]


# ── Andrea's Lotus competitor deep-dives (4 competitors) ─────────────────────

COMPETITORS: list[dict] = [
    {
        "name": "Rebound Behavioral Health",
        "type": "Both",
        "website": "https://www.reboundbehavioralhealth.com",
        "threat": "High",
        "review_count": 0,
        "review_rating": 0,
        "review_velocity": "",
        "service_menu_complete": False,
        "has_posts": False,
        "network_presence": "Acadia Healthcare Network, Glassdoor/Indeed, Psychology Today",
        "top_ranking_page": "https://www.reboundbehavioralhealth.com/disorders/suicidal-thoughts/symptoms-signs-effects/",
        "target_cluster": "Mental Health Crisis — ranks for broad clinical symptom queries",
        "content_depth": "Long",
        "uses_faqs": False,
        "uses_schema": "MedicalWebPage Schema + BreadcrumbList Schema — signals professional medical information",
        "eeat_signals": "Very Strong. Joint Commission Gold Seal, South Carolina Hospital Association, specific patient testimonial (Caroline E.)",
        "page_type": "Service / Disorder-specific page",
        "referring_domains": 326,
        "authority_score": 27,
        "local_backlinks": "USC Lancaster (.edu), Lancaster Chamber of Commerce, SC Hospital Association",
        "industry_links": "Acadia Healthcare Network, Glassdoor/Indeed (brand authority), Psychology Today",
        "link_gap_notes": "Benefits significantly from being part of the Acadia Healthcare network. Lacks links from local civic orgs beyond Chamber.",
        "strengths": "Clinical Detail — symptoms broken into Behavioral / Physical / Cognitive / Psychosocial to rank for highly specific long-tail searches. Strong medical review bylines.",
        "weaknesses": "Lack of engagement — text-heavy with few visual breaks on mental health content.",
    },
    {
        "name": "Southern Sky Recovery",
        "type": "Both",
        "website": "https://southernskyrecovery.com",
        "threat": "High",
        "review_count": 0,
        "review_rating": 0,
        "review_velocity": "",
        "service_menu_complete": False,
        "has_posts": False,
        "network_presence": "C4 Consulting, Addiction Recovery Foundation, treatment center aggregators",
        "top_ranking_page": "https://southernskyrecovery.com/understanding-tre/",
        "target_cluster": "Trauma Therapy — service-specific content",
        "content_depth": "Long",
        "uses_faqs": True,
        "uses_schema": "Article Schema + FAQPage Schema — increases chances of Rich Snippets in Google results",
        "eeat_signals": "Elite. Authored by specific team member, cites WHO and SAMHSA, includes Licensed Physician Assistant bio.",
        "page_type": "Service-specific page with bylined clinical authorship",
        "referring_domains": 200,
        "authority_score": 23,
        "local_backlinks": "Live 5 News (local SC news), Bluffton-area business listings",
        "industry_links": "C4 Consulting, Addiction Recovery Foundation, treatment center aggregators",
        "link_gap_notes": "Both Rebound and Southern Sky have .edu links from USC Lancaster and Lander University — .edu links are the gold standard of local authority that Lotus is missing.",
        "strengths": "Internal linking — pages seamlessly link back to core PHP and IOP programs. Uses Table of Contents enabling Google jump-link snippets.",
        "weaknesses": "Conversion path — Confidential Callback form is placed too far down the page.",
    },
    {
        "name": "Lantana Recovery",
        "type": "Both",
        "website": "https://lantanarecovery.com",
        "threat": "High",
        "review_count": 85,
        "review_rating": 4.8,
        "review_velocity": "less than 5 a month",
        "service_menu_complete": False,
        "has_posts": True,
        "network_presence": "Yelp, Bing, Psychology Today, Recovery.com",
        "top_ranking_page": "https://lantanarecovery.com/what-is-a-schedule-1-drug/",
        "target_cluster": "Substance Abuse Education — ranks for informational educational blog queries",
        "content_depth": "Long",
        "uses_faqs": True,
        "uses_schema": "Article Schema (with author attribution) + FAQPage Schema — signals high informational quality",
        "eeat_signals": "Elite / Medical Grade. Authored by Warren Phillips (LMSW) with full clinical bio linked. 'Last Updated' (March 2024) showing content freshness. Cites federal laws (CSA 1970) and official DEA roles.",
        "page_type": "Blog / educational content with commercial break insertions",
        "referring_domains": 1000,
        "authority_score": 24,
        "local_backlinks": "Uses S3 buckets for localized landing pages (Charleston, etc.); Charleston-specific local blogs",
        "industry_links": "Medium (addiction recovery stories), health/medical directories, mental health blogs",
        "link_gap_notes": "Lantana uses a unique (though sometimes risky) strategy of hosting localized HTML pages on Amazon S3. This allows them to build thousands of local keyword-rich backlinks. NOT RECOMMENDED for Lotus.",
        "strengths": "Internal Link Strategy — 'Contextual Commercial Breaks': inserts service CTAs (e.g. 'Alcohol Rehab South Carolina') mid-article to capture researchers. Massive referring domain footprint.",
        "weaknesses": "Visual hierarchy — text broken by headers but lacks high-quality infographics or comparison charts. S3 localized page strategy is risky from a Google quality standpoint.",
    },
    {
        "name": "Owl's Nest Recovery",
        "type": "Local",
        "website": "",
        "threat": "Medium",
        "review_count": 80,
        "review_rating": 4.2,
        "review_velocity": "less than 5 a month",
        "service_menu_complete": True,
        "has_posts": True,
        "network_presence": "Yelp, Bing, Rehab.com",
        "last_photo_added": "1 month ago",
        "top_ranking_page": "",
        "target_cluster": "",
        "content_depth": "",
        "uses_faqs": False,
        "uses_schema": "",
        "eeat_signals": "",
        "page_type": "",
        "referring_domains": 0,
        "authority_score": 0,
        "local_backlinks": "",
        "industry_links": "",
        "link_gap_notes": "",
        "strengths": "Strong local GBP presence — 80 reviews vs Lotus's 30, recent photo activity (1 month ago), complete service menu.",
        "weaknesses": "Limited organic SEO footprint compared to Rebound/Southern Sky/Lantana. No deep content strategy visible.",
    },
]


# ── Andrea's Battle Plan narrative (pasted verbatim, lightly formatted) ──────

BATTLE_PLAN_TITLE = "SEO Battle Plan 2026 — Andrea's Workbook Ingestion"

BATTLE_PLAN_SECTIONS: list[tuple[str, str]] = [
    ("heading_1", "Executive Summary"),
    ("paragraph", (
        "Lotus Recovery is currently a secondary player in the South Carolina "
        "organic landscape. While Lotus possesses high-quality service offerings "
        "and a unique 52-acre 12-step mentorship model, our digital presence is "
        "being overshadowed by competitors who have built 'Authority Moats' "
        "through deep clinical content and aggressive local citation strategies."
    )),

    ("heading_2", "Current Standing"),
    ("bulleted_list_item", "Strengths: Perfect 5.0 Google rating; unique niche — 12-Step Mentorship + Associated Housing."),
    ("bulleted_list_item", "Critical Weakness: Invisibility for high-volume service terms (IOP / PHP). Currently ranking Page 2 (#12-#14) for core local terms — missing ~90% of potential click-through traffic."),

    ("heading_1", "Key SEO Gaps & Insights"),
    ("heading_2", "A. The Content Depth Gap"),
    ("paragraph", (
        "Competitors like Southern Sky and Lantana are winning because their "
        "pages are 1,500+ words of medical-grade content. Gap: Lotus's service "
        "pages are ~800 words and lack FAQ Schema, which prevents appearing in "
        "'People Also Ask' boxes."
    )),
    ("heading_2", "B. The Trust Signal (E-E-A-T) Gap"),
    ("paragraph", (
        "Google's 2026 algorithm heavily weights Medical Reviewers. Competitors "
        "link their content to Clinical Directors (LMSW, MD). Lotus content is "
        "currently unattributed, which lowers its Trust Score in the YMYL (Your "
        "Money Your Life) category."
    )),
    ("heading_2", "C. The Backlink Authority Moat"),
    ("paragraph", (
        "Rebound and Southern Sky have exact links from USC Lancaster and Lander "
        "University. These .edu links are the gold standard of local authority "
        "that Lotus is currently missing."
    )),

    ("heading_1", "Strategic Action Plan"),

    ("heading_2", "Phase 1 — Local Dominance"),
    ("numbered_list_item", "Action 1 — GBP Refresh. Upload 10 new high-res photos of the 52-acre estate."),
    ("numbered_list_item", "Action 2 — Claim 'Ghost' Listings. Claim and verify Yelp. Even without ads, these provide the NAP consistency Google requires for Top 3 rankings."),
    ("numbered_list_item", "Action 3 — Review Drive. Implement an automated SMS request to reach 50+ reviews to rival Owl's Nest."),
    ("numbered_list_item", "Action 4 — Create Profiles. Create Recovery.com, StartYourRecovery, Zocdoc, Psychology Today profiles."),

    ("heading_2", "Phase 2 — Content & Service Pillars"),
    ("numbered_list_item", "Action 5 — The 'IOP Pillar' Page. Create a 1,500-word cornerstone page for 'IOP South Carolina.' Include a 'Day in the Life' schedule and anchor it with FAQ Schema."),
    ("numbered_list_item", "Action 6 — Medical Review Implementation. Add a 'Reviewed by [Doctor Name]' byline to every program page to satisfy E-E-A-T requirements."),

    ("heading_2", "Phase 3 — Authority & Link Building"),
    ("numbered_list_item", "Action 7 — Local Civic Links. Join the Florence Chamber of Commerce to secure a high-trust .org backlink."),

    ("heading_2", "Phase 4 — Programmatic SEO"),
    ("paragraph", "The 'Substance-Specific' Treatment Pages. Patients rarely search for 'Addiction Treatment' — they search for the specific substance they are struggling with. Use a database of substances to generate targeted program pages."),
    ("bulleted_list_item", "Keyword Pattern: [Substance] Rehab [Location]"),
    ("bulleted_list_item", "Template H1: 'Specialized [Substance] Addiction Recovery in South Carolina.'"),
    ("bulleted_list_item", "Variable Data: Pull in specific withdrawal symptoms or detox protocols for [Substance] (e.g. Opioids vs. Alcohol)."),
    ("bulleted_list_item", "Internal Link: 'Our program is specifically tailored to help those overcoming [Substance].'"),
    ("paragraph", "Why it works: meets the user's specific search intent, which Google views as a higher quality match than a generic rehab page."),

    ("heading_1", "What Success Looks Like"),
    ("paragraph", (
        "Primary objective: transition Lotus Recovery from a locally-recognized "
        "facility to a digitally dominant statewide authority. By executing this "
        "strategy, we expect measurable growth across three core pillars — "
        "Search Visibility, Authority, and Conversions."
    )),

    ("heading_2", "1. Search Visibility & Ranking Milestones"),
    ("bulleted_list_item", "Top 3 Local Dominance — secure a Top 3 position for 'rehab florence sc' and 'addiction treatment florence sc.' Map Pack presence captures ~40% of all local search clicks."),
    ("bulleted_list_item", "Page 1 Breakthrough — move all core 'South Carolina' terms (e.g. 'php treatment sc,' 'south carolina addiction treatment') from Page 2 (#11-#15) to Page 1 (#1-#5)."),
    ("bulleted_list_item", "New Service Visibility — achieve a Top 20 ranking for the newly created IOP Pillar keywords within the first 60 days of launch."),

    ("heading_2", "2. Digital Authority & Trust (E-E-A-T)"),
    ("bulleted_list_item", "Medical Credibility — 100% of clinical pages featuring 'Medical Review' bylines, resulting in higher Quality Scores from Google's manual and algorithmic reviewers."),

    ("heading_2", "3. Conversions & Business Impact"),
    ("bulleted_list_item", "Organic Monthly Traffic — +20%"),
    ("bulleted_list_item", "Top 3 Keywords — IOP and PHP-related"),
    ("bulleted_list_item", "Domain Authority — +5 points"),
    ("bulleted_list_item", "Google Review Count — 50+"),

    ("heading_1", "Source"),
    ("paragraph", "Ingested from the Lotus SEO Workbook 2026 (lead contributor: Andrea Tamayo). Original: https://docs.google.com/spreadsheets/d/100VgAkwF4TM_HqL4bAWbuGK0QPEADlUfH5ge35Me_Ho/edit"),
]


# ── Notion helpers (duplicated from Cielo ingestion — kept local for clarity) ──

def _rt(text: str, limit: int = 1900) -> dict:
    return {"rich_text": [{"text": {"content": (text or "")[:limit]}}]}


def _title(text: str) -> dict:
    return {"title": [{"text": {"content": (text or "")[:200]}}]}


def _select(name: str) -> dict:
    return {"select": {"name": name}} if name else {"select": None}


def _number(v) -> dict:
    try:
        return {"number": float(v)} if v not in (None, "") else {"number": None}
    except (ValueError, TypeError):
        return {"number": None}


def _url_prop(v: str) -> dict:
    return {"url": v or None}


def _checkbox(v: bool) -> dict:
    return {"checkbox": bool(v)}


# ── Schema self-heal — Lotus DBs predate the approval lifecycle + Gap Type +
# rank-monitor fields, so bring them up to current schema before writing rows ──

async def heal_keywords_schema(notion: NotionClient, keywords_db_id: str, dry_run: bool) -> None:
    """Add Gap Type select, Proposed/Dismissed Status options, and rank fields if missing."""
    db = await notion._client.request(path=f"databases/{keywords_db_id}", method="GET")
    existing = db.get("properties", {})

    fields_to_add: dict = {}
    if "Gap Type" not in existing:
        fields_to_add["Gap Type"] = {"select": {"options": [
            {"name": "Create",   "color": "red"},
            {"name": "Optimize", "color": "yellow"},
            {"name": "Defend",   "color": "green"},
        ]}}
    if "Current Rank" not in existing:
        fields_to_add["Current Rank"] = {"number": {}}
    if "Last Checked" not in existing:
        fields_to_add["Last Checked"] = {"date": {}}
    if "Rank History" not in existing:
        fields_to_add["Rank History"] = {"rich_text": {}}

    # Status options self-heal — merge existing + new options
    status_prop = existing.get("Status", {})
    current_options = status_prop.get("select", {}).get("options", [])
    current_names = {o["name"] for o in current_options}
    new_options = [{"name": n, "color": c} for n, c in [
        ("Proposed",  "blue"),
        ("Target",    "gray"),
        ("Ranking",   "yellow"),
        ("Won",       "green"),
        ("Dismissed", "red"),
    ] if n not in current_names]
    if new_options:
        merged = list(current_options) + new_options
        fields_to_add["Status"] = {"select": {"options": merged}}

    if not fields_to_add:
        print("  ✓ Keywords DB schema already current")
        return
    if dry_run:
        print(f"  [DRY] would patch Keywords DB: {list(fields_to_add.keys())}")
        return
    await notion._client.request(
        path=f"databases/{keywords_db_id}", method="PATCH",
        body={"properties": fields_to_add},
    )
    print(f"  ✓ patched Keywords DB: {list(fields_to_add.keys())}")


async def heal_competitors_schema(notion: NotionClient, competitors_db_id: str, dry_run: bool) -> None:
    """Add Status select (Proposed/Active/Dismissed/Partner), Competing Keywords,
    GBP Details, Top Backlinks if missing."""
    db = await notion._client.request(path=f"databases/{competitors_db_id}", method="GET")
    existing = db.get("properties", {})

    fields_to_add: dict = {}
    if "Status" not in existing:
        fields_to_add["Status"] = {"select": {"options": [
            {"name": "Proposed",  "color": "blue"},
            {"name": "Active",    "color": "green"},
            {"name": "Partner",   "color": "purple"},
            {"name": "Dismissed", "color": "red"},
        ]}}
    else:
        # Merge missing options onto existing Status field
        current_options = existing["Status"].get("select", {}).get("options", [])
        current_names = {o["name"] for o in current_options}
        required = [
            ("Proposed",  "blue"),
            ("Active",    "green"),
            ("Partner",   "purple"),
            ("Dismissed", "red"),
        ]
        missing = [{"name": n, "color": c} for n, c in required if n not in current_names]
        if missing:
            merged = list(current_options) + missing
            fields_to_add["Status"] = {"select": {"options": merged}}

    for fname in ["Competing Keywords", "GBP Details", "Top Backlinks"]:
        if fname not in existing:
            fields_to_add[fname] = {"rich_text": {}}

    if not fields_to_add:
        print("  ✓ Competitors DB schema already current")
        return
    if dry_run:
        print(f"  [DRY] would patch Competitors DB: {list(fields_to_add.keys())}")
        return
    await notion._client.request(
        path=f"databases/{competitors_db_id}", method="PATCH",
        body={"properties": fields_to_add},
    )
    print(f"  ✓ patched Competitors DB: {list(fields_to_add.keys())}")


# ── Step helpers ──────────────────────────────────────────────────────────────

async def existing_keyword_titles(notion: NotionClient, keywords_db_id: str) -> set[str]:
    entries = await notion.query_database(database_id=keywords_db_id)
    out: set[str] = set()
    for e in entries:
        title_items = e["properties"].get("Keyword", {}).get("title", [])
        out.add("".join(p.get("text", {}).get("content", "") for p in title_items).strip().lower())
    return out


async def existing_competitor_names(notion: NotionClient, competitors_db_id: str) -> set[str]:
    entries = await notion.query_database(database_id=competitors_db_id)
    out: set[str] = set()
    for e in entries:
        title_items = e["properties"].get("Competitor Name", {}).get("title", [])
        out.add("".join(p.get("text", {}).get("content", "") for p in title_items).strip().lower())
    return out


async def write_keywords(notion: NotionClient, keywords_db_id: str, dry_run: bool) -> int:
    existing = await existing_keyword_titles(notion, keywords_db_id)
    written = 0
    for kw in KEYWORDS:
        key = kw["keyword"].strip().lower()
        if key in existing:
            print(f"  ↳ skip (exists): {kw['keyword']}")
            continue
        if dry_run:
            print(f"  [DRY] {kw['keyword']} [{kw['cluster']}] gap={kw['gap_type']} priority={kw['priority']}")
            written += 1
            continue
        await notion.create_database_entry(
            database_id=keywords_db_id,
            properties={
                "Keyword":                _title(kw["keyword"]),
                "Cluster":                _rt(kw["cluster"]),
                "Monthly Search Volume":  _rt(kw["volume"]),
                "Intent":                 _select(kw["intent"]),
                "Our Position":           _rt(kw["our_position"]),
                "Competitor Positions":   _rt(kw["competitor_positions"]),
                "Priority":               _select(kw["priority"]),
                "Gap Type":               _select(kw["gap_type"]),
                "Status":                 _select("Target"),
            },
        )
        print(f"  ✓ {kw['keyword']}")
        written += 1
    return written


async def write_competitors(notion: NotionClient, competitors_db_id: str, dry_run: bool) -> int:
    existing = await existing_competitor_names(notion, competitors_db_id)
    written = 0
    for c in COMPETITORS:
        key = c["name"].strip().lower()
        if key in existing:
            print(f"  ↳ skip (exists): {c['name']}")
            continue
        if dry_run:
            print(f"  [DRY] {c['name']} [{c['type']}] threat={c['threat']}")
            written += 1
            continue
        props: dict = {
            "Competitor Name":            _title(c["name"]),
            "Type":                       _select(c["type"]),
            "Website":                    _url_prop(c.get("website", "")),
            "Status":                     _select("Active"),
            "Threat":                     _select(c["threat"]),
            "Review Count":               _number(c.get("review_count")),
            "Review Rating":              _number(c.get("review_rating")),
            "Review Velocity":            _rt(c.get("review_velocity", "")),
            "Service Menu Complete":      _checkbox(c.get("service_menu_complete", False)),
            "Has Posts":                  _checkbox(c.get("has_posts", False)),
            "Network Presence":           _rt(c.get("network_presence", "")),
            "Last Photo Added":           _rt(c.get("last_photo_added", "")),
            "Top Ranking Page":           _url_prop(c.get("top_ranking_page", "")),
            "Target Cluster":             _rt(c.get("target_cluster", "")),
            "Uses FAQs":                  _checkbox(c.get("uses_faqs", False)),
            "Uses Schema":                _rt(c.get("uses_schema", "")),
            "EEAT Signals":               _rt(c.get("eeat_signals", "")),
            "Page Type":                  _rt(c.get("page_type", "")),
            "Referring Domains":          _number(c.get("referring_domains")),
            "Authority Score":            _number(c.get("authority_score")),
            "Local Backlinks":            _rt(c.get("local_backlinks", "")),
            "Industry Links":             _rt(c.get("industry_links", "")),
            "Link Gap Notes":             _rt(c.get("link_gap_notes", "")),
            "Strengths":                  _rt(c.get("strengths", "")),
            "Weaknesses":                 _rt(c.get("weaknesses", "")),
        }
        if c.get("content_depth"):
            props["Content Depth"] = _select(c["content_depth"])
        await notion.create_database_entry(database_id=competitors_db_id, properties=props)
        print(f"  ✓ {c['name']}")
        written += 1
    return written


async def create_battle_plan_page(notion: NotionClient, parent_page_id: str, dry_run: bool) -> str:
    if dry_run:
        print(f"  [DRY] would create Battle Plan page under {parent_page_id}")
        return ""
    page_id = await notion.create_page(parent_page_id=parent_page_id, title=BATTLE_PLAN_TITLE)
    blocks: list[dict] = []
    for block_type, text in BATTLE_PLAN_SECTIONS:
        blocks.append({
            "object": "block",
            "type":   block_type,
            block_type: {"rich_text": [{"type": "text", "text": {"content": text[:1900]}}]},
        })
    for i in range(0, len(blocks), 90):
        await notion.append_blocks(page_id, blocks[i:i + 90])
    print(f"  ✓ Battle Plan page: {page_id}")
    return page_id


def update_clients_json(battle_plan_page_id: str, dry_run: bool) -> None:
    data = json.loads(CLIENTS_JSON_PATH.read_text())
    if battle_plan_page_id and not dry_run:
        data[CLIENT_KEY]["battle_plan_page_id"] = battle_plan_page_id
        CLIENTS_JSON_PATH.write_text(json.dumps(data, indent=2))
        print("  ✓ clients.json updated (battle_plan_page_id)")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(dry_run: bool) -> None:
    from config.clients import CLIENTS
    cfg = CLIENTS.get(CLIENT_KEY)
    if not cfg:
        print(f"✗ {CLIENT_KEY} not in registry")
        sys.exit(1)

    keywords_db_id    = cfg.get("keywords_db_id", "")
    competitors_db_id = cfg.get("competitors_db_id", "")
    client_info_db_id = cfg.get("client_info_db_id", "")

    if not (keywords_db_id and competitors_db_id and client_info_db_id):
        print("✗ Missing one of: keywords_db_id, competitors_db_id, client_info_db_id")
        sys.exit(1)

    notion = NotionClient(settings.notion_api_key)

    print(f"\n── Ingesting Lotus workbook {'[DRY RUN]' if dry_run else ''} ──")

    print(f"\n[1/4] Self-heal DB schemas (Gap Type + approval lifecycle + rank fields)")
    await heal_keywords_schema(notion, keywords_db_id, dry_run)
    await heal_competitors_schema(notion, competitors_db_id, dry_run)

    print(f"\n[2/4] Write {len(KEYWORDS)} keywords → Keywords DB (Status=Target)")
    k_written = await write_keywords(notion, keywords_db_id, dry_run)

    print(f"\n[3/4] Write {len(COMPETITORS)} competitors → Competitors DB (Status=Active)")
    c_written = await write_competitors(notion, competitors_db_id, dry_run)

    print("\n[4/4] Battle Plan page + clients.json")
    db_meta = await notion._client.request(path=f"databases/{client_info_db_id}", method="GET")
    parent_page_id = db_meta.get("parent", {}).get("page_id", "")
    if not parent_page_id:
        print("  ⚠ Could not resolve client root page; skipping Battle Plan page")
        battle_plan_id = ""
    else:
        battle_plan_id = await create_battle_plan_page(notion, parent_page_id, dry_run)
    update_clients_json(battle_plan_id, dry_run)

    print(f"\n── Summary ──")
    print(f"  Keywords written: {k_written} / {len(KEYWORDS)}")
    print(f"  Competitors written: {c_written} / {len(COMPETITORS)}")
    print(f"  Battle Plan page: {battle_plan_id or '(skipped)'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest Lotus's SEO workbook into Notion")
    parser.add_argument("--dry-run", action="store_true", help="preview without writing")
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run))
