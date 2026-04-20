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
        "Hero",
        "Leadership & Clinical Team (inline cards with photo, name, credentials, short bio — modal or expandable for full bio)",
        "Medical Director Spotlight (if named)",
        "Credentials & Accreditations",
        "Join Our Team / Careers CTA",
        "Final CTA",
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

    # ── Addiction Treatment ───────────────────────────────────────────────────
    # Service pages follow a consistent pattern — what it is, who it's for,
    # what happens day-to-day, who treats it, insurance, and conversion.

    "services_hub": [
        "Hero",
        "Overview — Full Continuum of Care",
        "Level of Care Cards (PHP / IOP / OP / MAT / etc.)",
        "Our Treatment Philosophy",
        "Insurance Accepted",
        "What Makes Us Different",
        "Final CTA",
    ],

    "service_detox": [
        "Hero",
        "What Is Medical Detox",
        "Substances We Detox From",
        "Medical Supervision & Safety",
        "Medications Used During Detox",
        "Typical Length of Stay",
        "Next Steps After Detox",
        "Reviewed by [Medical Director]",
        "Final CTA",
    ],

    "service_residential": [
        "Hero",
        "What Is Residential Treatment",
        "Who It's For",
        "Daily Schedule",
        "Amenities & Environment",
        "Typical Length of Stay",
        "Reviewed by [Medical Director]",
        "Final CTA",
    ],

    "service_php": [
        "Hero",
        "What Is PHP",
        "Who PHP Is For",
        "Daily Schedule",
        "Typical Length of Stay",
        "Insurance & Cost",
        "Reviewed by [Medical Director]",
        "Final CTA",
    ],

    "service_iop": [
        "Hero",
        "What Is IOP",
        "Who IOP Is For",
        "Weekly Schedule",
        "What Happens in Sessions",
        "Length of Program",
        "Insurance & Cost",
        "Reviewed by [Medical Director]",
        "Final CTA",
    ],

    "service_outpatient": [
        "Hero",
        "What Is Outpatient Treatment",
        "Who It's For",
        "Session Frequency",
        "What Happens in Sessions",
        "Insurance & Cost",
        "Reviewed by [Medical Director]",
        "Final CTA",
    ],

    "service_mat": [
        "Hero",
        "What Is MAT",
        "Medications We Use (Suboxone, Vivitrol, Naltrexone)",
        "How MAT Fits Into Treatment",
        "Myths About MAT",
        "Our Prescribers",
        "Reviewed by [Medical Director]",
        "Final CTA",
    ],

    "service_mental_health": [
        "Hero",
        "Conditions We Treat",
        "Our Approach",
        "Therapy Modalities Offered",
        "Individual vs Group Therapy",
        "Reviewed by [Medical Director]",
        "Final CTA",
    ],

    "service_dual_diagnosis": [
        "Hero",
        "What Is Dual Diagnosis",
        "Why Integrated Treatment Matters",
        "Conditions We Treat Alongside SUD",
        "Our Clinical Team",
        "Reviewed by [Medical Director]",
        "Final CTA",
    ],

    "service_sober_living": [
        "Hero",
        "About Our Sober Living",
        "Who It's For",
        "Daily Structure & House Rules",
        "Length of Stay",
        "Gallery / Virtual Tour",
        "How to Apply",
        "Final CTA",
    ],

    "service_substance_cms": [
        "Hero — [Service] for [Substance] Addiction",
        "About [Substance] Addiction",
        "Signs & Symptoms",
        "How We Treat [Substance] Addiction",
        "What to Expect in Treatment",
        "Reviewed by [Medical Director]",
        "Final CTA",
    ],

    # Admissions flow
    "admissions": [
        "Hero",
        "How to Get Started (3-step)",
        "What Happens on the First Call",
        "Pre-Admission Assessment",
        "Day One at Our Program",
        "Family Involvement",
        "Final CTA",
    ],

    "what_to_expect": [
        "Hero",
        "Your First Day",
        "Your First Week",
        "Weekly Schedule",
        "Who You'll Meet",
        "What to Bring",
        "Final CTA",
    ],

    # Other
    "who_we_serve_hub": [
        "Hero",
        "Populations We Serve",
        "Specialized Focus Areas",
        "Final CTA",
    ],

    "gallery": [
        "Hero",
        "Facility Photos",
        "Common Areas",
        "Therapy Spaces",
        "Outdoor / Recreation",
        "Virtual Tour CTA",
    ],

    "faq": [
        "Hero",
        "Getting Started",
        "Treatment & Clinical",
        "Insurance & Cost",
        "Daily Life in Treatment",
        "Family & Support",
        "Final CTA",
    ],

    "outcomes": [
        "Hero",
        "Completion Rates",
        "Patient Satisfaction",
        "Methodology",
        "Testimonial Highlight",
        "Final CTA",
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
