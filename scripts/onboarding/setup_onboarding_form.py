#!/usr/bin/env python3
"""
setup_onboarding_form.py — Create the Client Onboarding Submissions database in Notion

This creates a single agency-level database under the workspace root page.
After running, open Notion and enable a "Form" view on the database —
that gives you a shareable link to send to new clients.

When a client submits the form, their entry lands in this database.
An onboarding agent (future) reads it and bootstraps the full client pipeline.

Usage:
    python scripts/setup_onboarding_form.py
    python scripts/setup_onboarding_form.py --dry-run
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings
from src.integrations.notion import NotionClient


def onboarding_schema() -> dict:
    return {
        # ── Title (required by Notion) ────────────────────────────────────────
        "Business Name": {"title": {}},

        # ── Contact Info ──────────────────────────────────────────────────────
        "First Name": {"rich_text": {}},
        "Last Name": {"rich_text": {}},
        "Email": {"email": {}},
        "Phone Number": {"phone_number": {}},
        "Business Address": {"rich_text": {}},

        # ── Business Overview ─────────────────────────────────────────────────
        "Business Type": {
            "select": {
                "options": [
                    {"name": "Addiction Treatment & Recovery", "color": "blue"},
                    {"name": "Behavioral Health", "color": "green"},
                    {"name": "Mental Health & Therapy", "color": "purple"},
                    {"name": "Dermatology & Aesthetics", "color": "pink"},
                    {"name": "Speech-Language Pathology", "color": "yellow"},
                    {"name": "Occupational Therapy", "color": "orange"},
                    {"name": "Physical Therapy & Rehabilitation", "color": "red"},
                    {"name": "Other Medical / Healthcare", "color": "gray"},
                ]
            }
        },
        "Geographic Scope": {
            "select": {
                "options": [
                    {"name": "Local", "color": "blue"},
                    {"name": "Regional", "color": "green"},
                    {"name": "National", "color": "yellow"},
                    {"name": "Virtual / Nationwide", "color": "purple"},
                ]
            }
        },
        "Primary Service Location(s)": {"rich_text": {}},
        "Services Requested": {
            "multi_select": {
                "options": [
                    {"name": "New Website", "color": "blue"},
                    {"name": "Website Redesign", "color": "green"},
                    {"name": "SEO", "color": "yellow"},
                    {"name": "Content Writing", "color": "orange"},
                    {"name": "Branding", "color": "purple"},
                    {"name": "Logo Design", "color": "pink"},
                    {"name": "Social Media", "color": "red"},
                    {"name": "Ongoing Maintenance", "color": "gray"},
                ]
            }
        },
        "Mission Statement": {"rich_text": {}},
        "Core Values": {"rich_text": {}},
        "Tagline / Slogan": {"rich_text": {}},
        "Company Description": {"rich_text": {}},
        "Products / Services Offered": {"rich_text": {}},
        "What Sets You Apart": {"rich_text": {}},
        "Primary Competitors": {"rich_text": {}},

        # ── Brand & Design ────────────────────────────────────────────────────
        "How You Want to Be Perceived": {"rich_text": {}},
        "Words That Should NEVER Describe Your Brand": {"rich_text": {}},
        "Common Industry Misconceptions": {"rich_text": {}},
        "Do You Have a Branding Guide?": {
            "select": {
                "options": [
                    {"name": "Yes", "color": "green"},
                    {"name": "No", "color": "red"},
                ]
            }
        },
        "Branding Guide Link": {"url": {}},
        "Brand Colors": {"rich_text": {}},
        "Typography / Fonts": {"rich_text": {}},
        "Design Elements / Patterns": {"rich_text": {}},
        "Websites You Admire": {"rich_text": {}},
        "Websites You Dislike": {"rich_text": {}},

        # ── SEO & Content ─────────────────────────────────────────────────────
        "Target Audience": {"rich_text": {}},
        "SEO Keywords": {"rich_text": {}},
        "Currently Ranking for Keywords?": {
            "select": {
                "options": [
                    {"name": "Yes", "color": "green"},
                    {"name": "No", "color": "red"},
                    {"name": "Not Sure", "color": "gray"},
                ]
            }
        },
        "Current Keywords": {"rich_text": {}},
        "Frequently Asked Questions": {"rich_text": {}},

        # ── Website Details ───────────────────────────────────────────────────
        "Role of Website": {
            "multi_select": {
                "options": [
                    {"name": "Lead Generation", "color": "blue"},
                    {"name": "Online Booking", "color": "green"},
                    {"name": "Patient / Client Education", "color": "yellow"},
                    {"name": "Brand Credibility", "color": "purple"},
                    {"name": "E-Commerce", "color": "orange"},
                    {"name": "Patient Portal", "color": "pink"},
                    {"name": "Brand Awareness", "color": "red"},
                ]
            }
        },
        "Primary Goal of Website": {
            "select": {
                "options": [
                    {"name": "Book More Appointments", "color": "green"},
                    {"name": "Generate Leads", "color": "blue"},
                    {"name": "Establish Credibility", "color": "purple"},
                    {"name": "Educate Patients", "color": "yellow"},
                    {"name": "Sell Products / Services", "color": "orange"},
                    {"name": "Expand to New Markets", "color": "red"},
                ]
            }
        },
        "Most Important Visitor Action": {
            "multi_select": {
                "options": [
                    {"name": "Book an Appointment", "color": "green"},
                    {"name": "Call Us", "color": "blue"},
                    {"name": "Fill Out a Contact Form", "color": "yellow"},
                    {"name": "Subscribe to Newsletter", "color": "purple"},
                    {"name": "Learn About Services", "color": "orange"},
                    {"name": "Purchase / Order", "color": "red"},
                ]
            }
        },
        "Do You Have an Existing Website?": {
            "select": {
                "options": [
                    {"name": "Yes", "color": "green"},
                    {"name": "No", "color": "red"},
                ]
            }
        },
        "Current Domain": {"url": {}},
        "Desired Domain": {"rich_text": {}},
        "Pages You Know You Need": {"rich_text": {}},
        "Do You Have Web Content Ready?": {
            "select": {
                "options": [
                    {"name": "Yes", "color": "green"},
                    {"name": "Partially", "color": "yellow"},
                    {"name": "No", "color": "red"},
                ]
            }
        },
        "Content Link": {"url": {}},
        "Photos Available": {
            "multi_select": {
                "options": [
                    {"name": "Custom Photography", "color": "green"},
                    {"name": "Stock Photos", "color": "blue"},
                    {"name": "Client-Provided Assets", "color": "yellow"},
                    {"name": "Mix of Stock & Custom", "color": "orange"},
                    {"name": "No Photos Yet", "color": "gray"},
                ]
            }
        },
        "Required Pages / Content": {
            "multi_select": {
                "options": [
                    {"name": "Blog", "color": "blue"},
                    {"name": "FAQ", "color": "green"},
                    {"name": "Testimonials / Reviews", "color": "yellow"},
                    {"name": "Team / Staff", "color": "purple"},
                    {"name": "Locations", "color": "orange"},
                    {"name": "Patient Portal", "color": "pink"},
                    {"name": "Privacy Policy", "color": "gray"},
                    {"name": "Terms & Conditions", "color": "gray"},
                    {"name": "Medical Disclaimer", "color": "red"},
                    {"name": "Telehealth Consent", "color": "red"},
                ]
            }
        },

        # ── Technical ─────────────────────────────────────────────────────────
        "Form Submission Email(s)": {"email": {}},
        "Phone Number for Routing": {"phone_number": {}},
        "Social Media Profiles": {
            "multi_select": {
                "options": [
                    {"name": "Instagram", "color": "pink"},
                    {"name": "Facebook", "color": "blue"},
                    {"name": "LinkedIn", "color": "blue"},
                    {"name": "TikTok", "color": "gray"},
                    {"name": "YouTube", "color": "red"},
                    {"name": "Twitter / X", "color": "gray"},
                    {"name": "Pinterest", "color": "red"},
                ]
            }
        },
        "Social Media URLs": {"rich_text": {}},
        "Integrations / Services to Connect": {
            "multi_select": {
                "options": [
                    {"name": "Google Analytics", "color": "blue"},
                    {"name": "Google Search Console", "color": "green"},
                    {"name": "EMR / EHR System", "color": "purple"},
                    {"name": "Online Booking System", "color": "yellow"},
                    {"name": "Live Chat", "color": "orange"},
                    {"name": "Email Marketing", "color": "pink"},
                    {"name": "Patient Portal", "color": "red"},
                    {"name": "E-Commerce", "color": "gray"},
                ]
            }
        },
        "Has Assets (Images / Videos)?": {
            "select": {
                "options": [
                    {"name": "Yes — sending to keegan@rxmedia.io", "color": "green"},
                    {"name": "No", "color": "red"},
                ]
            }
        },

        # ── Approval Preference ───────────────────────────────────────────────
        "Approval Preference": {
            "select": {
                "options": [
                    {"name": "Review each stage (mood board + sitemap + content)", "color": "green"},
                    {"name": "Review final deliverable only", "color": "yellow"},
                ]
            }
        },

        # ── Agency internal ───────────────────────────────────────────────────
        "Submission Date": {"date": {}},
        "Pipeline Status": {
            "select": {
                "options": [
                    {"name": "New Submission", "color": "blue"},
                    {"name": "Onboarding In Progress", "color": "yellow"},
                    {"name": "Active Client", "color": "green"},
                    {"name": "On Hold", "color": "gray"},
                ]
            }
        },
        "Assigned To": {"rich_text": {}},
        "Internal Notes": {"rich_text": {}},
    }


async def main(dry_run: bool = False) -> None:
    print(f"\n{'[DRY RUN] ' if dry_run else ''}Setting up Client Onboarding Form database...")
    print("=" * 60)

    notion = NotionClient(settings.notion_api_key)

    if dry_run:
        print("\n[DRY RUN] Would create 'Client Onboarding Submissions' database")
        print(f"  Parent: {settings.notion_workspace_root_page_id}")
        print(f"  Properties: {len(onboarding_schema())} fields")
        print("\n[DRY RUN] Complete — no changes made.")
        return

    print(f"\nCreating database under workspace root ({settings.notion_workspace_root_page_id})...")

    db_id = await notion.create_database(
        parent_page_id=settings.notion_workspace_root_page_id,
        title="Client Onboarding Submissions",
        properties_schema=onboarding_schema(),
    )

    print(f"\n✓ Database created: {db_id}")
    print("\n" + "=" * 60)
    print("NEXT STEPS")
    print("=" * 60)
    print(f"""
1. Open Notion and find "Client Onboarding Submissions"
2. Click "+ Add a view" → select "Form"
3. In the Form view, click "Share form" to get the shareable link
4. Send that link to new clients

When a client submits:
  - Their entry appears in this database with Pipeline Status = "New Submission"
  - You (or the onboarding agent) reads the entry and runs:
    python scripts/setup_notion.py --client-name "Business Name" --contact-email "email"

Database ID (save this):
  ONBOARDING_DB_ID={db_id}
""")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Create the Client Onboarding Submissions database in Notion"
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without creating anything")
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run))
