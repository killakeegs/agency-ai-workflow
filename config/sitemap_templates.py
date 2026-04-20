"""
config/sitemap_templates.py — Templated sitemap baselines per vertical.

Every sitemap for a given vertical starts from a locked template. The sitemap
agent creates these pages deterministically (no AI for page structure — only
for per-page Purpose, Primary Keyword guess, and Tier 3 suggestions).

Tiers:
  - Tier 1 (always): core navigation + standard service pages
  - Tier 2 (only if services.seo = true): programmatic CMS collections + SEO
                                          pages like Outcomes
  - Tier 3 (AI-suggested): pages proposed by Claude from Business Profile +
                          Client Log context — written to the Sitemap DB as
                          "Suggested" status for team approval

Template entry schema:
  {
    "title": str,                    # display name
    "slug": str,                     # URL path (must start with / or be {placeholder})
    "page_type": "Static" | "CMS",
    "content_mode": "AI Generated" | "Client Provided",
    "section": str,                  # matches Sitemap DB Section options
    "parent_slug": str | None,       # slug of parent page for hierarchy
    "order": int,                    # sort order in the Sitemap DB
    "page_kind": str,                # key for config/page_sections.py + purpose templates
    "conditional": dict | None,      # {"type": "service_offering", "service": "detox"}
                                     # → agent checks Business Profile before creating
  }
"""
from __future__ import annotations


# ─── Addiction Treatment Template ────────────────────────────────────────────

_ADDICTION_TIER1_CORE = [
    {
        "title":        "Home",
        "slug":         "/",
        "page_type":    "Static",
        "content_mode": "AI Generated",
        "section":      "Core",
        "parent_slug":  None,
        "order":        0,
        "page_kind":    "home",
    },
    {
        "title":        "About Us",
        "slug":         "/about",
        "page_type":    "Static",
        "content_mode": "AI Generated",
        "section":      "Core",
        "parent_slug":  None,
        "order":        1,
        "page_kind":    "about",
    },
    {
        "title":        "Team & Providers",
        "slug":         "/team",
        "page_type":    "Static",
        "content_mode": "AI Generated",
        "section":      "Core",
        "parent_slug":  None,
        "order":        2,
        "page_kind":    "team",
        # Team bios are rendered inline on this page (photo + name + short bio
        # per member, either as cards or modal pop-ups). No separate URL-level
        # staff pages — simpler nav, no thin CMS pages.
    },
    {
        "title":        "Services",
        "slug":         "/services",
        "page_type":    "Static",
        "content_mode": "AI Generated",
        "section":      "Services",
        "parent_slug":  None,
        "order":        4,
        "page_kind":    "services_hub",
    },
    # Service pages are conditionally included — inserted below in _ADDICTION_SERVICES
    {
        "title":        "Admissions / Get Started",
        "slug":         "/admissions",
        "page_type":    "Static",
        "content_mode": "AI Generated",
        "section":      "Core",
        "parent_slug":  None,
        "order":        20,
        "page_kind":    "admissions",
    },
    {
        "title":        "Insurance & Payment",
        "slug":         "/admissions/insurance",
        "page_type":    "Static",
        "content_mode": "AI Generated",
        "section":      "Core",
        "parent_slug":  "/admissions",
        "order":        21,
        "page_kind":    "insurance",
    },
    {
        "title":        "What to Expect",
        "slug":         "/admissions/what-to-expect",
        "page_type":    "Static",
        "content_mode": "AI Generated",
        "section":      "Core",
        "parent_slug":  "/admissions",
        "order":        22,
        "page_kind":    "what_to_expect",
    },
    {
        "title":        "Who We Serve",
        "slug":         "/who-we-serve",
        "page_type":    "Static",
        "content_mode": "AI Generated",
        "section":      "Who We Serve",
        "parent_slug":  None,
        "order":        30,
        "page_kind":    "who_we_serve_hub",
    },
    {
        "title":        "Gallery",
        "slug":         "/gallery",
        "page_type":    "Static",
        "content_mode": "AI Generated",
        "section":      "Core",
        "parent_slug":  None,
        "order":        40,
        "page_kind":    "gallery",
    },
    {
        "title":        "FAQ",
        "slug":         "/faq",
        "page_type":    "Static",
        "content_mode": "AI Generated",
        "section":      "Patient Resources",
        "parent_slug":  None,
        "order":        50,
        "page_kind":    "faq",
    },
    {
        "title":        "Contact",
        "slug":         "/contact",
        "page_type":    "Static",
        "content_mode": "AI Generated",
        "section":      "Core",
        "parent_slug":  None,
        "order":        60,
        "page_kind":    "contact",
    },
    {
        "title":        "Blog",
        "slug":         "/blog",
        "page_type":    "Static",
        "content_mode": "AI Generated",
        "section":      "Blog",
        "parent_slug":  None,
        "order":        70,
        "page_kind":    "blog_hub",
    },
    {
        "title":        "Blog Post",
        "slug":         "/blog/{slug}",
        "page_type":    "CMS",
        "content_mode": "AI Generated",
        "section":      "Blog",
        "parent_slug":  "/blog",
        "order":        71,
        "page_kind":    "blog_post",
    },
]


_ADDICTION_SERVICES = [
    # All default ON — conditional.service is the key the agent checks against
    # the Business Profile's Levels of Care section. BP explicitly saying "no X"
    # (or equivalent negative) excludes that service.
    {
        "title":        "Detox",
        "slug":         "/services/detox",
        "page_type":    "Static",
        "content_mode": "AI Generated",
        "section":      "Services",
        "parent_slug":  "/services",
        "order":        5,
        "page_kind":    "service_detox",
        "conditional": {"type": "service_offering", "service": "detox"},
    },
    {
        "title":        "Residential / Inpatient",
        "slug":         "/services/residential",
        "page_type":    "Static",
        "content_mode": "AI Generated",
        "section":      "Services",
        "parent_slug":  "/services",
        "order":        6,
        "page_kind":    "service_residential",
        "conditional": {"type": "service_offering", "service": "residential"},
    },
    {
        "title":        "Partial Hospitalization Program (PHP)",
        "slug":         "/services/php",
        "page_type":    "Static",
        "content_mode": "AI Generated",
        "section":      "Services",
        "parent_slug":  "/services",
        "order":        7,
        "page_kind":    "service_php",
        "conditional": {"type": "service_offering", "service": "php"},
    },
    {
        "title":        "Intensive Outpatient Program (IOP)",
        "slug":         "/services/iop",
        "page_type":    "Static",
        "content_mode": "AI Generated",
        "section":      "Services",
        "parent_slug":  "/services",
        "order":        8,
        "page_kind":    "service_iop",
        "conditional": {"type": "service_offering", "service": "iop"},
    },
    {
        "title":        "Outpatient Program",
        "slug":         "/services/outpatient",
        "page_type":    "Static",
        "content_mode": "AI Generated",
        "section":      "Services",
        "parent_slug":  "/services",
        "order":        9,
        "page_kind":    "service_outpatient",
        "conditional": {"type": "service_offering", "service": "outpatient"},
    },
    {
        "title":        "Medication-Assisted Treatment",
        "slug":         "/services/medication-management",
        "page_type":    "Static",
        "content_mode": "AI Generated",
        "section":      "Services",
        "parent_slug":  "/services",
        "order":        10,
        "page_kind":    "service_mat",
        "conditional": {"type": "service_offering", "service": "mat"},
    },
    {
        "title":        "Mental Health Therapy",
        "slug":         "/services/mental-health",
        "page_type":    "Static",
        "content_mode": "AI Generated",
        "section":      "Services",
        "parent_slug":  "/services",
        "order":        11,
        "page_kind":    "service_mental_health",
        "conditional": {"type": "service_offering", "service": "mental_health"},
    },
    {
        "title":        "Dual Diagnosis Treatment",
        "slug":         "/services/dual-diagnosis",
        "page_type":    "Static",
        "content_mode": "AI Generated",
        "section":      "Services",
        "parent_slug":  "/services",
        "order":        12,
        "page_kind":    "service_dual_diagnosis",
        "conditional": {"type": "service_offering", "service": "dual_diagnosis"},
    },
    {
        "title":        "Sober Living / Housing",
        "slug":         "/services/sober-living",
        "page_type":    "Static",
        "content_mode": "AI Generated",
        "section":      "Services",
        "parent_slug":  "/services",
        "order":        13,
        "page_kind":    "service_sober_living",
        "conditional": {"type": "service_offering", "service": "sober_living"},
    },
]


_ADDICTION_LEGAL = [
    {
        "title":        "Privacy Policy",
        "slug":         "/privacy-policy",
        "page_type":    "Static",
        "content_mode": "Client Provided",
        "section":      "Legal",
        "parent_slug":  None,
        "order":        90,
        "page_kind":    "legal",
    },
    {
        "title":        "Terms of Service",
        "slug":         "/terms",
        "page_type":    "Static",
        "content_mode": "Client Provided",
        "section":      "Legal",
        "parent_slug":  None,
        "order":        91,
        "page_kind":    "legal",
    },
    {
        "title":        "Accessibility Statement",
        "slug":         "/accessibility",
        "page_type":    "Static",
        "content_mode": "AI Generated",
        "section":      "Legal",
        "parent_slug":  None,
        "order":        92,
        "page_kind":    "legal",
    },
    {
        "title":        "HIPAA Notice of Privacy Practices",
        "slug":         "/hipaa",
        "page_type":    "Static",
        "content_mode": "Client Provided",
        "section":      "Legal",
        "parent_slug":  None,
        "order":        93,
        "page_kind":    "legal",
    },
]


_ADDICTION_TIER2 = [
    # Only created when services.seo = true
    {
        "title":        "Services by Substance",
        "slug":         "/services/iop/{substance}",
        "page_type":    "CMS",
        "content_mode": "AI Generated",
        "section":      "Service Subcategories",
        "parent_slug":  "/services/iop",
        "order":        15,
        "page_kind":    "service_substance_cms",
    },
    {
        "title":        "Areas We Serve",
        "slug":         "/locations/{city}",
        "page_type":    "CMS",
        "content_mode": "AI Generated",
        "section":      "Locations",
        "parent_slug":  None,
        "order":        80,
        "page_kind":    "location",
    },
    {
        "title":        "Outcomes & Results",
        "slug":         "/outcomes",
        "page_type":    "Static",
        "content_mode": "AI Generated",
        "section":      "Patient Resources",
        "parent_slug":  None,
        "order":        52,
        "page_kind":    "outcomes",
    },
]


ADDICTION_TREATMENT_TEMPLATE = {
    "vertical":    "addiction_treatment",
    "tier1":       _ADDICTION_TIER1_CORE + _ADDICTION_SERVICES + _ADDICTION_LEGAL,
    "tier2":       _ADDICTION_TIER2,
    # Tier 3 rules: what to tell Claude to look for when proposing client-specific pages
    "tier3_prompt_guidance": """\
Propose ADDITIONAL pages for this client's sitemap based on specifics from their
onboarding form + kickoff meeting. The baseline template already includes all core
nav, service pages, admissions, gallery, FAQ, blog, and legal pages. Only suggest
pages that are genuinely differentiated based on what this specific client is doing
or serving.

Common Tier 3 suggestions for addiction treatment:
- Who We Serve subpages (College Students, Court-Referred, Veterans, LGBTQ+,
  Young Adults, Families, Professionals) — ONLY if the BP/meeting explicitly
  names this population as a focus with supporting context.
- Our Approach / Methodology — if the client has a specific differentiating
  philosophy (e.g., integrated housing model, evidence-based, faith-based,
  trauma-informed).
- Referrals — if referral partnerships are a stated focus (drug courts,
  community organizations, EAPs).
- Medical Director page — if the client has a named medical director they
  want to highlight for E-E-A-T.
- Specific program pages (e.g., Alumni Program, Family Program, Continuing Care)
  — ONLY if mentioned as a distinct offering.

DO NOT suggest:
- Pages already in the template baseline.
- Generic pages that don't reflect anything specific about this client.
- Pages that would duplicate existing structure.
""",
}


# ─── Vertical → template mapping ─────────────────────────────────────────────

SITEMAP_TEMPLATES = {
    "addiction_treatment": ADDICTION_TREATMENT_TEMPLATE,
    # Future templates: speech_pathology, occupational_therapy, physical_therapy,
    # mental_health, dermatology. Each follows the same schema.
}


def get_template(vertical: str) -> dict | None:
    """Return the sitemap template for a vertical, or None if no template exists."""
    return SITEMAP_TEMPLATES.get(vertical)


# ─── Service detection — reads BP to decide which conditional services to include ─

# Negative phrases that indicate a service is NOT offered. Checked against the
# BP's Levels of Care section text (lowercased). Any match → service excluded.
_NEGATIVE_PATTERNS_BY_SERVICE: dict[str, list[str]] = {
    "detox":            ["no detox", "does not offer detox", "don't offer detox", "do not offer detox", "no medically-managed detox", "no medical detox", "detox is not"],
    "residential":      ["no residential", "does not offer residential", "don't offer residential", "do not offer residential", "no inpatient", "residential is not"],
    "php":              ["no php", "does not offer php", "don't offer php", "no partial hospitalization"],
    "iop":              ["no iop", "does not offer iop", "don't offer iop", "no intensive outpatient"],
    "outpatient":       ["no outpatient", "does not offer outpatient", "no standard outpatient"],
    "mat":              ["no mat", "no medication-assisted", "no medication assisted", "no medication management", "does not prescribe"],
    "mental_health":    ["no mental health", "no co-occurring", "no dual diagnosis"],  # rare negative
    "dual_diagnosis":   ["no dual diagnosis", "no co-occurring"],
    "sober_living":     ["no sober living", "does not offer housing", "no sober housing", "no recovery housing"],
}


def service_excluded_by_bp(service_key: str, bp_text: str) -> bool:
    """Return True if Business Profile text explicitly excludes this service."""
    patterns = _NEGATIVE_PATTERNS_BY_SERVICE.get(service_key, [])
    if not patterns:
        return False
    lower = bp_text.lower()
    return any(p in lower for p in patterns)
