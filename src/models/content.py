from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVISION_REQUESTED = "revision_requested"


# ── Mood Board ────────────────────────────────────────────────────────────────

class MoodBoardVariation(BaseModel):
    """One of the 4–6 mood board variations presented to the client."""
    id: str
    title: str                          # e.g. "Option A — Bold & Modern"
    description: str                    # What this variation communicates
    color_palette_description: str      # Colors used in this variation
    style_keywords: list[str]           # e.g. ["minimalist", "clean", "tech-forward"]
    visual_references: list[str]        # URLs to reference images or inspiration sites
    rationale: str                      # Why this option fits the client
    client_feedback: str | None = None
    status: ApprovalStatus = ApprovalStatus.PENDING


class MoodBoard(BaseModel):
    client_id: str
    notion_page_id: str | None = None
    variations: list[MoodBoardVariation] = []
    approved_variation_id: str | None = None
    generated_at: datetime | None = None


# ── Sitemap ───────────────────────────────────────────────────────────────────

class PageType(str, Enum):
    STATIC = "static"       # Hard-coded content in Webflow designer
    CMS = "cms"             # Content managed via Webflow CMS collections


class ContentMode(str, Enum):
    AI_GENERATED = "ai_generated"       # Claude writes copy from onboarding data
    CLIENT_PROVIDED = "client_provided" # Client supplies their own copy


class SitemapPage(BaseModel):
    id: str
    title: str                          # e.g. "Home", "About Us", "Services"
    slug: str                           # URL slug, e.g. "/", "/about", "/services"
    parent_id: str | None = None        # None = top-level page
    page_type: PageType = PageType.STATIC
    content_mode: ContentMode = ContentMode.AI_GENERATED
    purpose: str                        # What this page is meant to accomplish
    key_sections: list[str] = []        # High-level content blocks on this page
    order: int = 0                      # Display order among siblings


class Sitemap(BaseModel):
    client_id: str
    notion_page_id: str | None = None
    pages: list[SitemapPage] = []
    status: ApprovalStatus = ApprovalStatus.PENDING
    client_feedback: str | None = None

    def get_page(self, page_id: str) -> SitemapPage | None:
        return next((p for p in self.pages if p.id == page_id), None)

    def top_level_pages(self) -> list[SitemapPage]:
        return sorted([p for p in self.pages if p.parent_id is None], key=lambda p: p.order)

    def children_of(self, page_id: str) -> list[SitemapPage]:
        return sorted([p for p in self.pages if p.parent_id == page_id], key=lambda p: p.order)


# ── Page Content ──────────────────────────────────────────────────────────────

class ContentSection(BaseModel):
    """A single content section on a page (e.g. hero, services list, CTA)."""
    title: str                          # Section heading
    body: str                           # Body copy
    cta_text: str | None = None         # Call-to-action button text
    cta_url: str | None = None          # CTA destination (if known)
    notes: str | None = None            # Developer/designer notes for this section


class PageContent(BaseModel):
    """Generated or provided content for a single sitemap page."""
    client_id: str
    page_id: str                        # References SitemapPage.id
    notion_page_id: str | None = None
    content_mode: ContentMode
    sections: list[ContentSection] = []
    meta_title: str | None = None       # SEO title tag
    meta_description: str | None = None # SEO meta description
    status: ApprovalStatus = ApprovalStatus.PENDING
    client_feedback: str | None = None


# ── CMS Collection Schema ─────────────────────────────────────────────────────

class CmsField(BaseModel):
    name: str
    field_type: str                     # Webflow field types: "PlainText", "RichText", "Image", etc.
    required: bool = False
    help_text: str | None = None


class CmsCollection(BaseModel):
    """Schema for a Webflow CMS collection (e.g. Blog Posts, Team Members)."""
    client_id: str
    collection_name: str                # e.g. "Blog Posts", "Team Members"
    slug: str                           # e.g. "blog-posts", "team"
    fields: list[CmsField] = []
    sample_entries: list[dict] = []     # 1–2 sample entries to seed the collection
    notion_page_id: str | None = None


# ── Wireframe ─────────────────────────────────────────────────────────────────

class RelumeComponent(BaseModel):
    """A single Relume component placed in the wireframe."""
    section_name: str                   # e.g. "Hero", "Services", "CTA"
    relume_component_id: str            # e.g. "Hero - 1", "Features - 3", "CTA - 4"
    content_notes: str                  # What content/copy goes in this component
    order: int                          # Position on the page


class WireframeSpec(BaseModel):
    """Relume-based wireframe specification for a single page."""
    client_id: str
    page_id: str                        # References SitemapPage.id
    notion_page_id: str | None = None
    components: list[RelumeComponent] = []
    layout_notes: str | None = None     # Any layout decisions not captured in components
    figma_url: str | None = None        # Populated once Figma wireframe is built
    status: ApprovalStatus = ApprovalStatus.PENDING
    client_feedback: str | None = None


# ── High-Fidelity Design Brief ────────────────────────────────────────────────

class HifidelityBrief(BaseModel):
    """Design direction brief for the homepage high-fidelity mockup."""
    client_id: str
    notion_page_id: str | None = None
    approved_mood_board_variation_id: str   # Which mood board variation to apply
    typography_notes: str | None = None     # Font pairing, size hierarchy
    color_application_notes: str | None = None  # How to apply the color palette
    imagery_direction: str | None = None    # Photo style, illustration style, etc.
    desktop_figma_url: str | None = None    # Populated once designed
    mobile_figma_url: str | None = None
    status: ApprovalStatus = ApprovalStatus.PENDING
    client_feedback: str | None = None
