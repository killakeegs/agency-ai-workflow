"""
client_readiness.py — Consolidated "what does a client need to be filled out
before downstream agents can work from it?" definition.

Four sources get checked (in depth order, shallow → deep):

  1. Clients DB            - top-level agency command center (workspace-wide)
  2. clients.json          - config registry
  3. Client Info DB        - per-client relationship / ops metadata
  4. Brand Guidelines DB   - per-client voice + visual identity
  5. Business Profile page - per-client deep knowledge (per-vertical sections,
                             in config/business_profile_requirements.py)

Each bucket has a list of REQUIRED fields (Blocked if missing) and
NICE_TO_HAVE fields (Partial if missing, not blocking).

SERVICE-CONDITIONAL FIELDS: some required fields are only required if the
client has a specific service active. Expressed via the `required_if` key:

  {"name": "SEO Mode", "required_if": "seo"} - only required if services.seo

Most fields are unconditional (Brand Guidelines is unconditional per
Keegan's directive — we fill everything upfront so future service
expansions don't hit readiness gates).
"""
from __future__ import annotations


# ── Clients DB (workspace-level, one row per client) ─────────────────────────

# The Clients DB is queried via cross-client filters (SEO mode splits, retainer
# totals, portfolio views). Fields here matter for agency-wide operations.
CLIENTS_DB_REQUIRED: list[dict] = [
    {"name": "Client Name",     "empty_check": "title"},
    {"name": "Services",        "empty_check": "multi_select"},
    {"name": "Vertical",        "empty_check": "rich_text"},
    {"name": "Primary Contact", "empty_check": "rich_text"},
    {"name": "Contact Email",   "empty_check": "email"},
    {"name": "Account Manager", "empty_check": "rich_text"},
    {"name": "Pipeline Stage",  "empty_check": "rich_text"},
    {"name": "SEO Mode",        "empty_check": "select", "required_if": "seo"},
    {"name": "Status",          "empty_check": "select"},
]

CLIENTS_DB_NICE_TO_HAVE: list[dict] = [
    {"name": "Start Date",        "empty_check": "date"},
    {"name": "Monthly Retainer",  "empty_check": "number"},
    {"name": "Secondary Contacts","empty_check": "rich_text"},
    {"name": "Client Page",       "empty_check": "url"},
    {"name": "Last Contact",      "empty_check": "date"},
]


# ── clients.json (config/clients.json registry) ─────────────────────────────

# These fields aren't in a Notion DB — they're in the JSON registry file.
# Canonical address is the authoritative source for state allowlist / geo
# filtering throughout the keyword pipeline — without it, local SEO doesn't
# function.
CLIENTS_JSON_REQUIRED: list[dict] = [
    {"name": "name",                  "empty_check": "str"},
    {"name": "vertical",              "empty_check": "list"},
    {"name": "canonical_address",     "empty_check": "str",
     "required_if": "local_or_hybrid_seo"},
    {"name": "canonical_phone",       "empty_check": "str"},
    {"name": "primary_contact",       "empty_check": "str"},
    {"name": "primary_contact_email", "empty_check": "str"},
    {"name": "email",                 "empty_check": "str"},
    {"name": "services",              "empty_check": "dict"},
    {"name": "seo_mode",              "empty_check": "str",
     "required_if": "seo"},
    {"name": "business_profile_page_id", "empty_check": "str"},
]


# ── Client Info DB ──────────────────────────────────────────────────────────

# Per-client operational metadata. This is the daily-use layer: when a
# team member opens a client, they land here.
CLIENT_INFO_REQUIRED: list[dict] = [
    {"name": "Name",                  "empty_check": "title"},
    {"name": "Business Type",         "empty_check": "select"},
    {"name": "Vertical",              "empty_check": "rich_text"},
    {"name": "Website",               "empty_check": "url"},
    {"name": "Primary Contact Name",  "empty_check": "rich_text"},
    {"name": "Primary Contact Email", "empty_check": "email"},
    {"name": "Phone",                 "empty_check": "phone_number"},
    {"name": "Services",              "empty_check": "multi_select"},
    {"name": "Pipeline Stage",        "empty_check": "select"},
    {"name": "Account Manager",       "empty_check": "rich_text"},
]

CLIENT_INFO_NICE_TO_HAVE: list[dict] = [
    {"name": "Client Contacts",  "empty_check": "rich_text"},
    {"name": "Monthly Retainer", "empty_check": "number"},
    {"name": "Template",         "empty_check": "rich_text",
     "required_if": "website_build"},
    {"name": "Figma Desktop URL","empty_check": "url",
     "required_if": "website_build"},
    {"name": "Project Start",    "empty_check": "date",
     "required_if": "website_build"},
    {"name": "Timeline (Weeks)", "empty_check": "number",
     "required_if": "website_build"},
    {"name": "ClickUp Folder ID","empty_check": "rich_text"},
    {"name": "Stage Status",     "empty_check": "select"},
]


# ── Brand Guidelines DB ─────────────────────────────────────────────────────

# Per Keegan's directive 2026-04-24: all required, no service gating. Clients
# expand services over time; filling BG upfront means zero friction at each
# expansion. Self-heal adds any missing fields on first check run.
BRAND_GUIDELINES_REQUIRED: list[dict] = [
    # Voice / verbal
    {"name": "Voice & Tone",             "empty_check": "rich_text"},
    {"name": "Target Audience",          "empty_check": "rich_text"},   # self-healed if missing
    {"name": "Words to Avoid",           "empty_check": "rich_text"},
    {"name": "Power Words",              "empty_check": "rich_text"},
    {"name": "CTA Style",                "empty_check": "rich_text"},
    {"name": "Reading Level",            "empty_check": "rich_text"},
    {"name": "POV Notes",                "empty_check": "rich_text"},
    # Visual
    {"name": "Primary Color",            "empty_check": "rich_text"},
    {"name": "Secondary Color",          "empty_check": "rich_text"},
    {"name": "Accent Color",             "empty_check": "rich_text"},
    {"name": "Primary Font",             "empty_check": "rich_text"},
    {"name": "Secondary Font",           "empty_check": "rich_text"},
    {"name": "Photography Style",        "empty_check": "rich_text"},
    # Blog (YMYL E-E-A-T)
    {"name": "Blog Reviewer Name",       "empty_check": "rich_text"},
    {"name": "Blog Reviewer Credentials","empty_check": "rich_text"},
    {"name": "Blog Reviewer Bio",        "empty_check": "rich_text"},
]

BRAND_GUIDELINES_NICE_TO_HAVE: list[dict] = [
    {"name": "Tone Descriptors",  "empty_check": "rich_text"},
    {"name": "Blog Voice",        "empty_check": "rich_text"},
    {"name": "Image Direction",   "empty_check": "rich_text"},
    {"name": "Inspiration URLs",  "empty_check": "rich_text"},
    {"name": "Logo Assets",       "empty_check": "files"},
    {"name": "Raw Guidelines",    "empty_check": "rich_text"},
]


# ── Business Profile ────────────────────────────────────────────────────────
# Per-vertical required sections are in config/business_profile_requirements.py.
# The readiness check also runs a Layer 2 Claude content-completeness pass
# that evaluates whether each section contains the specific facts downstream
# needs (not just "is section non-empty").

# Per-vertical "specific facts" prompt hints for Layer 2. Claude reads the
# section content and answers whether these facts are present.
BUSINESS_PROFILE_FACTS_NEEDED: dict[str, dict[str, list[str]]] = {
    "addiction_treatment": {
        "Services Overview":
            ["specific services offered (detox, residential, PHP, IOP, outpatient, MAT, sober living)"],
        "Specialized Populations":
            ["specific populations served (men, women, veterans, LGBTQ+, dual-diagnosis, etc.)",
             "age range served"],
        "Substances Treated":
            ["specific substance list (alcohol, opioids, heroin, fentanyl, cocaine, meth, benzos, etc.)"],
        "Insurance & Payment":
            ["specific in-network carriers (BCBS, Aetna, Cigna, etc.)",
             "Medicaid / Medicare stance",
             "cash pay / private pay option"],
        "Levels of Care":
            ["which levels are offered", "which levels are NOT offered"],
        "Length of Stay":
            ["typical program duration", "program-tier-specific durations (PHP weeks, IOP weeks)"],
        "Medications":
            ["MAT philosophy and whether it's offered",
             "specific medications if MAT is offered (Suboxone, Vivitrol, Methadone, etc.)"],
        "Treatment Philosophy":
            ["core treatment approach (12-step, trauma-informed, holistic, etc.)"],
        "Company Credentials & Accreditations":
            ["accrediting bodies (Joint Commission, CARF, LegitScript) + state licensure"],
    },
    # Other verticals to add as we onboard clients
    "speech_pathology": {
        "Services Overview": ["specific services offered"],
        "Age Groups & Settings": ["age groups served", "settings (clinic, home, school, tele)"],
        "Treatment Areas":
            ["specific treatment areas (articulation, fluency, AAC, feeding, etc.)"],
    },
    "physical_therapy": {
        "Services Overview": ["specific services / modalities offered"],
        "Specialties":
            ["specific specialties (orthopedic, sports, pelvic, pediatric, etc.)"],
    },
    "mental_health": {
        "Therapy Modalities": ["specific modalities (CBT, DBT, EMDR, etc.)"],
        "Specialized Populations": ["populations specialized in"],
    },
    "dermatology": {
        "Medical vs Cosmetic": ["medical vs cosmetic split"],
        "Procedures Offered": ["specific procedures"],
    },
}


# ── Helpers ─────────────────────────────────────────────────────────────────

def _service_active(services: dict | list, service_key: str) -> bool:
    """Check if a service is active in the client's services config."""
    if isinstance(services, dict):
        s = services.get(service_key)
        if isinstance(s, dict):
            return bool(s.get("active"))
        return bool(s)
    if isinstance(services, list):
        return service_key in services
    return False


def _meets_required_if(required_if: str, services: dict | list, seo_mode: str) -> bool:
    """Evaluate whether a conditionally-required field is actually required."""
    if not required_if:
        return True
    if required_if == "seo":
        return _service_active(services, "seo")
    if required_if == "website_build":
        return _service_active(services, "website_build")
    if required_if == "local_or_hybrid_seo":
        if not _service_active(services, "seo"):
            return False
        return (seo_mode or "").lower() in ("local", "hybrid")
    if required_if == "blog":
        return _service_active(services, "blog")
    if required_if == "content":
        return (
            _service_active(services, "content")
            or _service_active(services, "blog")
            or _service_active(services, "social")
        )
    return True  # unknown condition → err on the side of required


def effective_required(
    specs: list[dict], services: dict | list, seo_mode: str,
) -> list[dict]:
    """Return only the specs that actually apply to this client based on
    service flags + SEO mode."""
    return [
        s for s in specs
        if _meets_required_if(s.get("required_if", ""), services, seo_mode)
    ]
