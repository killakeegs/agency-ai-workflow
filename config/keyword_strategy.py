"""
config/keyword_strategy.py — Per-page-kind keyword strategy rules.

Without explicit strategy per page type, Claude tends to pick the MOST SPECIFIC
keyword for every page, which creates two big problems:
  1. Home pages get single-service keywords for multi-service practices
     (e.g., Summit with Speech + OT + PT got "pediatric therapy clinic Frisco TX"
     which collapses onto one service + one location)
  2. Pages duplicate each other's primary keywords (home vs service hub)

This module provides page-kind-specific keyword guidance that the sitemap agent
injects into its personalization prompt. Claude still picks the actual words,
but within guardrails that match how the SEO team actually thinks about each
page's role in the site.

Used by: src/agents/sitemap.py (_build_from_template personalization call)
"""
from __future__ import annotations


# ─── Per-page-kind strategy ──────────────────────────────────────────────────

KEYWORD_STRATEGY: dict[str, dict] = {

    # ── HOME — represents the WHOLE business ───────────────────────────────
    "home": {
        "pattern": "Umbrella category + broadest geo that covers ALL the client's services and locations",
        "priority": "HIGHEST — this is the brand page, not a service page",
        "must_not": [
            "Pick ONE specific service when the client offers multiple — use the umbrella category instead",
            "Pick ONE city when the client has multiple locations — use the metro, region, or comma-separate",
            "Duplicate the primary keyword of any service hub or location page",
        ],
        "decision_tree": [
            "If single service + single location → `[service] clinic [city] [state]`  (e.g., 'addiction treatment Columbus Ohio')",
            "If multi-service + single location → `[umbrella category] [city] [state]`  (e.g., 'pediatric therapy clinic Frisco Texas')",
            "If single service + multi-location → `[service] [metro or region]`  (e.g., 'physical therapy north Dallas')",
            "If multi-service + multi-location → `[umbrella] [primary + secondary city]` OR `[umbrella] [region]`  (e.g., 'pediatric therapy Frisco McKinney' or 'children's therapy north Texas')",
        ],
        "umbrella_examples": {
            "speech_pathology + occupational_therapy + physical_therapy": "pediatric therapy",
            "addiction_treatment + mental_health": "behavioral health treatment",
            "dermatology": "dermatology clinic",
            "physical_therapy": "physical therapy",
        },
    },

    # ── SERVICES HUB — one level below home ────────────────────────────────
    "services_hub": {
        "pattern": "`[Client's range of services] [primary city or metro]` — broader than any one service page, narrower than home",
        "priority": "MEDIUM",
        "must_not": [
            "Duplicate the home page keyword",
            "Pick a single sub-service — this page represents ALL services",
        ],
        "examples": [
            "addiction treatment services Columbus Ohio",
            "speech and occupational therapy Frisco",
            "behavioral health programs Columbus",
        ],
    },

    # ── INDIVIDUAL SERVICE PAGES (IOP, PHP, MAT, etc.) ─────────────────────
    "service_detox":         {"pattern": "medical detox [city] [state]", "priority": "HIGH", "must_differ_from": ["home", "services_hub", "other services"]},
    "service_residential":   {"pattern": "residential addiction treatment [city] [state]", "priority": "HIGH"},
    "service_php":           {"pattern": "partial hospitalization program [city] [state]", "priority": "HIGH"},
    "service_iop":           {"pattern": "intensive outpatient program [city] [state]", "priority": "HIGH — IOP is typically the highest-search-volume level of care"},
    "service_outpatient":    {"pattern": "outpatient addiction treatment [city] [state]"},
    "service_mat":           {"pattern": "medication-assisted treatment [city] [state] OR MAT [city] [state]"},
    "service_mental_health": {"pattern": "mental health therapy [city] [state] OR [condition] therapy [city]"},
    "service_dual_diagnosis":{"pattern": "dual diagnosis treatment [city] [state]"},
    "service_sober_living":  {"pattern": "sober living [city] [state] OR recovery housing [city]"},

    # Speech / OT / PT verticals (future templates)
    "service_speech_therapy":       {"pattern": "pediatric speech therapy [city] OR speech therapy clinic [city]"},
    "service_occupational_therapy": {"pattern": "pediatric occupational therapy [city] OR OT clinic [city]"},
    "service_physical_therapy":     {"pattern": "physical therapy [city] OR [specialty] PT [city]"},

    # ── SERVICE SUBCATEGORY (CMS, Tier 2 SEO) ──────────────────────────────
    "service_substance_cms": {
        "pattern": "`[service] for [substance] addiction [city] [state]`  (e.g., 'IOP for alcohol addiction Columbus Ohio')",
        "priority": "HIGH — primary SEO play for Tier 2",
        "rules": [
            "Each CMS entry gets its own unique keyword",
            "Include both the substance AND the service level (IOP, PHP, etc.)",
            "Include city for local intent",
        ],
    },

    # ── LOCATIONS ──────────────────────────────────────────────────────────
    "locations_hub": {
        "pattern": "[umbrella service] locations [metro area]",
        "priority": "MEDIUM",
    },
    "location": {
        "pattern": "`[umbrella service] [specific city] [state]`  (e.g., 'pediatric therapy McKinney Texas')",
        "priority": "HIGH — local SEO signal",
        "rules": [
            "Each city page gets its own primary keyword with THAT city",
            "Must differ from home + from other location pages",
        ],
    },

    # ── PATIENT RESOURCES ──────────────────────────────────────────────────
    "admissions":      {"pattern": "how to get into [service] [city] OR admissions process [service] [city]", "priority": "MEDIUM"},
    "insurance":       {"pattern": "does insurance cover [service] [city] OR insurance accepted [service] [city]", "priority": "MEDIUM"},
    "what_to_expect":  {"pattern": "what to expect at [service] [city]", "priority": "LOW"},
    "faq":             {"pattern": "[service] FAQ [city] OR [service] questions [city]", "priority": "LOW"},
    "new_patients":    {"pattern": "new patient intake [service] [city]", "priority": "LOW"},

    # ── WHO WE SERVE ───────────────────────────────────────────────────────
    "who_we_serve_hub": {
        "pattern": "[service] for [populations served] [city]",
        "priority": "LOW — brand page, supports specificity when Tier 3 subpages exist",
    },
    "who_we_serve_children": {"pattern": "pediatric [service] [city]", "priority": "HIGH"},
    "who_we_serve_adults":   {"pattern": "adult [service] [city]",     "priority": "HIGH"},

    # ── TRUST / BRAND ──────────────────────────────────────────────────────
    "about":    {"pattern": "[client name] OR about [client name] — brand-focused, LOW SEO priority", "priority": "LOW"},
    "team":     {"pattern": "[client name] team OR [service] providers [city]", "priority": "LOW"},
    "gallery":  {"pattern": "[client name] facility tour OR [client name] photos", "priority": "LOW"},
    "contact":  {"pattern": "contact [client name] OR [client name] phone number", "priority": "LOW"},
    "outcomes": {"pattern": "[service] success rate [city] OR [service] outcomes [city]", "priority": "MEDIUM — if client has real outcome data"},

    # ── BLOG ───────────────────────────────────────────────────────────────
    "blog_hub":  {"pattern": "[service] resources OR [service] education [city]", "priority": "LOW"},
    "blog_post": {"pattern": "per-post keyword driven by post topic — sitemap template shouldn't set", "priority": "N/A"},

    # ── LEGAL ──────────────────────────────────────────────────────────────
    "legal":     {"pattern": "N/A — legal pages are not SEO-targeted, leave primary_keyword empty or brand-only", "priority": "NONE"},
}


# ─── Prompt builder ──────────────────────────────────────────────────────────

def get_strategy_for(page_kind: str) -> dict:
    """Return the strategy config for a page kind, or a sensible default."""
    return KEYWORD_STRATEGY.get(page_kind, {
        "pattern": "Use the page topic + primary location. Don't duplicate other pages.",
        "priority": "MEDIUM",
    })


def format_strategy_for_prompt(page_kind: str) -> str:
    """Produce a one-paragraph strategy guidance string for a given page kind."""
    strat = get_strategy_for(page_kind)
    parts = []
    if "pattern" in strat:
        parts.append(f"Pattern: {strat['pattern']}")
    if "priority" in strat:
        parts.append(f"Priority: {strat['priority']}")
    if "must_not" in strat and strat["must_not"]:
        parts.append("Must NOT: " + "; ".join(strat["must_not"]))
    if "must_differ_from" in strat and strat["must_differ_from"]:
        parts.append("Must differ from keywords on: " + ", ".join(strat["must_differ_from"]))
    if "decision_tree" in strat and strat["decision_tree"]:
        parts.append("Decide: " + " | ".join(strat["decision_tree"]))
    if "rules" in strat and strat["rules"]:
        parts.append("Rules: " + "; ".join(strat["rules"]))
    return " — ".join(parts)


# ─── Post-generation validator ───────────────────────────────────────────────

def audit_keywords(pages: list[dict]) -> dict:
    """Scan generated pages for keyword quality issues.

    Returns a dict with:
      - duplicates: list of (keyword, [page_slugs]) where same keyword used 2+ times
      - missing:    list of page slugs with no primary_keyword (excluding legal)
      - short:      list of slugs where primary_keyword is < 3 words (too thin)
    """
    by_keyword: dict[str, list[str]] = {}
    missing: list[str] = []
    short: list[str] = []

    for p in pages:
        slug = p.get("slug", "")
        kw = (p.get("primary_keyword") or "").strip().lower()
        page_kind = p.get("page_kind", "")

        if not kw:
            if page_kind != "legal":
                missing.append(slug)
            continue

        if len(kw.split()) < 3:
            short.append(f"{slug}: {kw!r}")

        by_keyword.setdefault(kw, []).append(slug)

    duplicates = [(kw, slugs) for kw, slugs in by_keyword.items() if len(slugs) > 1]

    return {
        "duplicates": duplicates,
        "missing":    missing,
        "short":      short,
        "total":      len(pages),
        "with_kw":    len(pages) - len(missing),
    }
