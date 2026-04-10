"""
config/page_sections.py — Standard section templates for each page type.

These are the baseline sections every page of a given type should have.
Used by the SitemapAgent when generating Key Sections, and by the
generate_sections.py script to populate existing sitemap pages.

Each entry is a list of section names in display order.
Client-specific additions/removals are made during sitemap revision.

Rules:
- Testimonials are embedded on Home and all service pages — no standalone Testimonials page.
- Careers is excluded by default — add only if client explicitly requests it.
- "Reviewed by [Therapist]" is always required on service hub, service subcategory, and blog post pages.
- Blog preview (latest articles) is always on the Home page.
"""

PAGE_SECTIONS: dict[str, list[str]] = {

    # ── Core ──────────────────────────────────────────────────────────────────

    "home": [
        "Hero",
        "Services Overview",
        "Who We Serve",
        "Why Us",
        "Testimonials",
        "How to Get Started",
        "Locations",
        "Insurance",
        "Blog Preview",
        "Final CTA",
    ],

    "about": [
        "Hero",
        "Our Story",
        "Mission & Philosophy",
        "Our Team",
        "Our Clinics",
        "Credentials & Affiliations",
        "Final CTA",
    ],

    "team": [
        "Page Hero",
        "Team Grid",
        "Join Our Team CTA",
    ],

    "contact": [
        "Hero",
        "Appointment Request Form",
        "Location Details",
        "Map & Directions",
    ],

    # ── Who We Serve ──────────────────────────────────────────────────────────

    "who_we_serve_children": [
        "Hero",
        "Services for Children",
        "Age Groups We Serve",
        "Common Challenges",
        "What to Expect",
        "Testimonials",
        "Final CTA",
    ],

    "who_we_serve_adults": [
        "Hero",
        "Services for Adults",
        "Common Challenges",
        "What to Expect",
        "Testimonials",
        "Final CTA",
    ],

    # ── Services ──────────────────────────────────────────────────────────────

    "service_hub": [
        "Hero",
        "Overview",
        "Conditions We Help With",
        "Our Approach",
        "Who This Is For",
        "Reviewed by [Therapist]",
        "Testimonials",
        "Final CTA",
    ],

    "service_subcategory": [
        "Hero",
        "What Is [Condition]",
        "Signs & Symptoms",
        "How We Treat It",
        "What to Expect",
        "Reviewed by [Therapist]",
        "Testimonials",
        "Related Services",
        "Final CTA",
    ],

    # ── Locations ─────────────────────────────────────────────────────────────

    "locations_hub": [
        "Hero",
        "Location Cards",
        "Services Offered",
        "Insurance Accepted",
        "Final CTA",
    ],

    "location": [
        "Hero",
        "Location Details",
        "Map & Directions",
        "Services at This Location",
        "Insurance Accepted",
        "Testimonials",
        "Final CTA",
    ],

    # ── Patient Resources ─────────────────────────────────────────────────────

    "new_patients": [
        "Hero",
        "Before Your First Visit",
        "What to Bring",
        "Intake Forms",
        "FAQ",
        "Insurance Overview",
        "Final CTA",
    ],

    "insurance": [
        "Hero",
        "Accepted Insurance",
        "Self-Pay / Private Pay",
        "How Billing Works",
        "FAQ",
        "Contact for Billing Questions",
    ],

    # ── Blog ──────────────────────────────────────────────────────────────────

    "blog_hub": [
        "Hero",
        "Featured Post",
        "Posts Grid",
        "Category Filter",
        "Newsletter Signup",
        "Final CTA",
    ],

    "blog_post": [
        "Article Header",
        "Article Body",
        "Reviewed by [Therapist]",
        "About the Author",
        "Related Posts",
        "Final CTA",
    ],

    # ── Legal ─────────────────────────────────────────────────────────────────

    "legal": [
        "Page Header",
        "Body Content",
        "Contact Information",
    ],
}


def get_sections(template_key: str) -> list[str]:
    """Return the section list for a given template key, or empty list if unknown."""
    return PAGE_SECTIONS.get(template_key, [])


def infer_template_key(slug: str, title: str, section: str) -> str:
    """
    Infer the page_sections template key from a page's slug, title, and section.
    Used when auto-populating Key Sections for existing sitemap pages.
    """
    s = slug.lower().strip("/")
    t = title.lower()

    if s == "":
        return "home"
    if s == "about":
        return "about"
    if s == "about/team":
        return "team"
    if s == "contact":
        return "contact"
    if s == "who-we-serve/children":
        return "who_we_serve_children"
    if s == "who-we-serve/adults":
        return "who_we_serve_adults"
    if s in ("services/speech-therapy", "services/occupational-therapy", "services/physical-therapy"):
        return "service_hub"
    if s.startswith("services/") and s.count("/") >= 2:
        return "service_subcategory"
    if s == "locations":
        return "locations_hub"
    if s.startswith("locations/"):
        return "location"
    if s == "new-patients":
        return "new_patients"
    if s == "insurance":
        return "insurance"
    if s == "blog" and "[" not in s:
        return "blog_hub"
    if s.startswith("blog/"):
        return "blog_post"
    if s in ("privacy-policy", "terms", "accessibility"):
        return "legal"

    return ""
