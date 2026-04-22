#!/usr/bin/env python3
"""
ingest_cielo_workbook.py — Ingest Andrea's SEO workbook into Cielo's Notion DBs.

Source: https://docs.google.com/spreadsheets/d/1mvHEN1fPjriowN65sKHZlwWb7vxR-iO9MaitNKoFAPw/edit
Built by: RxMedia (lead contributor: Andrea Tamayo)

What this does (one-off migration, safe to re-run):
  1. Self-heal Cielo's Keywords DB — add "Gap Type" field if missing
  2. Write 10 keyword cluster rows → Keywords DB (dedups by keyword text)
  3. Write 4 competitor rows → Competitors DB (dedups by competitor name)
  4. Create a "SEO Battle Plan 2026" page under Cielo's Notion root with the
     executive summary, critical weaknesses, and 3-phase action plan
  5. Update clients.json with canonical NAP + tracking-exempt directories

Zero Claude credits. Zero DataForSEO credits. Pure data migration.

Usage:
    python3 scripts/seo/ingest_cielo_workbook.py --dry-run
    python3 scripts/seo/ingest_cielo_workbook.py              # live write
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


CLIENT_KEY = "cielo_treatment_center"
CLIENTS_JSON_PATH = Path(__file__).parent.parent.parent / "config" / "clients.json"


# ── Andrea's keyword cluster table (10 rows) ──────────────────────────────────

KEYWORDS: list[dict] = [
    {
        "cluster": "Core Substance Abuse",
        "keyword": "addiction treatment Portland Oregon",
        "volume": "1,000",
        "intent": "Local",
        "our_position": "-",
        "competitor_positions": "Crestview: 1, Tree House: 2, Fora: 12",
        "gap_type": "Create",
        "priority": "High",
    },
    {
        "cluster": "Core Substance Abuse",
        "keyword": "drug rehab Portland Oregon",
        "volume": "1,000",
        "intent": "Local",
        "our_position": "-",
        "competitor_positions": "Crestview: 1, Tree House: 2, Fora: 12",
        "gap_type": "Create",
        "priority": "High",
    },
    {
        "cluster": "Core Substance Abuse",
        "keyword": "alcohol rehab Portland Oregon",
        "volume": "590",
        "intent": "Local",
        "our_position": "-",
        "competitor_positions": "Crestview: 13, Tree House: 37, Fora: 43",
        "gap_type": "Create",
        "priority": "High",
    },
    {
        "cluster": "Mental Health",
        "keyword": "mental health counseling Portland Oregon",
        "volume": "390",
        "intent": "Local",
        "our_position": "-",
        "competitor_positions": "Crestview: 39",
        "gap_type": "Optimize",
        "priority": "High",
    },
    {
        "cluster": "Dual Diagnosis",
        "keyword": "ADHD and addiction treatment Portland Oregon",
        "volume": "Low-Vol / High-Intent",
        "intent": "Transactional",
        "our_position": "-",
        "competitor_positions": "None of Crestview, Tree House, or Fora rank",
        "gap_type": "Optimize",
        "priority": "High",
    },
    {
        "cluster": "Culturally Specific",
        "keyword": "Native American addiction treatment in Oregon",
        "volume": "260",
        "intent": "Informational",
        "our_position": "-",
        "competitor_positions": "None of Crestview, Tree House, or Fora rank",
        "gap_type": "Optimize",
        "priority": "High",
    },
    {
        "cluster": "Age-Specific",
        "keyword": "young adult rehab oregon",
        "volume": "480",
        "intent": "Transactional",
        "our_position": "-",
        "competitor_positions": "Crestview: 45",
        "gap_type": "Optimize",
        "priority": "High",
    },
    {
        "cluster": "Age-Specific",
        "keyword": "young adult residential treatment",
        "volume": "720",
        "intent": "Transactional",
        "our_position": "-",
        "competitor_positions": "Crestview: 32",
        "gap_type": "Optimize",
        "priority": "High",
    },
    {
        "cluster": "LGBTQ+ Affirming",
        "keyword": "lgbtq substance abuse treatment",
        "volume": "210",
        "intent": "Transactional",
        "our_position": "-",
        "competitor_positions": "None of Crestview, Tree House, or Fora rank",
        "gap_type": "Optimize",
        "priority": "High",
    },
    {
        "cluster": "LGBTQ+ Affirming",
        "keyword": "lgbtq rehab centers",
        "volume": "260",
        "intent": "Transactional",
        "our_position": "-",
        "competitor_positions": "None of Crestview, Tree House, or Fora rank",
        "gap_type": "Optimize",
        "priority": "High",
    },
]


# ── Andrea's competitor deep-dives (4 competitors) ────────────────────────────

COMPETITORS: list[dict] = [
    {
        "name": "Crest View Recovery",
        "type": "Both",
        "website": "https://www.crestviewrecovery.com",
        "threat": "High",
        "review_count": 121,
        "review_rating": 4.1,
        "review_velocity": "1 per week",
        "service_menu_complete": True,
        "has_posts": True,
        "network_presence": "Rehabs.com, Recovery.com, Help.org, Yelp, Bing",
        "last_photo_added": "2 months ago",
        "top_ranking_page": "https://www.crestviewrecovery.com/rehab-blog/what-are-the-top-10-medications-for-anxiety/",
        "target_cluster": "Anxiety & Medical Management — broad informational intent",
        "content_depth": "Long",
        "uses_faqs": False,
        "uses_schema": "Article & BlogPosting",
        "eeat_signals": "High: Medical Review by-lines, citations from SAMHSA / Mayo Clinic",
        "page_type": "Blog / Educational — Listicle format (Top 10) designed for high shareability and organic reach",
        "referring_domains": 1900,
        "authority_score": 34,
        "local_backlinks": "Directory resource pages like 101eldercare.com mentioning Portland detox",
        "industry_links": "WebMD (Holy Grail for medical SEO). Also network-links sister facilities like Bayview Recovery.",
        "link_gap_notes": "Lack the Institutional local links (.gov, large local news) that competitors use to anchor rankings for broad terms like 'Drug Rehab Portland'.",
        "strengths": "The Bridge Strategy (medical info → service CTA). Topical authority from medication content. Internal linking from blogs to money pages.",
        "weaknesses": "Low Conversion Intent on 'Anxiety meds' queries (high bounce). Maintenance burden (medical info changes yearly). Competes with Healthline/WebMD giants.",
    },
    {
        "name": "Tree House Recovery PDX",
        "type": "Both",
        "website": "https://treehouserecoverypdx.com",
        "threat": "High",
        "review_count": 38,
        "review_rating": 5.0,
        "review_velocity": "less than 5 a month",
        "service_menu_complete": False,
        "has_posts": True,
        "network_presence": "Recovery.com, Psychology Today, The Treatment Specialist",
        "last_photo_added": "1 year ago",
        "top_ranking_page": "https://treehouserecoverypdx.com/",
        "target_cluster": "Men's Addiction Recovery — Fitness, Young Adult Men, Veterans",
        "content_depth": "Medium-Long",
        "uses_faqs": False,
        "uses_schema": "LocalBusiness & MedicalOrganization — highly optimized for Portland Map Pack",
        "eeat_signals": "Videos + Gallery add transparency/trust",
        "page_type": "Homepage / Brand Pillar — primary landing page for all Portland-specific searches",
        "referring_domains": 336,
        "authority_score": 25,
        "local_backlinks": "Localized recovery maps and niche Portland directories (BetterAddictionCare, StartYourRecovery)",
        "industry_links": "Medium.com for long-form expert articles (anhedonia, masculinity). Buzzsprout for recovery podcasts.",
        "link_gap_notes": "Strength is High-Authority Medical mentions — Google trusts them because of association with authoritative medical sites.",
        "strengths": "Differentiated modality (ABI Therapy, ESM Fitness Therapy — proprietary brand keywords). Identity-driven marketing ('Teaching men how to live free'). Aggressive Veteran/First Responder focus with dedicated navigation.",
        "weaknesses": "Gender Gap — ignores LGBTQ+ / Gender-Affirming clusters entirely. Clinical vs Physical — gym/training branding feels intimidating to clients seeking caring/soft environment. Low ADHD/Native American content.",
    },
    {
        "name": "Fora Health",
        "type": "Organic",
        "website": "https://forahealth.org",
        "threat": "High",
        "review_count": 0,   # not captured in workbook Local tab
        "review_rating": 0,
        "review_velocity": "",
        "service_menu_complete": False,
        "has_posts": False,
        "network_presence": "",
        "top_ranking_page": "https://forahealth.org/",
        "target_cluster": "Full Continuum of Care — Detox, DUII, Residential, Outpatient",
        "content_depth": "Long",
        "uses_faqs": False,
        "uses_schema": "MedicalOrganization and MedicalClinic (advanced Medical Schema) — signals high authority for Health queries",
        "eeat_signals": "Industry-leading: CARF Accreditation, Joint Commission seals. Highlights Same Day Care.",
        "page_type": "Institutional Pillar — focused on accessibility, evidence-based care, high-volume admissions",
        "referring_domains": 284,
        "authority_score": 30,
        "local_backlinks": "Oregon.gov, Hillsboro-Oregon.gov, OregonLive.com",
        "industry_links": "Newsweek 'America's Best Addiction Treatment Centers'. Clinical referral links from Washington Dept of Health (DOH.wa.gov).",
        "link_gap_notes": "Strength is Local Authority — Google views them as a Public Resource, so they dominate general searches.",
        "strengths": "Low-Barrier Conversion (Same Day Care, Walk-In). Massive Trust Signals (Gala results, Annual Reports, Institutional Stability). Insurance Accessibility (OHP, sliding scale) — default result for 'Affordable rehab Portland'.",
        "weaknesses": "One-Size-Fits-All trap — lacks emotional resonance of niche providers. Keyword Dilution from covering too many services. Neighborhood Anonymity — feels statewide, not neighborhood-specific like Cielo's Sandy Blvd presence.",
    },
    {
        "name": "True Colors Recovery",
        "type": "Local",
        "website": "",  # not captured
        "threat": "Medium",
        "review_count": 42,
        "review_rating": 4.6,
        "review_velocity": "less than 5 a month",
        "service_menu_complete": False,
        "has_posts": True,
        "network_presence": "GuideStar / GreatNonprofits, Q Center",
        "last_photo_added": "1 year ago",
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
        "strengths": "",
        "weaknesses": "",
    },
]


# ── Battle Plan page content ──────────────────────────────────────────────────

BATTLE_PLAN_TITLE = "SEO Battle Plan 2026 — Andrea's Workbook Ingestion"

BATTLE_PLAN_SECTIONS = [
    ("heading_1", "Executive Summary"),
    ("paragraph", (
        "Cielo Treatment Center is a specialized niche player in the Portland, Oregon addiction "
        "recovery market, specifically excelling in identity-based care (LGBTQ+, Indigenous, and "
        "Young Adult). While Cielo holds a unique competitive advantage in these specialized tracks, "
        "overall digital authority is currently overshadowed by larger institutional 'Generalist' "
        "competitors like Fora Health and Crestview Recovery. These competitors have established "
        "dominant 'Authority Moats' through high-volume backlink profiles from state government and "
        "major medical sites."
    )),
    ("heading_2", "Current Standing"),
    ("bulleted_list_item", "Strengths: High-trust local mentions (e.g., The New York Times) and unique clinical sub-specialties that large facilities treat as generic secondary services."),
    ("bulleted_list_item", "Critical Weakness: Invisibility for high-volume, high-intent service terms (IOP/PHP/General Rehab). Cielo is currently missing from page 1 for core local terms like 'drug rehab Portland,' where Crestview and Tree House hold the Top 3 positions."),

    ("heading_1", "Key SEO Gaps & Insights"),
    ("heading_2", "A. The Content Depth Gap"),
    ("paragraph", "Competitors are winning broad, high-volume keywords because they maintain deep, medically-reviewed blog content that captures top-of-funnel research queries (e.g., Crestview's 'Anxiety Medications' or 'Xanax Withdrawal' guides). While Cielo has specialized pages, they lack the sheer volume of 'Search Intent' matches for long-tail medical and pharmacological queries that drive broad domain authority."),
    ("heading_2", "B. The Trust Signal (E-E-A-T) Gap"),
    ("paragraph", "Fora Health dominates local search by securing 'Institutional' links from Oregon.gov, Lander University, and Newsweek. Google's algorithm prioritizes healthcare providers linked to official public health entities. Cielo's current profile leans heavily on industry directories rather than local civic or educational institutions."),
    ("heading_2", "C. The Local Map Pack Gap"),
    ("paragraph", "Tree House Recovery PDX and Crestview are aggressive in the North and SE Portland Map Packs respectively. Cielo is not currently optimized to 'steal' traffic for broad Portland terms, despite being a high-quality alternative."),

    ("heading_1", "Strategic Action Plan"),
    ("heading_2", "Phase 1 — Local Dominance"),
    ("numbered_list_item", "Proximity and Quadrant Optimization. Update GBP to highlight NE Sandy Blvd location specifically for 'NE Portland Rehab' and 'Kerns Neighborhood Addiction Treatment.'"),
    ("numbered_list_item", "Niche Review Drive. Implement a campaign to gain reviews that specifically mention niche keywords: 'LGBTQ,' 'Indigenous healing,' and 'ADHD treatment.' Google reads these review keywords to determine Map Pack relevancy."),
    ("numbered_list_item", "Profile Expansion. Fully build out and verify profiles on Recovery.com, Psychology Today, and StartYourRecovery to mirror competitor presence."),
    ("heading_2", "Phase 2 — Content & Service Pillars"),
    ("numbered_list_item", "ADHD + Addiction Authority. Build a dedicated clinical landing page for 'ADHD and addiction treatment Portland.' None of the top 3 competitors are successfully ranking for this specific intersection — this is the 'Blue Ocean' opportunity."),
    ("numbered_list_item", "Medical Attribution. Ensure every blog post features a 'Medical Reviewer' byline (e.g., Clinical Director or Medical Doctor) to satisfy 2026 E-E-A-T requirements for YMYL content."),
    ("heading_2", "Phase 3 — Authority & Link Building"),
    ("numbered_list_item", "Civic Link Outreach. Target Portland-specific educational institutions and civic orgs for backlinks; chase Oregon.gov / news media parity with Fora Health."),

    ("heading_1", "Source"),
    ("paragraph", "Ingested from the Cielo SEO Workbook 2026 (lead contributor: Andrea Tamayo). Original Google Sheet: https://docs.google.com/spreadsheets/d/1mvHEN1fPjriowN65sKHZlwWb7vxR-iO9MaitNKoFAPw/edit"),
]


# ── NAP canonicals + tracking exceptions ──────────────────────────────────────

CIELO_NAP_CONFIG = {
    "canonical_address":            "1805 NE Sandy Blvd, Portland, OR 97232",
    "canonical_phone":              "503-647-6132",
    "tracking_phone_directories":   ["recovery.com", "startyourrecovery.com", "psychologytoday.com", "zocdoc.com"],
}


# ── Notion helpers ────────────────────────────────────────────────────────────

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


# ── Ingestion steps ───────────────────────────────────────────────────────────

async def ensure_gap_type_field(notion: NotionClient, keywords_db_id: str, dry_run: bool) -> None:
    """Self-heal — add Gap Type select field to Keywords DB if missing."""
    db = await notion._client.request(path=f"databases/{keywords_db_id}", method="GET")
    if "Gap Type" in db.get("properties", {}):
        print("  ✓ Gap Type field already present on Keywords DB")
        return
    if dry_run:
        print("  [DRY] Would patch Keywords DB with Gap Type field")
        return
    await notion._client.request(
        path=f"databases/{keywords_db_id}",
        method="PATCH",
        body={"properties": {
            "Gap Type": {
                "select": {
                    "options": [
                        {"name": "Create",   "color": "red"},
                        {"name": "Optimize", "color": "yellow"},
                        {"name": "Defend",   "color": "green"},
                    ]
                }
            }
        }},
    )
    print("  ✓ Patched Keywords DB — added Gap Type field")


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
            print(f"  [DRY] would write: {kw['keyword']} [{kw['cluster']}] gap={kw['gap_type']} priority={kw['priority']}")
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
        print(f"  ✓ wrote: {kw['keyword']}")
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
            print(f"  [DRY] would write: {c['name']} [{c['type']}] threat={c['threat']}")
            written += 1
            continue
        props: dict = {
            "Competitor Name":            _title(c["name"]),
            "Type":                       _select(c["type"]),
            "Website":                    _url_prop(c.get("website", "")),
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
        print(f"  ✓ wrote: {c['name']}")
        written += 1
    return written


async def create_battle_plan_page(notion: NotionClient, parent_page_id: str, dry_run: bool) -> str:
    if dry_run:
        print(f"  [DRY] would create Battle Plan page under {parent_page_id}")
        return ""

    page_id = await notion.create_page(
        parent_page_id=parent_page_id,
        title=BATTLE_PLAN_TITLE,
    )

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
    entry = data[CLIENT_KEY]
    entry["canonical_address"]          = CIELO_NAP_CONFIG["canonical_address"]
    entry["canonical_phone"]            = CIELO_NAP_CONFIG["canonical_phone"]
    entry["tracking_phone_directories"] = CIELO_NAP_CONFIG["tracking_phone_directories"]
    if battle_plan_page_id:
        entry["battle_plan_page_id"] = battle_plan_page_id
    if dry_run:
        print(f"  [DRY] would write NAP canonicals + battle_plan_page_id to clients.json")
        return
    CLIENTS_JSON_PATH.write_text(json.dumps(data, indent=2))
    print("  ✓ clients.json updated (NAP canonicals + battle_plan_page_id)")


# ── Orchestrator ──────────────────────────────────────────────────────────────

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

    print(f"\n── Ingesting Cielo workbook {'[DRY RUN]' if dry_run else ''} ──")

    print("\n[1/4] Self-heal Keywords DB")
    await ensure_gap_type_field(notion, keywords_db_id, dry_run)

    print(f"\n[2/4] Write {len(KEYWORDS)} keywords → Keywords DB")
    k_written = await write_keywords(notion, keywords_db_id, dry_run)

    print(f"\n[3/4] Write {len(COMPETITORS)} competitors → Competitors DB")
    c_written = await write_competitors(notion, competitors_db_id, dry_run)

    print("\n[4/4] Create Battle Plan page + update clients.json")
    # Resolve the client root page via Client Info DB parent
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
    parser = argparse.ArgumentParser(description="Ingest Cielo's SEO workbook into Notion")
    parser.add_argument("--dry-run", action="store_true", help="preview without writing")
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run))
