"""
business_profile_requirements.py — Per-vertical "required sections for SEO" checklist.

When `populate_business_profile` runs, it compares the populated Business
Profile against this checklist and flags any required section that's empty
or thin. Gaps land in two places:
  1. An inline "🚨 Information Gaps" callout at the top of the Business
     Profile page (so the team sees them every time they open it)
  2. The workspace Flags DB (so Rex / the oncall team see them at the
     agency level, dedup'd against prior runs)

Section names must match the H2 headings created by
`scripts/onboarding/setup_notion.py::_build_business_profile_blocks`
exactly — case and punctuation. If they drift apart, the gap detector
will false-flag every section.

Edit this file when:
  - We learn that a specific section is load-bearing for keyword
    discovery and should be required (add to UNIVERSAL_REQUIRED or
    a vertical entry).
  - We onboard a new vertical and its template lives in VERTICAL_SECTIONS.
  - A section is renamed in setup_notion.py (rename it here too).
"""
from __future__ import annotations


# Required across every vertical — these sections drive keyword strategy for
# any service business. Empty = gap.
UNIVERSAL_REQUIRED: list[str] = [
    "Services Overview",
    "Specialized Populations",
    "Insurance & Payment",
    "Facility & Environment",
]

# Vertical-specific required sections (APPENDED to UNIVERSAL_REQUIRED).
# Keep this tight — only sections that materially change the keyword
# strategy should be here. "Nice to have" content lives outside the gate.
VERTICAL_REQUIRED: dict[str, list[str]] = {
    "addiction_treatment": [
        "Levels of Care",
        "Treatment Philosophy",
        "Substances Treated",
    ],
    "speech_pathology": [
        "Age Groups & Settings",
        "Treatment Areas",
    ],
    "occupational_therapy": [
        "Age Groups & Settings",
        "Specialties",
    ],
    "physical_therapy": [
        "Specialties",
    ],
    "dermatology": [
        "Medical vs Cosmetic",
        "Procedures Offered",
    ],
    "mental_health": [
        "Therapy Modalities",
        "Specialized Populations",  # mental_health has its own vertical-level populations section
    ],
}

# Minimum content length (characters) for a section to be considered
# "populated". A single-line stub shouldn't pass gap detection.
MIN_SECTION_CHARS = 80


def required_sections_for(verticals: list[str]) -> list[str]:
    """Return the full list of section names required for this client's
    vertical(s). Multi-vertical clients get the union."""
    out = list(UNIVERSAL_REQUIRED)
    seen = set(out)
    for v in verticals or []:
        for name in VERTICAL_REQUIRED.get(v, []):
            if name not in seen:
                out.append(name)
                seen.add(name)
    return out
