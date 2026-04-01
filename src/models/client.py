from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from .pipeline import PipelineState


class ContactInfo(BaseModel):
    name: str
    email: str
    phone: str | None = None
    company_name: str
    website_url: str | None = None


class OnboardingData(BaseModel):
    """Structured data collected from the client onboarding form."""
    business_description: str
    target_audience: str
    project_goals: list[str]

    # Design preferences
    design_style_preferences: str | None = None    # e.g. "modern, clean, minimal"
    website_inspiration_urls: list[str] = []        # Sites they like
    colors_they_like: list[str] = []
    colors_they_dislike: list[str] = []

    # Business details
    services: list[str] = []                        # Main services/products offered
    target_keywords: list[str] = []                 # SEO keywords to target
    business_type: str | None = None                # e.g. "local", "national", "e-commerce"
    competitors: list[str] = []

    # Project scope
    budget_range: str | None = None
    timeline_weeks: int | None = None
    has_existing_content: bool = False              # Does client have copy ready?
    has_existing_branding: bool = False             # Logo, brand guidelines?

    raw_form_data: dict = {}                        # Original form fields, preserved


class MeetingNotes(BaseModel):
    """Structured data extracted from a client meeting transcript."""
    meeting_date: datetime | None = None
    meeting_type: str = "kickoff"                   # "kickoff" | "review" | "check-in"

    key_decisions: list[str] = []
    design_preferences: list[str] = []
    client_likes: list[str] = []
    client_dislikes: list[str] = []
    action_items: list[str] = []                    # Will also be created as ClickUp tasks
    open_questions: list[str] = []

    raw_transcript: str | None = None               # Full transcript text
    notion_page_id: str | None = None               # ID of the Meeting Notes DB entry


class Client(BaseModel):
    id: str                                         # UUID
    contact: ContactInfo
    onboarding: OnboardingData | None = None
    meetings: list[MeetingNotes] = []
    pipeline_state: PipelineState

    # External IDs
    notion_client_page_id: str | None = None        # Root Notion page
    clickup_folder_id: str | None = None            # ClickUp folder for this client

    # Webflow build preferences
    use_webflow_template: bool = False              # True = use a Webflow template as starting point
    webflow_template_url: str | None = None

    created_at: datetime
    updated_at: datetime
