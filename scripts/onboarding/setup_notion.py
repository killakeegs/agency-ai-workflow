#!/usr/bin/env python3
"""
setup_notion.py — Provision a new client in Notion

Creates the complete Notion database structure for a new client:
  1. A top-level client page under NOTION_WORKSPACE_ROOT_PAGE_ID
  2. Eight linked databases (two passes — see note below)

IMPORTANT — Two-pass creation:
  Notion requires a database to exist before another database can reference it
  via a "relation" property. So we create all 8 databases in Pass 1, then
  add relation properties in Pass 2.

Usage:
    cd "AI-Powered Digital Marketing Agency Workflow"
    source .venv/bin/activate
    python scripts/setup_notion.py --client-name "ACME Corp" --contact-email "jane@acme.com"
    python scripts/setup_notion.py --client-name "Test Client" --contact-email "test@example.com" --dry-run

After running, add the printed database IDs to your .env or store them
in the Client Info database entry for this client.
"""
import argparse
import asyncio
import sys
from pathlib import Path

# Allow running as a script from the project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings
from src.integrations.notion import NotionClient
from src.models.pipeline import PipelineStage


# ── Database schema definitions ───────────────────────────────────────────────

def client_info_schema() -> dict:
    """Schema for the Client Info database."""
    pipeline_options = [
        {"name": stage.value, "color": _stage_color(stage)}
        for stage in PipelineStage
    ]
    return {
        "Name": {"title": {}},
        "Company": {"rich_text": {}},
        "Email": {"email": {}},
        "Phone": {"phone_number": {}},
        "Website": {"url": {}},
        "Business Type": {
            "select": {
                "options": [
                    {"name": "Local Business", "color": "blue"},
                    {"name": "National Business", "color": "green"},
                    {"name": "E-Commerce", "color": "purple"},
                    {"name": "Service Business", "color": "yellow"},
                    {"name": "SaaS / Tech", "color": "red"},
                ]
            }
        },
        "Pipeline Stage": {"select": {"options": pipeline_options}},
        "Stage Status": {
            "select": {
                "options": [
                    {"name": "In Progress",       "color": "blue"},
                    {"name": "Pending Review",     "color": "yellow"},
                    {"name": "Approved",           "color": "green"},
                    {"name": "Revision Requested", "color": "red"},
                ]
            }
        },
        "Revision Notes": {"rich_text": {}},
        "ClickUp Folder ID": {"rich_text": {}},
        "Project Start": {"date": {}},
        "Timeline (Weeks)": {"number": {}},
        "Notes": {"rich_text": {}},
    }


def meeting_notes_schema() -> dict:
    return {
        "Title": {"title": {}},
        "Meeting Date": {"date": {}},
        "Meeting Type": {
            "select": {
                "options": [
                    {"name": "Kickoff", "color": "blue"},
                    {"name": "Mood Board Review", "color": "yellow"},
                    {"name": "Sitemap Review", "color": "green"},
                    {"name": "Wireframe Review", "color": "orange"},
                    {"name": "Design Review", "color": "purple"},
                    {"name": "Check-in", "color": "gray"},
                ]
            }
        },
        "Parsed": {"checkbox": {}},
        "Key Decisions": {"rich_text": {}},
        "Action Items Count": {"number": {}},
        "Raw Transcript": {"rich_text": {}},
    }


def brand_guidelines_schema() -> dict:
    return {
        "Name": {"title": {}},
        "Primary Color": {"rich_text": {}},
        "Secondary Color": {"rich_text": {}},
        "Accent Color": {"rich_text": {}},
        "Primary Font": {"rich_text": {}},
        "Secondary Font": {"rich_text": {}},
        "Tone Descriptors": {"rich_text": {}},
        "Logo Assets": {"files": {}},
        "Inspiration URLs": {"rich_text": {}},
        "Raw Guidelines": {"rich_text": {}},
        "Image Direction": {"rich_text": {}},
        "Photography Style": {"rich_text": {}},
        # ── Content Style Guide ───────────────────────────────────────────────
        "Voice & Tone":   {"rich_text": {}},
        "Reading Level":  {"rich_text": {}},
        "Power Words":    {"rich_text": {}},
        "Words to Avoid": {"rich_text": {}},
        "CTA Style":      {"rich_text": {}},
        "POV Notes":      {"rich_text": {}},
        # ── Blog Voice & Reviewer (used by blog pipeline) ─────────────────────
        "Blog Voice":                {"rich_text": {}},  # synthesized style brief from blog_setup
        "Blog Reviewer Name":        {"rich_text": {}},  # e.g. "Sarah Chen"
        "Blog Reviewer Credentials": {"rich_text": {}},  # e.g. "M.S., CCC-SLP"
        "Blog Reviewer Bio":         {"rich_text": {}},  # 2–3 sentence bio for post footer
    }


def mood_board_schema() -> dict:
    return {
        "Title": {"title": {}},
        "Variation": {
            "select": {
                "options": [
                    {"name": "Option A", "color": "blue"},
                    {"name": "Option B", "color": "green"},
                    {"name": "Option C", "color": "yellow"},
                    {"name": "Option D", "color": "orange"},
                    {"name": "Option E", "color": "purple"},
                    {"name": "Option F", "color": "red"},
                ]
            }
        },
        "Status": {
            "select": {
                "options": [
                    {"name": "Draft", "color": "gray"},
                    {"name": "Pending Review", "color": "yellow"},
                    {"name": "Approved", "color": "green"},
                    {"name": "Rejected", "color": "red"},
                ]
            }
        },
        "Style Keywords": {"rich_text": {}},
        "Color Palette Description": {"rich_text": {}},
        "Visual References": {"rich_text": {}},
        "Client Feedback": {"rich_text": {}},
    }


def sitemap_schema() -> dict:
    return {
        "Page Title": {"title": {}},
        "Slug": {"rich_text": {}},
        "Page Type": {
            "select": {
                "options": [
                    {"name": "Static", "color": "blue"},
                    {"name": "CMS", "color": "green"},
                ]
            }
        },
        "Content Mode": {
            "select": {
                "options": [
                    {"name": "AI Generated", "color": "purple"},
                    {"name": "Client Provided", "color": "yellow"},
                ]
            }
        },
        "Status": {
            "select": {
                "options": [
                    {"name": "Draft", "color": "gray"},
                    {"name": "Approved", "color": "green"},
                ]
            }
        },
        "Purpose": {"rich_text": {}},
        "Key Sections": {"rich_text": {}},
        "Order": {"number": {}},
    }


def wireframes_schema() -> dict:
    return {
        "Page Title": {"title": {}},
        "Status": {
            "select": {
                "options": [
                    {"name": "Draft", "color": "gray"},
                    {"name": "Pending Review", "color": "yellow"},
                    {"name": "Approved", "color": "green"},
                    {"name": "Revision Requested", "color": "red"},
                ]
            }
        },
        "Figma URL": {"url": {}},
        "Component Count": {"number": {}},
        "Client Feedback": {"rich_text": {}},
    }


def high_fid_schema() -> dict:
    return {
        "Title": {"title": {}},
        "Status": {
            "select": {
                "options": [
                    {"name": "Brief Ready", "color": "blue"},
                    {"name": "In Design", "color": "yellow"},
                    {"name": "Pending Review", "color": "orange"},
                    {"name": "Approved", "color": "green"},
                    {"name": "Revision Requested", "color": "red"},
                ]
            }
        },
        "Desktop Figma URL": {"url": {}},
        "Mobile Figma URL": {"url": {}},
        "Client Feedback": {"rich_text": {}},
    }


def content_schema() -> dict:
    return {
        "Page Title": {"title": {}},
        "Slug": {"rich_text": {}},
        "Page Type": {
            "select": {
                "options": [
                    {"name": "Static", "color": "blue"},
                    {"name": "CMS", "color": "green"},
                ]
            }
        },
        "Content Mode": {
            "select": {
                "options": [
                    {"name": "AI Generated", "color": "purple"},
                    {"name": "Client Provided", "color": "yellow"},
                ]
            }
        },
        "Status": {
            "select": {
                "options": [
                    {"name": "Draft", "color": "gray"},
                    {"name": "Team Review", "color": "blue"},
                    {"name": "Client Review", "color": "yellow"},
                    {"name": "Client Providing", "color": "orange"},
                    {"name": "Approved", "color": "green"},
                    {"name": "Revision Requested", "color": "red"},
                ]
            }
        },
        "Title Tag": {"rich_text": {}},
        "Meta Description": {"rich_text": {}},
        "H1": {"rich_text": {}},
        "SEO Keywords": {"rich_text": {}},
        "Word Count": {"number": {}},
    }


def images_schema() -> dict:
    return {
        "Image Name": {"title": {}},
        "Batch": {
            "select": {
                "options": [
                    {"name": "Brand Creative", "color": "blue"},
                    {"name": "Page Content",   "color": "green"},
                    {"name": "Stock",          "color": "purple"},
                ]
            }
        },
        "Category": {
            "select": {
                "options": [
                    {"name": "Hero Lifestyle",     "color": "blue"},
                    {"name": "Detail Close-Up",    "color": "pink"},
                    {"name": "Texture Background", "color": "gray"},
                    {"name": "Environment",        "color": "green"},
                    {"name": "Product Flat Lay",   "color": "purple"},
                    {"name": "Brand Abstract",     "color": "orange"},
                    {"name": "Page Feature",       "color": "yellow"},
                    {"name": "People — Candid",    "color": "red"},
                    {"name": "Clinic / Environment","color": "brown"},
                    {"name": "Abstract / Texture", "color": "gray"},
                ]
            }
        },
        "Status": {
            "select": {
                "options": [
                    {"name": "Generated",       "color": "blue"},
                    {"name": "Candidate",       "color": "purple"},
                    {"name": "Approved",        "color": "green"},
                    {"name": "Rejected",        "color": "red"},
                    {"name": "Revision Needed", "color": "yellow"},
                ]
            }
        },
        "Page":              {"rich_text": {}},
        "Image URL":         {"url": {}},
        "Source":            {"rich_text": {}},
        "Prompt Used":       {"rich_text": {}},
        "Replicate Job ID":  {"rich_text": {}},
        "Mood Board Option": {"rich_text": {}},
    }


def care_plan_schema() -> dict:
    return {
        "Name": {"title": {}},  # e.g. "Summit Therapy — April 2026"
        "Report Date": {"date": {}},
        "Site URL": {"url": {}},
        "Mobile Score": {"number": {}},
        "Desktop Score": {"number": {}},
        "Mobile Rating": {
            "select": {
                "options": [
                    {"name": "Good (90–100)", "color": "green"},
                    {"name": "Needs Improvement (50–89)", "color": "yellow"},
                    {"name": "Poor (0–49)", "color": "red"},
                ]
            }
        },
        "Desktop Rating": {
            "select": {
                "options": [
                    {"name": "Good (90–100)", "color": "green"},
                    {"name": "Needs Improvement (50–89)", "color": "yellow"},
                    {"name": "Poor (0–49)", "color": "red"},
                ]
            }
        },
        "Mobile Metrics":  {"rich_text": {}},
        "Desktop Metrics": {"rich_text": {}},
        "Insights":        {"rich_text": {}},
        "Recommendations": {"rich_text": {}},
        "ADA Widget": {"checkbox": {}},
        "Privacy Policy": {
            "select": {
                "options": [
                    {"name": "Current", "color": "green"},
                    {"name": "Needs Update", "color": "yellow"},
                    {"name": "Not Set", "color": "gray"},
                ]
            }
        },
        "Terms of Service": {
            "select": {
                "options": [
                    {"name": "Current", "color": "green"},
                    {"name": "Needs Update", "color": "yellow"},
                    {"name": "Not Set", "color": "gray"},
                ]
            }
        },
        "Hours Used": {"number": {}},
        "Notes": {"rich_text": {}},
    }


def competitors_schema() -> dict:
    """
    Schema for the Competitors DB.
    Covers Local SEO Competitor Analysis + Organic Competitor Analysis + Authority Gap
    from the SEO Battle Plan workbook. One row per competitor per client.
    """
    return {
        # ── Identity (primary view columns) ──────────────────────────────────
        "Competitor Name": {"title": {}},
        "Type": {
            "select": {
                "options": [
                    {"name": "Local",   "color": "blue"},
                    {"name": "Organic", "color": "purple"},
                    {"name": "Both",    "color": "green"},
                ]
            }
        },
        "Website":         {"url": {}},
        "Threat": {
            "select": {
                "options": [
                    {"name": "High",   "color": "red"},
                    {"name": "Medium", "color": "yellow"},
                    {"name": "Low",    "color": "green"},
                ]
            }
        },
        "Multi-Location":  {"checkbox": {}},
        # ── GBP signals ───────────────────────────────────────────────────────
        "GBP URL":                    {"url": {}},
        "Review Count":               {"number": {}},
        "Review Rating":              {"number": {}},
        "Review Velocity":            {"rich_text": {}},  # e.g. "< 5/month", "1/week"
        "Professional Quality Images":{"checkbox": {}},   # manually checked by team
        # ── SERP performance ──────────────────────────────────────────────────
        "Keyword Count":   {"number": {}},
        "Avg Position":    {"number": {}},
        "AI Mentions":     {"number": {}},
        # ── Authority / backlink data ─────────────────────────────────────────
        "Authority Score":    {"number": {}},
        "Referring Domains":  {"number": {}},
        "Backlinks":          {"number": {}},
        "Local Backlinks":    {"rich_text": {}},  # notes on local/civic links
        "Industry Links":     {"rich_text": {}},  # notes on industry links
        "Link Gap Notes":     {"rich_text": {}},  # gap analysis narrative
        # ── GBP detail (manual) ───────────────────────────────────────────────
        "Reviews Last 30 Days":  {"number": {}},   # manual — count from GBP
        "Last Photo Added":      {"rich_text": {}}, # e.g. "1 year ago", "2 months ago"
        "Has Posts":             {"checkbox": {}},
        "Service Menu Complete": {"checkbox": {}},
        "Network Presence":      {"rich_text": {}}, # list of directories
        # ── Organic page analysis ─────────────────────────────────────────────
        "Top Ranking Page":  {"url": {}},
        "Target Cluster":    {"rich_text": {}},
        "Content Depth": {
            "select": {
                "options": [
                    {"name": "Short",       "color": "red"},
                    {"name": "Medium",      "color": "yellow"},
                    {"name": "Medium-Long", "color": "blue"},
                    {"name": "Long",        "color": "green"},
                ]
            }
        },
        "Uses FAQs":    {"checkbox": {}},
        "Uses Schema":  {"rich_text": {}},  # which schema types
        "EEAT Signals": {"rich_text": {}},
        "Page Type":    {"rich_text": {}},
        # ── Analysis ─────────────────────────────────────────────────────────
        "Strengths":  {"rich_text": {}},
        "Weaknesses": {"rich_text": {}},
        "Notes":      {"rich_text": {}},
    }


def keywords_schema() -> dict:
    """
    Schema for the Keywords DB.
    Covers the Keyword Mapping tab from the SEO Battle Plan workbook.
    One row per target keyword per client.
    """
    return {
        "Keyword": {"title": {}},
        "Cluster": {"rich_text": {}},  # e.g. "Core Substance Abuse", "Mental Health"
        "Monthly Search Volume": {"rich_text": {}},  # rich_text: handles "Low-Vol / High-Intent"
        "Intent": {
            "select": {
                "options": [
                    {"name": "Informational",   "color": "blue"},
                    {"name": "Commercial",      "color": "purple"},
                    {"name": "Transactional",   "color": "green"},
                    {"name": "Local",           "color": "orange"},
                    {"name": "Navigational",    "color": "gray"},
                ]
            }
        },
        "Our Position":          {"rich_text": {}},  # current rank or "-"
        "Competitor Positions":  {"rich_text": {}},  # "Crestview: 1, TreeHouse: 2"
        "Priority": {
            "select": {
                "options": [
                    {"name": "High",   "color": "red"},
                    {"name": "Medium", "color": "yellow"},
                    {"name": "Low",    "color": "gray"},
                ]
            }
        },
        "Type": {
            "select": {
                "options": [
                    {"name": "GBP",           "color": "green"},
                    {"name": "Landing Page",  "color": "blue"},
                    {"name": "Blog",          "color": "purple"},
                    {"name": "Home",          "color": "orange"},
                    {"name": "Service Hub",   "color": "yellow"},
                ]
            }
        },
        "Target Page":       {"rich_text": {}},  # which page should rank for this
        "Location Modifier": {"rich_text": {}},  # e.g. "Portland OR", "NE Portland"
        "Status": {
            "select": {
                "options": [
                    {"name": "Target",   "color": "gray"},
                    {"name": "Ranking",  "color": "yellow"},
                    {"name": "Won",      "color": "green"},
                ]
            }
        },
        "Notes": {"rich_text": {}},
    }


def seo_metrics_schema() -> dict:
    """
    Schema for the SEO Metrics DB.
    Monthly performance rows — full SEO clients only.
    Covers the Benchmark Metrics tab from the SEO workbook.
    Created by `make seo-activate`, not at initial onboarding.
    """
    return {
        "Month": {"title": {}},  # e.g. "April 2026"
        "Report Date": {"date": {}},
        # ── GBP ──────────────────────────────────────────────────────────────
        "GBP Impressions":   {"number": {}},
        "GBP Calls":         {"number": {}},
        "GBP Clicks":        {"number": {}},
        "GBP Score":         {"number": {}},
        # ── Site / organic ───────────────────────────────────────────────────
        "Organic Sessions":          {"number": {}},
        "Domain Authority":          {"number": {}},
        "Referring Domains":         {"number": {}},
        "Backlinks":                 {"number": {}},
        "Organic Conversion Rate":   {"number": {}},
        "Organic Engagement Rate":   {"number": {}},
        "Branded Clicks":            {"number": {}},
        "Non-Branded Clicks":        {"number": {}},
        # ── Citations ────────────────────────────────────────────────────────
        "Citation Score":       {"number": {}},
        "Data Aggregators":     {"number": {}},
        # ── Technical ────────────────────────────────────────────────────────
        "PageSpeed Mobile":   {"number": {}},
        "PageSpeed Desktop":  {"number": {}},
        "404 Errors":         {"number": {}},
        "4XX Errors":         {"number": {}},
        "5XX Errors":         {"number": {}},
        # ── LLM visibility (0=not visible, 1=mentioned, 2=recommended) ───────
        "LLM Gemini":      {"number": {}},
        "LLM ChatGPT":     {"number": {}},
        "LLM Perplexity":  {"number": {}},
        # ── Reviews ──────────────────────────────────────────────────────────
        "Review Count":          {"number": {}},
        "New Reviews This Month": {"number": {}},
        # ── Meta ─────────────────────────────────────────────────────────────
        "Data Source": {
            "select": {
                "options": [
                    {"name": "API",    "color": "green"},
                    {"name": "Manual", "color": "yellow"},
                    {"name": "Mixed",  "color": "blue"},
                ]
            }
        },
        "Notes": {"rich_text": {}},
    }


def action_items_schema() -> dict:
    return {
        "Task": {"title": {}},
        "Assigned To": {
            "select": {
                "options": [
                    {"name": "Agency", "color": "blue"},
                    {"name": "Client", "color": "green"},
                ]
            }
        },
        "Status": {
            "select": {
                "options": [
                    {"name": "To Do", "color": "gray"},
                    {"name": "In Progress", "color": "yellow"},
                    {"name": "Done", "color": "green"},
                ]
            }
        },
        "Due Date": {"date": {}},
        "Source Meeting": {"rich_text": {}},
        "ClickUp Task ID": {"rich_text": {}},
    }


def blog_posts_schema() -> dict:
    """
    Schema for the Blog Posts DB.
    One row per blog post — covers the full lifecycle from idea to published.
    Created automatically by `make blog-ideas` if it doesn't exist.
    """
    return {
        "Title":                    {"title": {}},
        "Status": {
            "select": {
                "options": [
                    {"name": "Idea",         "color": "gray"},
                    {"name": "Approved",     "color": "green"},
                    {"name": "Draft",        "color": "blue"},
                    {"name": "Under Review", "color": "yellow"},
                    {"name": "Image Needed", "color": "orange"},
                    {"name": "Scheduled",    "color": "purple"},
                    {"name": "Published",    "color": "pink"},
                ]
            }
        },
        "Target Keyword":           {"rich_text": {}},
        "Search Intent":            {"rich_text": {}},
        "Internal Link Target":     {"rich_text": {}},
        "Publish Month": {
            "select": {
                "options": [
                    {"name": "Month 1", "color": "blue"},
                    {"name": "Month 2", "color": "green"},
                    {"name": "Month 3", "color": "yellow"},
                ]
            }
        },
        "Suggested Publish Date":   {"date": {}},
        "Author Name":              {"rich_text": {}},
        "Reviewer Name":            {"rich_text": {}},
        "Reviewer Credentials":     {"rich_text": {}},
        "Review Date":              {"date": {}},
        "Published URL":            {"url": {}},
        "Cross-Client Link Suggestion": {"rich_text": {}},
        "Word Count":               {"number": {}},
        "Title Tag":                {"rich_text": {}},
        "Meta Description":         {"rich_text": {}},
        "H1":                       {"rich_text": {}},
        "Primary Keyword":          {"rich_text": {}},
        "Feedback":                 {"rich_text": {}},
    }


def _stage_color(stage: PipelineStage) -> str:
    colors = ["gray", "blue", "green", "yellow", "orange", "purple", "red", "pink"]
    stages = list(PipelineStage)
    return colors[stages.index(stage) % len(colors)]


# ── Main setup logic ──────────────────────────────────────────────────────────

async def setup_client(client_name: str, contact_email: str, dry_run: bool = False) -> dict:
    print(f"\n{'[DRY RUN] ' if dry_run else ''}Setting up Notion for: {client_name}")
    print("=" * 60)

    notion = NotionClient(settings.notion_api_key)

    # ── Pass 1: Create client page + all 8 databases ──────────────────────────
    print("\nPass 1: Creating client page and databases...")

    if not dry_run:
        client_page_id = await notion.create_page(
            parent_page_id=settings.notion_workspace_root_page_id,
            title=client_name,
        )
        print(f"  ✓ Client page created: {client_page_id}")
    else:
        client_page_id = "DRY_RUN_PAGE_ID"
        print(f"  [DRY RUN] Would create client page under {settings.notion_workspace_root_page_id}")

    databases: dict[str, str] = {}  # name → database_id

    db_definitions = [
        ("Client Info", client_info_schema()),
        ("Meeting Notes & Transcripts", meeting_notes_schema()),
        ("Brand Guidelines", brand_guidelines_schema()),
        ("Mood Board", mood_board_schema()),
        ("Sitemap", sitemap_schema()),
        ("Page Content", content_schema()),
        ("Wireframes", wireframes_schema()),
        ("High-Fidelity Design", high_fid_schema()),
        ("Action Items", action_items_schema()),
        ("Images", images_schema()),
        ("Care Plan", care_plan_schema()),
        # ── SEO — Battle Plan DBs (always created, even for website-only clients) ──
        ("Competitors", competitors_schema()),
        ("Keywords", keywords_schema()),
        # Note: SEO Metrics DB is created separately by `make seo-activate`
        # (only for clients on the full SEO retainer)
    ]

    for db_name, schema in db_definitions:
        if not dry_run:
            db_id = await notion.create_database(
                parent_page_id=client_page_id,
                title=db_name,
                properties_schema=schema,
            )
            databases[db_name] = db_id
            print(f"  ✓ {db_name}: {db_id}")
        else:
            print(f"  [DRY RUN] Would create database: {db_name}")

    # ── Pass 2: Add relation properties ──────────────────────────────────────
    if not dry_run:
        print("\nPass 2: Adding relation properties...")

        # Meeting Notes → Client Info
        await notion.update_database(
            database_id=databases["Meeting Notes & Transcripts"],
            properties_schema={
                "Client": {
                    "relation": {
                        "database_id": databases["Client Info"],
                        "single_property": {},
                    }
                }
            },
        )
        print("  ✓ Meeting Notes → Client Info relation added")

        # Add more relations as needed...
        # Action Items → Meeting Notes relation
        await notion.update_database(
            database_id=databases["Action Items"],
            properties_schema={
                "Source Meeting": {
                    "relation": {
                        "database_id": databases["Meeting Notes & Transcripts"],
                        "single_property": {},
                    }
                }
            },
        )
        print("  ✓ Action Items → Meeting Notes relation added")
    else:
        print("\n[DRY RUN] Would add relation properties in Pass 2")

    # ── Create initial Client Info entry ──────────────────────────────────────
    if not dry_run:
        print("\nCreating initial Client Info entry...")
        entry_id = await notion.create_database_entry(
            database_id=databases["Client Info"],
            properties={
                "Name": notion.title_property(client_name),
            },
        )
        print(f"  ✓ Client Info entry: {entry_id}")

    # ── Print summary ─────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("SETUP COMPLETE" if not dry_run else "DRY RUN COMPLETE")
    print("=" * 60)
    if not dry_run:
        print(f"\nClient page ID: {client_page_id}")
        print("\nDatabase IDs (add these to your .env or client record):")
        for name, db_id in databases.items():
            print(f"  {name}: {db_id}")
        print("\nNext steps:")
        print("  1. Add NOTION_CLIENT_PAGE_ID to your .env or run setup_clickup.py")
        print("  2. Share the Notion page with your integration (Settings → Connections)")
        print("  3. Run: python scripts/migrate_from_gdrive.py (if migrating existing client)")

    return {
        "client_page_id": client_page_id if not dry_run else "",
        "databases": databases if not dry_run else {},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Set up Notion structure for a new client")
    parser.add_argument("--client-name", required=True, help="Client/company name")
    parser.add_argument("--contact-email", required=True, help="Client primary email")
    parser.add_argument("--dry-run", action="store_true", help="Preview without creating anything")
    args = parser.parse_args()

    asyncio.run(setup_client(
        client_name=args.client_name,
        contact_email=args.contact_email,
        dry_run=args.dry_run,
    ))


if __name__ == "__main__":
    main()
