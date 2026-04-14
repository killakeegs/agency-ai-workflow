#!/usr/bin/env python3
"""
check_env.py — Validate all environment variables before running the pipeline.

Run this when onboarding a new team member or after updating .env to confirm
all required keys are present and correctly formatted.

Usage:
    python3 scripts/setup/check_env.py
    python3 scripts/setup/check_env.py --service seo      # check SEO keys only
    python3 scripts/setup/check_env.py --service blog     # check blog/Webflow keys only
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Load .env before checking
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent.parent / ".env")


# ── Key groups ────────────────────────────────────────────────────────────────

CORE = {
    "ANTHROPIC_API_KEY":             ("required", "sk-ant-",  "Claude API — get from console.anthropic.com"),
    "NOTION_API_KEY":                ("required", ("secret_", "ntn_"), "Notion integration — notion.so/my-integrations"),
    "NOTION_WORKSPACE_ROOT_PAGE_ID": ("required", None,       "Parent page ID for all client pages"),
    "CLICKUP_API_KEY":               ("required", "pk_",      "ClickUp API — app.clickup.com/settings/apps"),
    "CLICKUP_WORKSPACE_ID":          ("required", None,       "ClickUp workspace ID (in URL)"),
    "CLICKUP_DEFAULT_SPACE_ID":      ("optional", None,       "Default space for new client folders"),
}

SEO = {
    "GOOGLE_API_KEY":                ("optional", None, "PageSpeed Insights + Places API"),
    "GOOGLE_CLIENT_ID":              ("optional", None, "OAuth client — needed for GSC/GA4/GBP reporting"),
    "GOOGLE_CLIENT_SECRET":          ("optional", None, "OAuth client secret"),
    "GOOGLE_REFRESH_TOKEN":          ("optional", None, "Set by: python3 scripts/setup/google_auth.py"),
    "DATAFORSEO_LOGIN":              ("optional", None, "keegan@rxmedia.io — DataForSEO account"),
    "DATAFORSEO_PASSWORD":           ("optional", None, "DataForSEO API password (not account password)"),
    "SEARCH_ATLAS_API_KEY":          ("optional", None, "Search Atlas rank tracker"),
}

IMAGES = {
    "REPLICATE_API_KEY":             ("optional", "r8_",  "AI image generation — replicate.com/account/api-tokens"),
    "PEXELS_API_KEY":                ("optional", None,   "Stock photography — pexels.com/api"),
    "UNSPLASH_ACCESS_KEY":           ("optional", None,   "Stock photography fallback — unsplash.com/developers"),
}

PUBLISHING = {
    "WEBFLOW_API_TOKEN":             ("optional", None,   "Webflow site-level token — for blog + CMS publish"),
    "SLACK_BOT_TOKEN":               ("optional", "xoxb-","Rex Slack bot — api.slack.com/apps"),
    "SLACK_SIGNING_SECRET":          ("optional", None,   "Rex Slack signing secret"),
}

ALL_GROUPS = {
    "Core (required for all pipeline stages)": CORE,
    "SEO pipeline":                             SEO,
    "Image generation":                         IMAGES,
    "Publishing (Webflow, Slack)":              PUBLISHING,
}

SERVICE_FILTER = {
    "core":       [CORE],
    "seo":        [CORE, SEO],
    "images":     [CORE, IMAGES],
    "blog":       [CORE, PUBLISHING],
    "publishing": [CORE, PUBLISHING],
}


# ── Check logic ───────────────────────────────────────────────────────────────

def _check_key(name: str, required: str, prefix, description: str) -> tuple[str, str]:
    """
    Returns (status, message).
    status: "ok" | "warn" | "error"
    """
    value = os.environ.get(name, "").strip()

    if not value:
        if required == "required":
            return "error", f"MISSING — {description}"
        else:
            return "warn", f"not set (optional) — {description}"

    if prefix:
        prefixes = (prefix,) if isinstance(prefix, str) else prefix
        if not any(value.startswith(p) for p in prefixes):
            expected = " or ".join(f"'{p}'" for p in prefixes)
            return "error", f"wrong format — expected {expected}, got '{value[:12]}...'"

    # Mask value for display
    masked = value[:6] + "..." if len(value) > 6 else value
    return "ok", f"set ({masked})"


def run_checks(groups: dict) -> bool:
    """Run checks for the given groups. Returns True if no errors."""
    icons = {"ok": "✓", "warn": "–", "error": "✗"}
    colors = {
        "ok":    "\033[32m",  # green
        "warn":  "\033[33m",  # yellow
        "error": "\033[31m",  # red
        "reset": "\033[0m",
    }

    any_errors = False

    for group_name, keys in groups.items():
        print(f"\n  {group_name}")
        print(f"  {'─' * len(group_name)}")
        for name, (required, prefix, description) in keys.items():
            status, msg = _check_key(name, required, prefix, description)
            if status == "error":
                any_errors = True
            icon  = icons[status]
            color = colors[status]
            reset = colors["reset"]
            print(f"  {color}{icon}{reset} {name:<38} {msg}")

    return not any_errors


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Validate .env variables for the agency pipeline")
    parser.add_argument(
        "--service",
        choices=list(SERVICE_FILTER.keys()),
        default=None,
        help="Check keys for a specific service only",
    )
    args = parser.parse_args()

    print("\n" + "=" * 60)
    print("  RxMedia Agency Pipeline — Environment Check")
    print("=" * 60)

    if args.service:
        groups_to_check = {}
        for group_dict in SERVICE_FILTER[args.service]:
            # Find group name
            for gname, gdict in ALL_GROUPS.items():
                if gdict is group_dict:
                    groups_to_check[gname] = gdict
        print(f"  Checking: {args.service} keys only\n")
    else:
        groups_to_check = ALL_GROUPS

    ok = run_checks(groups_to_check)

    print()
    if ok:
        print("  ✓ All checks passed — pipeline is ready to run.\n")
        sys.exit(0)
    else:
        print("  ✗ Some required variables are missing. Fix them in .env and re-run.\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
