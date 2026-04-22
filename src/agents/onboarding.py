"""
OnboardingAgent — Provision a new client from a Notion intake form submission

Triggered by: a new entry in the Client Intake — Submissions DB
              with Pipeline Status = "New Submission"
              and Intake Type = "Website Build Intake" (or "Core Business Intake")

What it does:
  1. Reads the intake form submission from Notion
  2. Creates the new Notion structure (4 base DBs + Business Profile + service DBs)
  3. Populates Client Info DB with contact + business details from the form
  4. Populates Brand Guidelines DB with colors, fonts, tone from the form
  5. Asks Claude to synthesize the form into a Client Brief document
     and writes it to a Notion page under the client root
  6. Writes the new client entry to config/clients.json (with services config)
  7. Marks the submission as "Active Client" in the intake DB
  8. Returns the new client key (slug) for immediate pipeline use

Note: ClickUp folder + Slack channel are created automatically by the GHL
integration when a deal closes. This agent does NOT create ClickUp resources.

After this runs, the client is fully provisioned and:
    make transcript CLIENT=<client_key>
    make sitemap CLIENT=<client_key>
    make content CLIENT=<client_key>
...work immediately.
"""
from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from ..config import settings
from ..models.pipeline import PipelineStage
from .base_agent import AgentError, BaseAgent

logger = logging.getLogger(__name__)

CLIENTS_JSON_PATH = Path(__file__).parent.parent.parent / "config" / "clients.json"

# ── Claude system prompt ───────────────────────────────────────────────────────

BRIEF_SYSTEM_PROMPT = """\
You are a senior account manager at RxMedia, a digital marketing agency
specializing in healthcare and telehealth websites.

Your task: read a client's onboarding form submission and write a concise,
actionable Client Brief for the internal agency team.

The brief will be read before the kickoff meeting. It should give the team a
clear picture of:
- Who the client is and what they do
- Their target audience and what matters to them
- Their brand personality and visual direction
- Key goals for the website
- Anything unusual, specific, or important to know

Format as clean prose with clear sections. Be specific — reference actual
details from the form, not generic observations. Flag any gaps or things to
clarify in the kickoff meeting.

Return ONLY this JSON:
{
  "brief": {
    "executive_summary": "2-3 sentences. Who they are, what they do, why they hired us.",
    "business_overview": "3-5 sentences. Services, location, model (virtual/in-person/both), scale.",
    "target_audience": "2-3 sentences. Who their patients/customers are, what drives them.",
    "brand_personality": "2-3 sentences. Tone, visual direction, how they want to be perceived.",
    "website_goals": "Bullet list as a single string, newline-separated. Primary goal, secondary goals, CTAs.",
    "key_decisions_needed": "Bullet list of questions/gaps to resolve in the kickoff meeting.",
    "competitive_context": "1-2 sentences. Who their competitors are and what gap they fill.",
    "notes_for_team": "Anything else the team should know before the kickoff."
  }
}
"""


# ── Submission merge helpers ───────────────────────────────────────────────────

def _is_prop_empty(prop: dict) -> bool:
    """Return True if a Notion property value has no meaningful content."""
    if not prop:
        return True
    prop_type = prop.get("type", "")
    if prop_type == "rich_text":
        return not any(p.get("text", {}).get("content", "") for p in prop.get("rich_text", []))
    if prop_type == "title":
        return not any(p.get("text", {}).get("content", "") for p in prop.get("title", []))
    if prop_type == "select":
        return prop.get("select") is None
    if prop_type == "multi_select":
        return len(prop.get("multi_select", [])) == 0
    if prop_type in ("email", "phone_number", "url"):
        return not prop.get(prop_type)
    if prop_type == "number":
        return prop.get("number") is None
    return True


def _merge_submission_props(submissions: list[dict]) -> dict:
    """
    Merge Notion property dicts from multiple submissions.
    First non-empty value wins — Core Business Intake should be first in the list.
    """
    merged: dict = {}
    for sub in submissions:
        for key, val in sub.get("properties", {}).items():
            if key not in merged or _is_prop_empty(merged[key]):
                merged[key] = val
    return merged


# ── Notion helpers ─────────────────────────────────────────────────────────────

def _get_rich_text(prop: dict) -> str:
    return "".join(
        p.get("text", {}).get("content", "") for p in prop.get("rich_text", [])
    )


def _get_select(prop: dict) -> str:
    sel = prop.get("select")
    return sel.get("name", "") if sel else ""


def _get_multi_select(prop: dict) -> list[str]:
    return [opt.get("name", "") for opt in prop.get("multi_select", [])]


def _get_title(prop: dict) -> str:
    return "".join(
        t.get("text", {}).get("content", "") for t in prop.get("title", [])
    )


def _get_email(prop: dict) -> str:
    return prop.get("email", "") or ""


def _get_phone(prop: dict) -> str:
    return prop.get("phone_number", "") or ""


def _get_url(prop: dict) -> str:
    return prop.get("url", "") or ""


def _slug(name: str) -> str:
    """Convert a client name to a safe Python identifier."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower().strip())
    return slug.strip("_")


# ── OnboardingAgent ────────────────────────────────────────────────────────────

class OnboardingAgent(BaseAgent):
    """
    Provisions a new client from a Notion onboarding form submission.
    """

    name = "onboarding"
    tools: list[dict] = []

    async def run(self, client_id: str, **kwargs: Any) -> dict:
        """
        Process one or more intake form submissions for the same client.

        Required kwargs:
          - submission_page_ids (list[str]): Notion page IDs of all submissions for
            this client. Core Business Intake should be first — its fields take
            priority when merging. All IDs are marked "Active Client" when done.
          - intake_db_id (str): Notion DB ID of the Client Intake — Submissions DB
        """
        submission_page_ids = kwargs["submission_page_ids"]
        intake_db_id        = kwargs["intake_db_id"]
        primary_id          = submission_page_ids[0]

        self.log.info(
            f"OnboardingAgent starting | submissions={len(submission_page_ids)} | primary={primary_id}"
        )

        # ── Step 1: Read and merge all submissions ─────────────────────────────
        all_submissions = []
        for page_id in submission_page_ids:
            sub = await self.notion._client.request(path=f"pages/{page_id}", method="GET")
            all_submissions.append(sub)
            self.log.info(f"  Loaded submission: {page_id}")

        props = _merge_submission_props(all_submissions)
        self.log.info(f"  Merged {len(all_submissions)} submission(s) into unified props")

        # Business Name is rich_text in the new DB; Submission is the title field
        business_name      = _get_rich_text(props.get("Business Name", {})) or \
                             _get_title(props.get("Submission", {}))
        first_name         = _get_rich_text(props.get("First Name", {}))
        last_name          = _get_rich_text(props.get("Last Name", {}))
        contact_name       = f"{first_name} {last_name}".strip() or business_name
        email              = _get_email(props.get("Email", {}))
        phone              = _get_phone(props.get("Phone", {}))
        practice_type      = _get_multi_select(props.get("Practice Type", {}))
        geo_scope          = _get_select(props.get("What Best Describes Your Service Area", {}))
        services_interested = _get_multi_select(props.get("Services Interested", {}))
        description        = _get_rich_text(props.get("Practice Description", {}))
        mission            = _get_rich_text(props.get("Mission Statement", {}))
        core_values        = _get_rich_text(props.get("Core Values", {}))
        tagline            = _get_rich_text(props.get("Tagline / Slogan", {}))
        differentiators    = _get_rich_text(props.get("Differentiators", {}))
        competitors        = _get_rich_text(props.get("Competitors", {}))
        brand_colors       = _get_rich_text(props.get("Brand Colors", {}))
        brand_fonts        = _get_rich_text(props.get("Brand Fonts", {}))
        brand_elements     = _get_rich_text(props.get("Brand Elements", {}))
        websites_admire    = _get_rich_text(props.get("Websites You Admire", {}))
        websites_dislike   = _get_rich_text(props.get("Websites You Dislike", {}))
        target_audience    = _get_rich_text(props.get("Ideal Patient/Client", {}))
        seo_keywords       = _get_rich_text(props.get("SEO Keywords", {}))
        primary_goals      = _get_multi_select(props.get("Primary Goals", {}))
        existing_domain    = _get_url(props.get("Current Website URL", {})) or \
                             _get_rich_text(props.get("Current Website URL", {}))
        pages_needed       = _get_rich_text(props.get("Specific Pages Needed", {}))
        required_pages     = _get_multi_select(props.get("Required Pages", {}))
        priority_services  = _get_rich_text(props.get("Priority Services (top 3–5)", {}))
        location_addresses = _get_rich_text(props.get("Location Addresses", {}))
        primary_location   = _get_rich_text(props.get("Primary Service Locations", {}))
        website_type       = _get_select(props.get("Website Project Type", {}))
        booking_platform   = _get_rich_text(props.get("Booking Platform (if any)", {}))
        medical_reviewer   = _get_rich_text(props.get("Medical Reviewer Name", {}))
        intake_type        = _get_select(props.get("Intake Type", {}))

        if not business_name:
            raise AgentError("Submission has no Business Name — cannot onboard.")

        client_key = _slug(business_name)
        self.log.info(f"Processing: {business_name} ({intake_type}) → client_key={client_key}")

        # ── Step 2: Create Notion structure ───────────────────────────────────
        self.log.info("Creating Notion databases...")

        # Determine services from form data
        active_services = ["website_build", "care_plan"]  # defaults
        if services_interested:
            si_lower = [s.lower() for s in services_interested]
            if any("seo" in s for s in si_lower):
                active_services.append("seo")
            if any("social" in s for s in si_lower):
                active_services.append("social_media")
            if any("content" in s or "blog" in s for s in si_lower):
                active_services.append("blog")

        # Determine verticals from practice type — substring match so
        # "Addiction Treatment & Recovery" → addiction_treatment,
        # "Mental Health & Therapy" → mental_health, etc.
        vertical_keywords = [
            ("speech-language pathology", "speech_pathology"),
            ("speech pathology",          "speech_pathology"),
            ("speech",                    "speech_pathology"),
            ("occupational therapy",      "occupational_therapy"),
            ("physical therapy",          "physical_therapy"),
            ("addiction",                 "addiction_treatment"),
            ("substance use",             "addiction_treatment"),
            ("recovery",                  "addiction_treatment"),
            ("mental health",             "mental_health"),
            ("behavioral health",         "mental_health"),
            ("therapy",                   "mental_health"),  # fallback — Mental Health & Therapy
            ("dermatology",               "dermatology"),
            ("aesthetics",                "dermatology"),
        ]
        active_verticals = []
        for pt in practice_type:
            pt_lower = pt.lower().strip()
            for keyword, vertical in vertical_keywords:
                if keyword in pt_lower and vertical not in active_verticals:
                    active_verticals.append(vertical)
                    break  # one vertical per practice type

        from scripts.onboarding.setup_notion import setup_client as notion_setup
        setup_result = await notion_setup(
            client_name=business_name,
            contact_email=email or "unknown@example.com",
            dry_run=False,
            services=active_services,
            verticals=active_verticals,
        )
        client_page_id = setup_result["client_page_id"]
        databases      = setup_result["databases"]
        self.log.info(f"  ✓ Notion structure created | page={client_page_id}")

        # ── Step 3: Populate Client Info ──────────────────────────────────────
        self.log.info("Populating Client Info DB...")
        client_info_db_id = databases.get("Client Info", "")

        notes_parts = [p for p in [
            f"Location Addresses:\n{location_addresses}" if location_addresses else "",
            f"Service Area: {geo_scope}" if geo_scope else "",
            f"Primary Location: {primary_location}" if primary_location else "",
            f"Services Interested: {', '.join(services_interested)}" if services_interested else "",
            f"Practice Type: {', '.join(practice_type)}" if practice_type else "",
            f"Primary Goals: {', '.join(primary_goals)}" if primary_goals else "",
            f"Website Project Type: {website_type}" if website_type else "",
            f"Existing Website: {existing_domain}" if existing_domain else "",
            f"Pages Needed: {pages_needed}" if pages_needed else "",
            f"Required Pages: {', '.join(required_pages)}" if required_pages else "",
            f"Booking Platform: {booking_platform}" if booking_platform else "",
            f"Medical Reviewer: {medical_reviewer}" if medical_reviewer else "",
        ] if p]

        client_info_entries = await self.notion.query_database(client_info_db_id)
        if client_info_entries:
            entry_id = client_info_entries[0]["id"]
            update_props: dict = {
                "Company":        self.notion.text_property(business_name),
                "Email":          {"email": email} if email else {},
                "Pipeline Stage": self.notion.select_property(PipelineStage.ONBOARDING_COMPLETE.value),
                "Stage Status":   self.notion.select_property("In Progress"),
                "Notes":          self.notion.text_property("\n".join(notes_parts)[:2000]),
            }
            if phone:
                update_props["Phone"] = {"phone_number": phone}
            if existing_domain:
                update_props["Website"] = {"url": existing_domain}
            await self.notion.update_database_entry(entry_id, update_props)
            self.log.info("  ✓ Client Info populated")

        # ── Step 4: Populate Brand Guidelines ────────────────────────────────
        self.log.info("Populating Brand Guidelines DB...")
        brand_db_id = databases.get("Brand Guidelines", "")

        tone_parts = [p for p in [
            f"Mission: {mission}" if mission else "",
            f"Core Values: {core_values}" if core_values else "",
            f"Tagline: {tagline}" if tagline else "",
            f"What sets us apart: {differentiators}" if differentiators else "",
            f"Brand elements: {brand_elements}" if brand_elements else "",
        ] if p]

        raw_parts = [p for p in [
            f"DESCRIPTION: {description}" if description else "",
            f"MISSION: {mission}" if mission else "",
            f"CORE VALUES: {core_values}" if core_values else "",
            f"TAGLINE: {tagline}" if tagline else "",
            f"DIFFERENTIATORS: {differentiators}" if differentiators else "",
            f"PRIORITY SERVICES: {priority_services}" if priority_services else "",
            f"COMPETITORS: {competitors}" if competitors else "",
            f"TARGET AUDIENCE: {target_audience}" if target_audience else "",
            f"WEBSITES WE ADMIRE: {websites_admire}" if websites_admire else "",
            f"WEBSITES WE DISLIKE: {websites_dislike}" if websites_dislike else "",
            f"SEO KEYWORDS: {seo_keywords}" if seo_keywords else "",
            f"PAGES NEEDED: {pages_needed}" if pages_needed else "",
            f"REQUIRED PAGES: {', '.join(required_pages)}" if required_pages else "",
        ] if p]

        brand_entry_props: dict = {
            "Name":             self.notion.title_property(f"{business_name} Brand Guidelines"),
            "Tone Descriptors": self.notion.text_property("\n".join(tone_parts)[:2000]),
            "Raw Guidelines":   self.notion.text_property("\n\n".join(raw_parts)[:2000]),
        }
        if brand_colors:
            brand_entry_props["Primary Color"] = self.notion.text_property(brand_colors[:200])
        if brand_fonts:
            brand_entry_props["Primary Font"] = self.notion.text_property(brand_fonts[:200])
        if websites_admire:
            brand_entry_props["Inspiration URLs"] = self.notion.text_property(websites_admire[:500])

        await self.notion.create_database_entry(brand_db_id, brand_entry_props)
        self.log.info("  ✓ Brand Guidelines populated")

        # ── Step 5: Claude writes the Client Brief ────────────────────────────
        self.log.info("Generating client brief with Claude...")
        brief_data = await self._generate_brief(props, business_name)

        brief_page_id = await self.notion.create_page(
            parent_page_id=client_page_id,
            title=f"{business_name} — Client Brief",
        )
        await self.notion.append_blocks(brief_page_id, _brief_blocks(brief_data))
        self.log.info(f"  ✓ Client brief written → {brief_page_id}")

        # ── Step 6: Write to config/clients.json ──────────────────────────────
        self.log.info("Writing client config...")

        # Build services config block
        services_config = {
            "website_build":          "website_build" in active_services,
            "care_plan":              True,  # always on
            "seo":                    "seo" in active_services,
            "gbp_management":         False,
            "gbp_posts_per_month":    8,
            "blog":                   "blog" in active_services,
            "blog_posts_per_month":   0,
            "social_media":           "social_media" in active_services,
            "social_posts_per_month": 8,
            "linkedin_posts_per_month": 2,
            "newsletter":             False,
            "paid_ads":               False,
        }

        new_client_config = {
            "client_id":   client_key,
            "name":        business_name,
            "email":       email,
            "primary_contact_email": email,
            "primary_contact":       contact_name,
            "phone":       phone,
            "services":    services_config,
            "vertical":    active_verticals,
            "intake_submission_ids":     submission_page_ids,
            # ── Base DBs (always created) ────────────────────────────────────
            "client_info_db_id":         databases.get("Client Info", ""),
            "client_log_db_id":          databases.get("Client Log", ""),
            "meeting_prep_db_id":        databases.get("Meeting Prep", ""),
            "brand_guidelines_db_id":    databases.get("Brand Guidelines", ""),
            "care_plan_db_id":           databases.get("Care Plan", ""),
            "business_profile_page_id":  setup_result.get("business_profile_id", ""),
            # ── Service-specific DBs (created if service active) ─────────────
            "sitemap_db_id":             databases.get("Sitemap", ""),
            "content_db_id":             databases.get("Page Content", ""),
            "images_db_id":              databases.get("Images", ""),
            "competitors_db_id":         databases.get("Competitors", ""),
            "keywords_db_id":            databases.get("Keywords", ""),
            # ── Auto-created on first run ────────────────────────────────────
            "seo_metrics_db_id":         "",
            "gbp_posts_db_id":           "",
            "blog_posts_db_id":          "",
            "social_posts_db_id":        "",
            # ── External IDs ─────────────────────────────────────────────────
            "gbp_location_id":           "",
            "clickup_review_list_id":    "",
            "meeting_notes_entry_id":    "",  # legacy — kept for backward compat
        }
        existing: dict = {}
        if CLIENTS_JSON_PATH.exists():
            try:
                existing = json.loads(CLIENTS_JSON_PATH.read_text()) or {}
            except json.JSONDecodeError:
                existing = {}
        existing[client_key] = new_client_config
        CLIENTS_JSON_PATH.write_text(json.dumps(existing, indent=2))
        self.log.info(f"  ✓ config/clients.json updated")

        # ── Step 7: Add row to top-level Clients DB (agency roster) ───────────
        if settings.notion_clients_db_id:
            try:
                service_display = {
                    "website_build":  "Website Build",
                    "care_plan":      "Care Plan",
                    "seo":            "SEO",
                    "gbp_management": "GBP Management",
                    "blog":           "Blog",
                    "social_media":   "Social Media",
                    "newsletter":     "Newsletter",
                    "paid_ads":       "Paid Ads",
                }
                services_multi = [
                    {"name": service_display[k]}
                    for k, v in services_config.items()
                    if k in service_display and v is True
                ]
                from datetime import date as _date
                roster_props: dict = {
                    "Client Name":    self.notion.title_property(business_name),
                    "Status":         self.notion.select_property("Onboarding"),
                    "Services":       {"multi_select": services_multi},
                    "Vertical":       self.notion.text_property(", ".join(active_verticals)),
                    "Start Date":     {"date": {"start": _date.today().isoformat()}},
                    "Client Page":    {"url": f"https://notion.so/{client_page_id.replace('-', '')}"},
                    "Pipeline Stage": self.notion.text_property(PipelineStage.ONBOARDING_COMPLETE.value),
                }
                if contact_name:
                    roster_props["Primary Contact"] = self.notion.text_property(contact_name)
                if email:
                    roster_props["Contact Email"] = {"email": email}
                await self.notion.create_database_entry(
                    settings.notion_clients_db_id, roster_props,
                )
                self.log.info("  ✓ Agency roster row added to top-level Clients DB")
            except Exception as e:
                self.log.warning(f"  ⚠ Could not add roster row: {e}")
        else:
            self.log.info("  (NOTION_CLIENTS_DB_ID not set — skipping roster row)")

        # ── Step 8: Mark all submissions as Active Client ─────────────────────
        for page_id in submission_page_ids:
            await self.notion.update_database_entry(page_id, {
                "Pipeline Status": self.notion.select_property("Active Client"),
            })
        self.log.info(f"  ✓ {len(submission_page_ids)} submission(s) marked as Active Client")

        return {
            "status":         "success",
            "client_key":     client_key,
            "client_name":    business_name,
            "client_page_id": client_page_id,
            "brief_page_id":  brief_page_id,
            "databases":      databases,
        }

    async def _generate_brief(self, props: dict, business_name: str) -> dict:
        """Ask Claude to synthesize the form into a client brief."""
        # Build a readable summary of all form fields
        field_lines = []
        for field_name, prop in props.items():
            prop_type = prop.get("type", "")
            if prop_type == "title":
                val = _get_title(prop)
            elif prop_type == "rich_text":
                val = _get_rich_text(prop)
            elif prop_type == "select":
                val = _get_select(prop)
            elif prop_type == "multi_select":
                val = ", ".join(_get_multi_select(prop))
            elif prop_type == "email":
                val = _get_email(prop)
            elif prop_type == "phone_number":
                val = _get_phone(prop)
            elif prop_type == "url":
                val = _get_url(prop)
            else:
                continue
            if val:
                field_lines.append(f"{field_name}: {val}")

        form_text = "\n".join(field_lines)

        response = await self.anthropic.messages.create(
            model=self.model,
            max_tokens=2048,
            system=BRIEF_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"CLIENT: {business_name}\n\nONBOARDING FORM:\n{form_text[:6000]}"}],
        )
        raw = response.content[0].text if response.content else "{}"
        try:
            clean = re.sub(r"```(?:json)?\n?", "", raw).strip()
            data = json.loads(clean)
            return data.get("brief", {})
        except json.JSONDecodeError:
            self.log.warning("Brief JSON parse failed — returning raw text")
            return {"executive_summary": raw[:500]}

    async def _execute_tool(self, tool_name: str, tool_input: dict) -> str:
        raise NotImplementedError(f"OnboardingAgent uses direct API calls. Tool {tool_name} not dispatched.")


# ── Notion block builders ──────────────────────────────────────────────────────

def _brief_blocks(brief: dict) -> list[dict]:
    def h(text: str, level: int = 2) -> dict:
        ht = f"heading_{level}"
        return {"object": "block", "type": ht, ht: {
            "rich_text": [{"type": "text", "text": {"content": text}}]
        }}

    def p(text: str) -> dict:
        return {"object": "block", "type": "paragraph", "paragraph": {
            "rich_text": [{"type": "text", "text": {"content": text[:1900]}}]
        }}

    def bullet(text: str) -> dict:
        return {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {
            "rich_text": [{"type": "text", "text": {"content": text[:1900]}}]
        }}

    blocks = []

    if brief.get("executive_summary"):
        blocks += [h("Executive Summary", 2), p(brief["executive_summary"])]

    if brief.get("business_overview"):
        blocks += [h("Business Overview", 2), p(brief["business_overview"])]

    if brief.get("target_audience"):
        blocks += [h("Target Audience", 2), p(brief["target_audience"])]

    if brief.get("brand_personality"):
        blocks += [h("Brand Personality", 2), p(brief["brand_personality"])]

    if brief.get("website_goals"):
        goal_lines = [l.strip("- •").strip() for l in brief["website_goals"].split("\n") if l.strip()]
        blocks += [h("Website Goals", 2)] + [bullet(l) for l in goal_lines if l]

    if brief.get("competitive_context"):
        blocks += [h("Competitive Context", 2), p(brief["competitive_context"])]

    if brief.get("key_decisions_needed"):
        decision_lines = [l.strip("- •").strip() for l in brief["key_decisions_needed"].split("\n") if l.strip()]
        blocks += [h("Questions to Resolve in Kickoff", 2)] + [bullet(l) for l in decision_lines if l]

    if brief.get("notes_for_team"):
        blocks += [h("Notes for the Team", 2), p(brief["notes_for_team"])]

    return blocks
