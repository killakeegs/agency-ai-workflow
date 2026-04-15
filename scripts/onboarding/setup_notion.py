#!/usr/bin/env python3
"""
setup_notion.py — Provision a new client in Notion

Creates the Notion structure for a new client:
  1. A top-level client page under NOTION_WORKSPACE_ROOT_PAGE_ID
  2. Four base databases (Client Info, Client Log, Brand Guidelines, Care Plan)
  3. A Business Profile page with universal + vertical-specific sections
  4. Service-specific databases (Sitemap, Content, Images, etc.) based on services config

The old 13-database-per-client structure was replaced Apr 2026.
Mood Board, Wireframes, Hi-Fi Design, and Action Items DBs are no longer created.

IMPORTANT — Two-pass creation:
  Notion requires a database to exist before another database can reference it
  via a "relation" property. So we create databases in Pass 1, then
  add relation properties in Pass 2.

Usage:
    python scripts/onboarding/setup_notion.py --client-name "ACME Corp" --contact-email "jane@acme.com"
    python scripts/onboarding/setup_notion.py --client-name "Test Client" --dry-run
    python scripts/onboarding/setup_notion.py --client-name "Summit Therapy" --verticals speech_pathology occupational_therapy physical_therapy

After running, add the printed database IDs to your .env or store them
in the Client Info database entry for this client.
"""
import argparse
import asyncio
import sys
from pathlib import Path

# Allow running as a script from the project root
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.config import settings
from src.integrations.notion import NotionClient
from src.models.pipeline import PipelineStage


# ── Database schema definitions ───────────────────────────────────────────────

def client_info_schema() -> dict:
    """Schema for the Client Info database — expanded Apr 2026."""
    pipeline_options = [
        {"name": stage.value, "color": _stage_color(stage)}
        for stage in PipelineStage
    ]
    return {
        # ── Contact & Company ────────────────────────────────────────────────
        "Name": {"title": {}},
        "Company": {"rich_text": {}},
        "Email": {"email": {}},
        "Phone": {"phone_number": {}},
        "Website": {"url": {}},
        "Primary Contact Name": {"rich_text": {}},
        "Primary Contact Email": {"email": {}},
        "Client Contacts": {"rich_text": {}},  # comma-separated emails for Rex email monitoring
        # ── Business Classification ──────────────────────────────────────────
        "Business Type": {
            "select": {
                "options": [
                    {"name": "Local Business",    "color": "blue"},
                    {"name": "National Business",  "color": "green"},
                    {"name": "E-Commerce",         "color": "purple"},
                    {"name": "Service Business",   "color": "yellow"},
                    {"name": "SaaS / Tech",        "color": "red"},
                ]
            }
        },
        "Vertical": {"rich_text": {}},  # e.g. "speech_pathology, occupational_therapy"
        # ── Services & Retainer ──────────────────────────────────────────────
        "Services": {
            "multi_select": {
                "options": [
                    {"name": "Website Build",    "color": "blue"},
                    {"name": "Care Plan",        "color": "green"},
                    {"name": "SEO",              "color": "purple"},
                    {"name": "GBP Management",   "color": "orange"},
                    {"name": "Blog",             "color": "yellow"},
                    {"name": "Social Media",     "color": "pink"},
                    {"name": "Newsletter",       "color": "gray"},
                    {"name": "Paid Ads",         "color": "red"},
                ]
            }
        },
        "Monthly Retainer": {"number": {}},
        "Account Manager": {"rich_text": {}},
        # ── Pipeline ─────────────────────────────────────────────────────────
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
        # ── Design & Build ───────────────────────────────────────────────────
        "Template": {"rich_text": {}},  # which Webflow template (e.g. "speech_pathology_v1")
        "Figma Desktop URL": {"url": {}},
        "Figma Mobile URL": {"url": {}},
        # ── External IDs ─────────────────────────────────────────────────────
        "ClickUp Folder ID": {"rich_text": {}},
        "Project Start": {"date": {}},
        "Timeline (Weeks)": {"number": {}},
        "Notes": {"rich_text": {}},
    }


def client_log_schema() -> dict:
    """
    Client Log DB — single chronological timeline of every client interaction.
    Replaces the old Meeting Notes + Action Items DBs.
    """
    return {
        "Title": {"title": {}},
        "Date": {"date": {}},
        "Type": {
            "select": {
                "options": [
                    {"name": "Meeting",        "color": "blue"},
                    {"name": "Email Inbound",  "color": "green"},
                    {"name": "Email Outbound", "color": "purple"},
                    {"name": "Phone Call",     "color": "yellow"},
                ]
            }
        },
        "Meeting Type": {
            "select": {
                "options": [
                    {"name": "Kickoff",          "color": "blue"},
                    {"name": "Pipeline Review",  "color": "green"},
                    {"name": "Content Review",   "color": "yellow"},
                    {"name": "Design Review",    "color": "orange"},
                    {"name": "Check-in",         "color": "gray"},
                    {"name": "Ad Hoc",           "color": "pink"},
                ]
            }
        },
        "Attendees": {"rich_text": {}},
        "Duration (min)": {"number": {}},
        "Pipeline Stage": {"rich_text": {}},  # auto-tagged by Rex
        # ── Meeting notes sections (populated by Rex) ────────────────────────
        "Summary": {"rich_text": {}},
        "Key Decisions": {"rich_text": {}},
        "Approvals Given": {"rich_text": {}},
        "Action Items": {"rich_text": {}},       # structured: who | what | due date
        "Revision Feedback": {"rich_text": {}},   # stored for next agent run
        "Client Requests": {"rich_text": {}},
        "Brand Updates": {"rich_text": {}},       # preferences that should propagate
        "Client Quotes": {"rich_text": {}},       # verbatim phrases for content voice
        "Value Add Opportunities": {"rich_text": {}},
        "Risk Flags": {"rich_text": {}},
        "Client Sentiment": {"rich_text": {}},
        "Next Steps": {"rich_text": {}},
        # ── Processing ───────────────────────────────────────────────────────
        "Processed": {"checkbox": {}},           # Rex marks True after processing
        "Follow-Up Sent": {"checkbox": {}},      # True after email sent
        "Tasks Created": {"number": {}},         # count of ClickUp tasks created
        "Source": {"rich_text": {}},             # email subject or transcript link
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


# ── Top-level Clients DB (one per workspace) ─────────────────────────────────

def clients_db_schema() -> dict:
    """
    Master Clients database — one row per client across the entire workspace.
    This is the agency command center: shows all clients at a glance.
    Created once via: python scripts/onboarding/setup_notion.py --setup-clients-db
    """
    return {
        "Client Name": {"title": {}},
        "Status": {
            "select": {
                "options": [
                    {"name": "Active",      "color": "green"},
                    {"name": "Onboarding",  "color": "blue"},
                    {"name": "Paused",      "color": "yellow"},
                    {"name": "Completed",   "color": "gray"},
                ]
            }
        },
        "Services": {
            "multi_select": {
                "options": [
                    {"name": "Website Build",    "color": "blue"},
                    {"name": "Care Plan",        "color": "green"},
                    {"name": "SEO",              "color": "purple"},
                    {"name": "GBP Management",   "color": "orange"},
                    {"name": "Blog",             "color": "yellow"},
                    {"name": "Social Media",     "color": "pink"},
                    {"name": "Newsletter",       "color": "gray"},
                    {"name": "Paid Ads",         "color": "red"},
                ]
            }
        },
        "Vertical": {"rich_text": {}},
        "Pipeline Stage": {"rich_text": {}},
        "Monthly Retainer": {"number": {}},
        "Primary Contact": {"rich_text": {}},
        "Contact Email": {"email": {}},
        "Account Manager": {"rich_text": {}},
        "Start Date": {"date": {}},
        "Client Page": {"url": {}},  # link to the client's Notion page
        "Notes": {"rich_text": {}},
    }


# ── Business Profile page builder ────────────────────────────────────────────

# Universal sections — every client gets these
UNIVERSAL_SECTIONS = [
    ("Company Credentials & Accreditations",
     "Accrediting bodies (CARF, Joint Commission, LegitScript), state licenses, "
     "industry memberships, years in operation, awards, number of locations."),
    ("Specialized Populations",
     "Who specifically does this practice serve? Age-specific, cultural/language, "
     "military/veterans, LGBTQ+, neurodivergent, athletes, executives, court-ordered, "
     "dual diagnosis, specific conditions they're known for."),
    ("Staffing & Team",
     "Staff-to-client ratio, total team size, key leadership (founder, medical director, "
     "clinical director), credentials held across team, hiring philosophy, multilingual staff."),
    ("Services Overview",
     "Each service offered with detail: who it's for, what it involves, duration/frequency, "
     "expected outcomes, how it differs from competitors' version of the same service."),
    ("Insurance & Payment",
     "In-network payers (specific list), out-of-network benefits, Medicaid (which state "
     "programs), Medicare, Tricare, workers comp, private pay rates or ranges, sliding scale, "
     "VOB process, superbills."),
    ("Admissions & Intake",
     "How someone gets started (call, form, walk-in), response time expectation, assessment "
     "process, wait time, what to expect on day one, who handles intake, referral process for providers."),
    ("Facility & Environment",
     "Setting description (what does it feel/look like), amenities, ADA/accessibility, "
     "telehealth availability, multi-location differences, capacity."),
    ("Outcomes & Results",
     "What outcomes can be claimed (with compliance guardrails), completion rates, "
     "patient satisfaction scores, testimonial policy, before/after policies."),
    ("Referral Network",
     "Who refers patients to them (physicians, schools, courts, EAPs), who they refer out to, "
     "partnership organizations, community involvement."),
    ("Compliance & Legal",
     "HIPAA considerations for marketing, state-specific advertising regulations, "
     "outcome claims they can/can't make, testimonial policies, photo consent process, "
     "required disclaimers by platform (Google Ads, Meta)."),
    ("Tech Stack",
     "EMR/EHR, scheduling/booking, billing, patient portal, telehealth platform, CRM, "
     "review management, call tracking, analytics already in place."),
    ("Common Objections & FAQs",
     "Real questions patients/clients ask, what makes people hesitate, "
     "common misconceptions about their services."),
]

# Vertical-specific sections — appended based on client's verticals
VERTICAL_SECTIONS: dict[str, list[tuple[str, str]]] = {
    "addiction_treatment": [
        ("Levels of Care",
         "Detox, residential, PHP, IOP, outpatient — which levels are offered, "
         "capacity for each, typical pathway through levels."),
        ("Treatment Philosophy",
         "12-step, MAT-based, holistic, faith-based, dual diagnosis, trauma-informed — "
         "what is the core approach and what alternatives are offered?"),
        ("Medications",
         "Suboxone, vivitrol, naltrexone, antidepressants — stance on each, "
         "what's prescribed, what's not, MAT philosophy."),
        ("Length of Stay",
         "Typical duration per program level, factors that extend/shorten stay, "
         "insurance coverage for each duration."),
        ("Substances Treated",
         "Full list of substances treated, any they don't treat, "
         "co-occurring mental health conditions addressed."),
        ("Court-Ordered & Legal",
         "Court-ordered referral process, drug court relationships, "
         "legal advocacy or case management services."),
        ("Family & Alumni Programs",
         "Family therapy, family education, visitation policies, "
         "alumni/aftercare program, sober living partnerships."),
    ],
    "speech_pathology": [
        ("Age Groups & Settings",
         "Pediatric, adult, geriatric — which populations served, "
         "clinic-based, school-based, home visits, teletherapy."),
        ("Evaluation Types",
         "Speech-language evaluations offered, standardized assessments used, "
         "evaluation process and timeline."),
        ("Treatment Areas",
         "Articulation, fluency/stuttering, feeding/swallowing, receptive/expressive language, "
         "AAC (augmentative/alternative communication), voice, social communication, accent modification."),
        ("Parent & Caregiver Involvement",
         "Parent coaching approach, home program philosophy, "
         "carryover strategies, how parents are included in sessions."),
        ("School Collaboration",
         "IEP/IFSP collaboration, school-based services vs private, "
         "coordination with teachers and school teams."),
        ("Session Norms & Milestones",
         "Typical session frequency and duration, developmental milestones referenced, "
         "discharge criteria, progress measurement approach."),
    ],
    "occupational_therapy": [
        ("Age Groups & Settings",
         "Pediatric, adult, geriatric — which populations, "
         "clinic-based, school-based, home health, teletherapy."),
        ("Specialties",
         "Sensory processing, fine motor, ADLs, hand therapy, pediatric development, "
         "visual motor, self-regulation, executive functioning."),
        ("Evaluation Tools",
         "Standardized assessments used (BOT-2, Beery VMI, Sensory Profile, etc.), "
         "evaluation process and timeline."),
        ("Home Programs & Equipment",
         "Home program philosophy, adaptive equipment recommended, "
         "sensory diet design, environmental modifications."),
        ("School-Based vs Outpatient",
         "Differences in approach, IEP goals vs clinical goals, "
         "coordination with school OTs."),
    ],
    "physical_therapy": [
        ("Specialties",
         "Orthopedic, neurological, pediatric, sports, pelvic floor, "
         "vestibular, geriatric, post-surgical."),
        ("Pre/Post-Surgical Protocols",
         "Which surgeries, prehab programs, post-surgical timelines, "
         "physician coordination process."),
        ("Equipment & Modalities",
         "Equipment used (dry needling, TENS, ultrasound, taping), "
         "manual therapy approach, exercise progression philosophy."),
        ("Physician Referral & Workers Comp",
         "Referral requirements by state, direct access policies, "
         "workers comp process, auto accident/PI cases."),
    ],
    "dermatology": [
        ("Medical vs Cosmetic",
         "What percentage medical vs cosmetic, how they're positioned, "
         "which providers handle which."),
        ("Procedures Offered",
         "Full list with detail: medical (biopsies, excisions, Mohs) and "
         "cosmetic (Botox, fillers, lasers, peels, microneedling)."),
        ("Product Lines",
         "Skincare lines carried, private label products, "
         "how products are recommended and sold."),
        ("Before/After & Consultations",
         "Before/after photo policies, consent process, "
         "consultation flow for cosmetic procedures."),
        ("Seasonal Promotions",
         "Treatment calendars, seasonal specials, "
         "membership/loyalty programs."),
    ],
    "mental_health": [
        ("Therapy Modalities",
         "CBT, DBT, EMDR, psychodynamic, ACT, motivational interviewing — "
         "which modalities are offered and by whom."),
        ("Individual vs Group vs Family",
         "Session formats offered, group therapy topics, "
         "couples/family therapy approach."),
        ("Crisis & Emergency Protocols",
         "Crisis response, safety planning, after-hours availability, "
         "coordination with emergency services."),
        ("Psychiatry vs Therapy",
         "Is psychiatry offered in-house? Medication management approach, "
         "collaborative care model."),
        ("Specialized Populations",
         "Trauma, couples, adolescents, LGBTQ+, perinatal, grief, "
         "specific populations they specialize in."),
    ],
}


def _build_business_profile_blocks(
    business_name: str,
    verticals: list[str],
) -> list[dict]:
    """
    Build Notion blocks for a Business Profile page.
    Includes universal sections + vertical-specific sections.
    """
    def h1(text: str) -> dict:
        return {"object": "block", "type": "heading_1", "heading_1": {
            "rich_text": [{"type": "text", "text": {"content": text}}]
        }}

    def h2(text: str) -> dict:
        return {"object": "block", "type": "heading_2", "heading_2": {
            "rich_text": [{"type": "text", "text": {"content": text}}]
        }}

    def h3(text: str) -> dict:
        return {"object": "block", "type": "heading_3", "heading_3": {
            "rich_text": [{"type": "text", "text": {"content": text}}]
        }}

    def p(text: str) -> dict:
        return {"object": "block", "type": "paragraph", "paragraph": {
            "rich_text": [{"type": "text", "text": {"content": text[:1900]}}]
        }}

    def callout(text: str, emoji: str = "📝") -> dict:
        return {"object": "block", "type": "callout", "callout": {
            "rich_text": [{"type": "text", "text": {"content": text[:1900]}}],
            "icon": {"type": "emoji", "emoji": emoji},
        }}

    def divider() -> dict:
        return {"object": "block", "type": "divider", "divider": {}}

    blocks: list[dict] = []

    # Header
    blocks.append(callout(
        f"This is the Business Profile for {business_name}. "
        "Fill in each section with as much detail as possible. "
        "Agents read this page before generating any content. "
        "The deeper the profile, the better every output.",
        "🏢",
    ))

    # Universal sections
    blocks.append(h1("Business Profile"))
    for section_name, prompt_text in UNIVERSAL_SECTIONS:
        blocks.append(h2(section_name))
        blocks.append(callout(prompt_text, "📝"))
        blocks.append(p(""))  # empty paragraph for content entry

    # Vertical-specific sections
    for vertical in verticals:
        sections = VERTICAL_SECTIONS.get(vertical)
        if not sections:
            continue

        vertical_label = vertical.replace("_", " ").title()
        blocks.append(divider())
        blocks.append(h1(f"{vertical_label} — Vertical Details"))

        for section_name, prompt_text in sections:
            blocks.append(h2(section_name))
            blocks.append(callout(prompt_text, "📝"))
            blocks.append(p(""))

    return blocks


# ── Main setup logic ──────────────────────────────────────────────────────────

async def setup_clients_db(dry_run: bool = False) -> str:
    """
    Create the top-level Clients DB (one per workspace).
    Run once: python scripts/onboarding/setup_notion.py --setup-clients-db
    """
    print(f"\n{'[DRY RUN] ' if dry_run else ''}Creating top-level Clients DB...")
    notion = NotionClient(settings.notion_api_key)

    if not dry_run:
        db_id = await notion.create_database(
            parent_page_id=settings.notion_workspace_root_page_id,
            title="Clients",
            properties_schema=clients_db_schema(),
        )
        print(f"  ✓ Clients DB created: {db_id}")
        print(f"\n  Add this to your .env: NOTION_CLIENTS_DB_ID={db_id}")
        return db_id
    else:
        print(f"  [DRY RUN] Would create Clients DB under {settings.notion_workspace_root_page_id}")
        return ""


async def setup_client(
    client_name: str,
    contact_email: str = "",
    dry_run: bool = False,
    services: list[str] | None = None,
    verticals: list[str] | None = None,
) -> dict:
    """
    Provision a new client in Notion.

    Creates:
      1. Client page under workspace root
      2. Base databases: Client Info, Client Log, Brand Guidelines, Care Plan
      3. Business Profile page (universal + vertical-specific sections)
      4. Service-specific databases based on `services` list

    Args:
        client_name: Business name
        contact_email: Primary contact email
        dry_run: Preview without creating anything
        services: List of active services (e.g. ["website_build", "care_plan"])
        verticals: List of industry verticals (e.g. ["speech_pathology", "occupational_therapy"])
    """
    services  = services  or ["website_build", "care_plan"]
    verticals = verticals or []

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Setting up Notion for: {client_name}")
    print("=" * 60)
    print(f"  Services:  {', '.join(services)}")
    print(f"  Verticals: {', '.join(verticals) or '(none)'}")

    notion = NotionClient(settings.notion_api_key)

    # ── Create client page ────────────────────────────────────────────────────
    print("\nCreating client page...")

    if not dry_run:
        client_page_id = await notion.create_page(
            parent_page_id=settings.notion_workspace_root_page_id,
            title=client_name,
        )
        print(f"  ✓ Client page: {client_page_id}")
    else:
        client_page_id = "DRY_RUN_PAGE_ID"
        print(f"  [DRY RUN] Would create client page under {settings.notion_workspace_root_page_id}")

    databases: dict[str, str] = {}  # name → database_id

    # ── Pass 1: Create databases ──────────────────────────────────────────────
    # Base databases (always created)
    base_dbs = [
        ("Client Info",       client_info_schema()),
        ("Client Log",        client_log_schema()),
        ("Brand Guidelines",  brand_guidelines_schema()),
        ("Care Plan",         care_plan_schema()),
    ]

    # Service-specific databases
    service_dbs: list[tuple[str, dict]] = []
    if "website_build" in services:
        service_dbs += [
            ("Sitemap",      sitemap_schema()),
            ("Page Content", content_schema()),
            ("Images",       images_schema()),
        ]
    if "seo" in services:
        service_dbs += [
            ("Competitors", competitors_schema()),
            ("Keywords",    keywords_schema()),
        ]
    # Blog, Social, GBP databases are auto-created on first run by their scripts
    # — not pre-created here. This keeps the client page clean.

    all_dbs = base_dbs + service_dbs

    print(f"\nPass 1: Creating {len(all_dbs)} databases...")
    for db_name, schema in all_dbs:
        if not dry_run:
            db_id = await notion.create_database(
                parent_page_id=client_page_id,
                title=db_name,
                properties_schema=schema,
            )
            databases[db_name] = db_id
            print(f"  ✓ {db_name}: {db_id}")
        else:
            print(f"  [DRY RUN] Would create: {db_name}")

    # ── Pass 2: Add relation properties ───────────────────────────────────────
    if not dry_run:
        print("\nPass 2: Adding relation properties...")

        # Client Log → Client Info
        await notion.update_database(
            database_id=databases["Client Log"],
            properties_schema={
                "Client": {
                    "relation": {
                        "database_id": databases["Client Info"],
                        "single_property": {},
                    }
                }
            },
        )
        print("  ✓ Client Log → Client Info relation added")
    else:
        print("\n[DRY RUN] Would add relation properties in Pass 2")

    # ── Create initial Client Info entry ──────────────────────────────────────
    if not dry_run:
        print("\nCreating initial Client Info entry...")
        entry_props = {
            "Name": notion.title_property(client_name),
        }
        if contact_email:
            entry_props["Email"] = {"email": contact_email}
        if verticals:
            entry_props["Vertical"] = notion.text_property(", ".join(verticals))

        entry_id = await notion.create_database_entry(
            database_id=databases["Client Info"],
            properties=entry_props,
        )
        print(f"  ✓ Client Info entry: {entry_id}")

    # ── Create Business Profile page ──────────────────────────────────────────
    business_profile_id = ""
    if not dry_run:
        print("\nCreating Business Profile page...")
        business_profile_id = await notion.create_page(
            parent_page_id=client_page_id,
            title=f"{client_name} — Business Profile",
        )

        profile_blocks = _build_business_profile_blocks(client_name, verticals)
        # Notion API limits to 100 blocks per append — batch if needed
        for i in range(0, len(profile_blocks), 100):
            batch = profile_blocks[i:i+100]
            await notion.append_blocks(business_profile_id, batch)

        section_count = len(UNIVERSAL_SECTIONS)
        for v in verticals:
            section_count += len(VERTICAL_SECTIONS.get(v, []))
        print(f"  ✓ Business Profile: {business_profile_id} ({section_count} sections)")
    else:
        section_count = len(UNIVERSAL_SECTIONS)
        for v in verticals:
            section_count += len(VERTICAL_SECTIONS.get(v, []))
        print(f"\n[DRY RUN] Would create Business Profile page with {section_count} sections:")
        print(f"  Universal: {len(UNIVERSAL_SECTIONS)} sections")
        for v in verticals:
            v_sections = VERTICAL_SECTIONS.get(v, [])
            print(f"  {v}: {len(v_sections)} sections")

    # ── Print summary ─────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("SETUP COMPLETE" if not dry_run else "DRY RUN COMPLETE")
    print("=" * 60)
    if not dry_run:
        print(f"\nClient page ID: {client_page_id}")
        print(f"Business Profile: {business_profile_id}")
        print(f"\nDatabase IDs:")
        for name, db_id in databases.items():
            print(f"  {name}: {db_id}")
        print("\nNext steps:")
        print("  1. Fill in the Business Profile page with client details")
        print("  2. Run: make transcript / make sitemap / make content")

    return {
        "client_page_id": client_page_id if not dry_run else "",
        "business_profile_id": business_profile_id,
        "databases": databases if not dry_run else {},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Set up Notion structure for a new client")
    parser.add_argument("--client-name", default="", help="Client/company name")
    parser.add_argument("--contact-email", default="", help="Client primary email")
    parser.add_argument("--dry-run", action="store_true", help="Preview without creating anything")
    parser.add_argument("--setup-clients-db", action="store_true",
                        help="Create the top-level Clients DB (one-time per workspace)")
    parser.add_argument("--services", nargs="*", default=["website_build", "care_plan"],
                        help="Active services (e.g. website_build care_plan seo blog)")
    parser.add_argument("--verticals", nargs="*", default=[],
                        help="Industry verticals (e.g. speech_pathology occupational_therapy)")
    args = parser.parse_args()

    if args.setup_clients_db:
        asyncio.run(setup_clients_db(dry_run=args.dry_run))
    elif args.client_name:
        asyncio.run(setup_client(
            client_name=args.client_name,
            contact_email=args.contact_email,
            dry_run=args.dry_run,
            services=args.services,
            verticals=args.verticals,
        ))
    else:
        parser.error("Either --client-name or --setup-clients-db is required")


if __name__ == "__main__":
    main()
