"""
OnboardingAgent — Provision a new client from a Notion form submission

Triggered by: a new entry in the Client Onboarding Submissions DB
              with Pipeline Status = "New Submission"

What it does:
  1. Reads all 54 onboarding form fields from Notion
  2. Creates the full 9-database Notion structure for the client
  3. Creates a ClickUp folder + "Website Development" list
  4. Populates Client Info DB with contact + business details from the form
  5. Populates Brand Guidelines DB with colors, fonts, tone from the form
  6. Asks Claude to synthesize the form into a Client Brief document
     and writes it to a Notion page under the client root
  7. Writes the new client entry to config/clients.json
  8. Marks the submission as "Active Client" in the Onboarding Submissions DB
  9. Returns the new client key (slug) for immediate pipeline use

After this runs, the client is fully provisioned and:
    make mood-board CLIENT=<client_key>
    make images-brand CLIENT=<client_key>
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
        Process a single onboarding form submission.

        Required kwargs:
          - submission_page_id (str): Notion page ID of the submission entry
          - onboarding_db_id (str): Notion DB ID of Onboarding Submissions
          - clickup_space_id (str): ClickUp space to create the client folder in
        """
        submission_page_id = kwargs["submission_page_id"]
        onboarding_db_id   = kwargs["onboarding_db_id"]
        clickup_space_id   = kwargs["clickup_space_id"]

        self.log.info(f"OnboardingAgent starting | submission={submission_page_id}")

        # ── Step 1: Read the onboarding form submission ────────────────────────
        submission = await self.notion._client.request(
            path=f"pages/{submission_page_id}", method="GET"
        )
        props = submission.get("properties", {})

        business_name      = _get_title(props.get("Business Name", {}))
        first_name         = _get_rich_text(props.get("First Name", {}))
        last_name          = _get_rich_text(props.get("Last Name", {}))
        contact_name       = f"{first_name} {last_name}".strip() or business_name
        email              = _get_email(props.get("Email", {}))
        phone              = _get_phone(props.get("Phone Number", {}))
        business_type      = _get_select(props.get("Business Type", {}))
        geo_scope          = _get_select(props.get("Geographic Scope", {}))
        services_requested = _get_multi_select(props.get("Services Requested", {}))
        mission            = _get_rich_text(props.get("Mission Statement", {}))
        core_values        = _get_rich_text(props.get("Core Values", {}))
        tagline            = _get_rich_text(props.get("Tagline / Slogan", {}))
        description        = _get_rich_text(props.get("Company Description", {}))
        differentiators    = _get_rich_text(props.get("What Sets You Apart", {}))
        competitors        = _get_rich_text(props.get("Primary Competitors", {}))
        perceived_as       = _get_rich_text(props.get("How You Want to Be Perceived", {}))
        brand_colors       = _get_rich_text(props.get("Brand Colors", {}))
        typography         = _get_rich_text(props.get("Typography / Fonts", {}))
        websites_admire    = _get_rich_text(props.get("Websites You Admire", {}))
        websites_dislike   = _get_rich_text(props.get("Websites You Dislike", {}))
        target_audience    = _get_rich_text(props.get("Target Audience", {}))
        seo_keywords       = _get_rich_text(props.get("SEO Keywords", {}))
        primary_goal       = _get_select(props.get("Primary Goal of Website", {}))
        existing_domain    = _get_url(props.get("Current Domain", {}))
        pages_needed       = _get_rich_text(props.get("Pages You Know You Need", {}))
        faqs               = _get_rich_text(props.get("Frequently Asked Questions", {}))
        business_address   = _get_rich_text(props.get("Business Address", {}))
        primary_location   = _get_rich_text(props.get("Primary Service Location(s)", {}))

        if not business_name:
            raise AgentError("Submission has no Business Name — cannot onboard.")

        client_key = _slug(business_name)
        self.log.info(f"Processing: {business_name} → client_key={client_key}")

        # ── Step 2: Create Notion structure ───────────────────────────────────
        self.log.info("Creating Notion databases...")
        from scripts.setup_notion import setup_client as notion_setup
        setup_result = await notion_setup(
            client_name=business_name,
            contact_email=email or "unknown@example.com",
            dry_run=False,
        )
        client_page_id = setup_result["client_page_id"]
        databases      = setup_result["databases"]
        self.log.info(f"  ✓ Notion structure created | page={client_page_id}")

        # ── Step 3: Create ClickUp folder + list ──────────────────────────────
        self.log.info("Creating ClickUp folder...")
        clickup_folder_id = await self.clickup.create_folder(clickup_space_id, business_name)
        clickup_list_id   = await self.clickup.create_list(clickup_folder_id, "Website Development")
        self.log.info(f"  ✓ ClickUp folder={clickup_folder_id} | list={clickup_list_id}")

        # ── Step 4: Populate Client Info ──────────────────────────────────────
        self.log.info("Populating Client Info DB...")
        client_info_db_id = databases.get("Client Info", "")

        notes_parts = [p for p in [
            f"Business Address: {business_address}" if business_address else "",
            f"Geographic Scope: {geo_scope}" if geo_scope else "",
            f"Primary Location: {primary_location}" if primary_location else "",
            f"Services Requested: {', '.join(services_requested)}" if services_requested else "",
            f"Primary Goal: {primary_goal}" if primary_goal else "",
            f"Existing Website: {existing_domain}" if existing_domain else "",
            f"Pages Needed: {pages_needed}" if pages_needed else "",
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
            self.log.info(f"  ✓ Client Info populated")

        # ── Step 5: Populate Brand Guidelines ────────────────────────────────
        self.log.info("Populating Brand Guidelines DB...")
        brand_db_id = databases.get("Brand Guidelines", "")

        tone_parts = [p for p in [
            f"How we want to be perceived: {perceived_as}" if perceived_as else "",
            f"Core Values: {core_values}" if core_values else "",
            f"Tagline: {tagline}" if tagline else "",
            f"What sets us apart: {differentiators}" if differentiators else "",
        ] if p]

        raw_parts = [p for p in [
            f"MISSION: {mission}" if mission else "",
            f"DESCRIPTION: {description}" if description else "",
            f"DIFFERENTIATORS: {differentiators}" if differentiators else "",
            f"COMPETITORS: {competitors}" if competitors else "",
            f"TARGET AUDIENCE: {target_audience}" if target_audience else "",
            f"FAQs: {faqs}" if faqs else "",
            f"WEBSITES WE ADMIRE: {websites_admire}" if websites_admire else "",
            f"WEBSITES WE DISLIKE: {websites_dislike}" if websites_dislike else "",
            f"SEO KEYWORDS: {seo_keywords}" if seo_keywords else "",
            f"PAGES NEEDED: {pages_needed}" if pages_needed else "",
        ] if p]

        brand_entry_props: dict = {
            "Name":            self.notion.title_property(f"{business_name} Brand Guidelines"),
            "Tone Descriptors": self.notion.text_property("\n".join(tone_parts)[:2000]),
            "Raw Guidelines":  self.notion.text_property("\n\n".join(raw_parts)[:2000]),
        }
        if brand_colors:
            brand_entry_props["Primary Color"] = self.notion.text_property(brand_colors[:200])
        if typography:
            brand_entry_props["Primary Font"] = self.notion.text_property(typography[:200])
        if websites_admire:
            brand_entry_props["Inspiration URLs"] = self.notion.text_property(websites_admire[:500])

        await self.notion.create_database_entry(brand_db_id, brand_entry_props)
        self.log.info("  ✓ Brand Guidelines populated")

        # ── Step 6: Claude writes the Client Brief ────────────────────────────
        self.log.info("Generating client brief with Claude...")
        brief_data = await self._generate_brief(props, business_name)

        brief_page_id = await self.notion.create_page(
            parent_page_id=client_page_id,
            title=f"{business_name} — Client Brief",
        )
        await self.notion.append_blocks(brief_page_id, _brief_blocks(brief_data))
        self.log.info(f"  ✓ Client brief written → {brief_page_id}")

        # ── Step 7: Write to config/clients.json ──────────────────────────────
        self.log.info("Writing client config...")
        new_client_config = {
            "client_id":   client_key,
            "name":        business_name,
            "client_info_db_id":       databases.get("Client Info", ""),
            "meeting_notes_db_id":     databases.get("Meeting Notes & Transcripts", ""),
            "brand_guidelines_db_id":  databases.get("Brand Guidelines", ""),
            "mood_board_db_id":        databases.get("Mood Board", ""),
            "sitemap_db_id":           databases.get("Sitemap", ""),
            "content_db_id":           databases.get("Page Content", ""),
            "wireframes_db_id":        databases.get("Wireframes", ""),
            "hifi_db_id":              databases.get("High-Fidelity Design", ""),
            "action_items_db_id":      databases.get("Action Items", ""),
            "images_db_id":            databases.get("Images", ""),
            "meeting_notes_entry_id":  "",
            "clickup_review_list_id":  clickup_list_id,
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

        # ── Step 8: Mark submission as Active Client ──────────────────────────
        await self.notion.update_database_entry(submission_page_id, {
            "Pipeline Status": self.notion.select_property("Active Client"),
        })
        self.log.info("  ✓ Submission marked as Active Client")

        return {
            "status":        "success",
            "client_key":    client_key,
            "client_name":   business_name,
            "client_page_id": client_page_id,
            "brief_page_id": brief_page_id,
            "clickup_folder_id": clickup_folder_id,
            "clickup_list_id":   clickup_list_id,
            "databases":     databases,
        }

    async def _generate_brief(self, props: dict, business_name: str) -> dict:
        """Ask Claude to synthesize the form into a client brief."""
        # Build a readable summary of all form fields
        field_lines = []
        for field_name, prop in props.items():
            prop_type = next(iter(prop.keys()), "")
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
