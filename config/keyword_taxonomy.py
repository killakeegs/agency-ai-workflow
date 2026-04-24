"""
keyword_taxonomy.py — Per-vertical terminology bundles for keyword discovery.

Used by scripts/seo/discover_keywords.py to give Claude a structured menu
of services, terminologies, populations, substances, insurance modifiers,
etc. that exist in each vertical. Claude maps the client's Business
Profile facts against this taxonomy and generates realistic search
keywords covering every bucket.

Why this file exists separately from keyword_strategy.py:
  - keyword_strategy.py drives SITEMAP primary/secondary keyword
    assignment per page_kind (one keyword per page).
  - keyword_taxonomy.py drives INITIAL KEYWORD POOL generation for the
    client (dozens to hundreds of candidates, cross-bucket coverage).
  - They share terminology but serve different stages of the pipeline.

Structure per vertical:
  - services: discrete service offerings / levels of care the practice
    might provide. Used for service × geo keyword generation.
  - terminologies: synonymous framings of the whole vertical. Keywords
    for "rehab south carolina" vs "addiction treatment south carolina"
    target the same intent but different searcher vocabularies.
  - populations: specialized populations the practice might serve.
    Used for population × service × geo keyword generation.
  - substances: specific substances / conditions (addiction-specific).
    Used for substance × geo keyword generation.
  - program_lengths: common duration framings searched for.
  - insurance_patterns: how searchers phrase insurance + service combos.

Claude is instructed to IGNORE buckets the client doesn't offer (per
their BP). These are a menu to map against, not a mandate.
"""
from __future__ import annotations


KEYWORD_TAXONOMY: dict[str, dict] = {
    "addiction_treatment": {
        "services": [
            "detox", "medical detox", "drug detox", "alcohol detox",
            "residential", "residential treatment", "inpatient rehab",
            "PHP", "partial hospitalization", "partial hospitalization program",
            "IOP", "intensive outpatient", "intensive outpatient program",
            "outpatient", "outpatient rehab", "outpatient treatment",
            "sober living", "sober living home",
            "MAT", "medication assisted treatment",
            "aftercare", "alumni program",
        ],
        "terminologies": [
            "rehab", "rehab center", "rehab centers",
            "addiction treatment", "addiction treatment center",
            "substance abuse treatment", "substance abuse center",
            "recovery center", "recovery program",
            "drug and alcohol treatment", "drug and alcohol rehab",
            "substance use disorder treatment", "SUD treatment",
            "treatment center",
        ],
        "populations": [
            "men", "mens", "men's",
            "women", "womens", "women's",
            "veterans", "military",
            "first responders", "law enforcement", "healthcare professionals",
            "LGBTQ", "LGBTQ+", "LGBTQ friendly",
            "dual diagnosis", "co-occurring",
            "young adults", "adolescents", "teens",
            "executives", "professionals",
            "court ordered", "court mandated",
            "pregnant women", "mothers",
            "seniors", "older adults",
            "faith based", "christian",
        ],
        "substances": [
            "alcohol", "alcoholism",
            "opioid", "opioids", "opiate", "opiates",
            "heroin", "fentanyl",
            "prescription drug", "prescription painkiller",
            "cocaine", "crack",
            "meth", "methamphetamine", "crystal meth",
            "benzo", "benzos", "benzodiazepine", "xanax",
            "marijuana", "cannabis",
            "polysubstance", "poly drug",
        ],
        "program_lengths": [
            "30 day", "60 day", "90 day",
            "short term", "long term",
            "extended care",
        ],
        "insurance_patterns": [
            # phrasings that treat insurance as the qualifier
            "rehabs that take {INS}",
            "{INS} rehab",
            "{INS} addiction treatment",
            "{INS} inpatient rehab",
            "{INS} outpatient rehab",
            "does {INS} cover rehab",
            "{INS} covered rehab",
        ],
    },
    # Other verticals to add as we onboard clients
    "speech_pathology": {
        "services": [
            "speech therapy", "speech-language therapy",
            "feeding therapy", "swallowing therapy",
            "articulation therapy", "fluency therapy",
            "stuttering therapy", "voice therapy",
            "AAC", "augmentative communication",
            "language therapy",
            "social communication therapy",
        ],
        "terminologies": [
            "speech therapist", "SLP", "speech language pathologist",
            "speech therapy clinic", "speech pathology",
        ],
        "populations": [
            "pediatric", "children", "kids", "toddlers",
            "adult", "adults", "geriatric",
            "autism", "autistic", "on the spectrum",
            "apraxia", "dyslexia",
            "bilingual", "spanish speaking",
        ],
        "substances": [],
        "program_lengths": [],
        "insurance_patterns": [
            "{INS} speech therapy",
            "does {INS} cover speech therapy",
        ],
    },
    "occupational_therapy": {
        "services": [
            "occupational therapy", "sensory integration therapy",
            "fine motor therapy", "hand therapy",
            "sensory processing therapy",
            "ADL training", "self care therapy",
        ],
        "terminologies": [
            "occupational therapist", "OT", "pediatric OT",
            "occupational therapy clinic",
        ],
        "populations": [
            "pediatric", "children", "toddlers",
            "adult", "geriatric",
            "autism", "sensory processing disorder", "SPD",
            "ADHD",
        ],
        "substances": [],
        "program_lengths": [],
        "insurance_patterns": [
            "{INS} occupational therapy",
            "does {INS} cover OT",
        ],
    },
    "physical_therapy": {
        "services": [
            "physical therapy", "manual therapy",
            "dry needling", "sports rehabilitation",
            "post surgical rehab", "prehab",
            "orthopedic physical therapy", "vestibular therapy",
            "pelvic floor therapy",
        ],
        "terminologies": [
            "physical therapist", "PT", "physiotherapy",
            "physical therapy clinic", "sports medicine clinic",
        ],
        "populations": [
            "pediatric", "adult", "geriatric",
            "athlete", "sports injury", "post surgical",
            "workers comp", "auto accident",
        ],
        "substances": [],
        "program_lengths": [],
        "insurance_patterns": [
            "{INS} physical therapy",
            "does {INS} cover PT",
        ],
    },
    "mental_health": {
        "services": [
            "therapy", "psychotherapy", "counseling",
            "CBT", "DBT", "EMDR",
            "group therapy", "couples therapy", "family therapy",
            "psychiatry", "medication management",
        ],
        "terminologies": [
            "therapist", "counselor", "psychologist", "psychiatrist",
            "mental health counselor", "therapy practice", "counseling center",
        ],
        "populations": [
            "adult", "adolescent", "teen", "children",
            "couples", "family",
            "LGBTQ", "veterans", "first responders",
            "trauma", "PTSD",
            "anxiety", "depression", "grief",
        ],
        "substances": [],
        "program_lengths": [],
        "insurance_patterns": [
            "therapists that take {INS}",
            "{INS} therapist",
            "does {INS} cover therapy",
        ],
    },
    "dermatology": {
        "services": [
            "dermatology", "skin exam", "mole check",
            "acne treatment", "eczema treatment", "psoriasis treatment",
            "skin cancer screening", "mohs surgery",
            "botox", "fillers", "laser treatment", "chemical peel",
            "microneedling", "CoolSculpting",
        ],
        "terminologies": [
            "dermatologist", "skin doctor", "dermatology clinic",
            "medical spa", "med spa",
        ],
        "populations": [
            "pediatric", "adult",
            "teen acne",
        ],
        "substances": [],
        "program_lengths": [],
        "insurance_patterns": [
            "{INS} dermatologist",
        ],
    },
}


def taxonomy_for(verticals: list[str]) -> dict:
    """Merge taxonomy entries across a multi-vertical client. Lists dedupe
    in-order; empty keys stay empty.
    """
    merged = {
        "services": [], "terminologies": [], "populations": [],
        "substances": [], "program_lengths": [], "insurance_patterns": [],
    }
    for v in verticals or []:
        entry = KEYWORD_TAXONOMY.get(v, {})
        for k, lst in entry.items():
            if k not in merged:
                continue
            for item in lst:
                if item not in merged[k]:
                    merged[k].append(item)
    return merged
