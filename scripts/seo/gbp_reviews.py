#!/usr/bin/env python3
"""
gbp_reviews.py — Respond to Google Business Profile reviews

Fetches new/unanswered reviews via the Google Business Profile API.
- Positive (4-5 stars): generates a personalized response from a template
  library + brand voice, posts automatically.
- Negative (1-2 stars): flags for team review — creates a ClickUp task
  assigned to Keegan, NEVER auto-posts.
- Neutral (3 stars): drafts a response, flags for team approval before posting.

Usage:
    make gbp-reviews CLIENT=summit_therapy
    make gbp-reviews CLIENT=summit_therapy --dry-run   # preview without posting

Prerequisites:
    - gbp_location_id must be set in clients config (e.g. "locations/1234567890")
    - GOOGLE_REFRESH_TOKEN set in .env (run scripts/setup/google_auth.py)
    - CLICKUP_API_KEY set in .env (for flagging negative reviews)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import anthropic
import httpx

from config.clients import CLIENTS
from src.config import settings
from src.integrations.notion import NotionClient

CLIENTS_JSON_PATH = Path(__file__).parent.parent.parent / "config" / "clients.json"

# Keegan's ClickUp user ID — negative reviews always assigned to him
KEEGAN_CLICKUP_ID = 3852174


# ── Google OAuth helpers ───────────────────────────────────────────────────────

async def _get_access_token() -> str:
    """Exchange refresh token for a short-lived access token."""
    client_id     = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN", "").strip()

    if not all([client_id, client_secret, refresh_token]):
        raise ValueError(
            "Missing Google OAuth credentials. Set GOOGLE_CLIENT_ID, "
            "GOOGLE_CLIENT_SECRET, and GOOGLE_REFRESH_TOKEN in .env"
        )

    async with httpx.AsyncClient() as http:
        r = await http.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id":     client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type":    "refresh_token",
            },
        )
    r.raise_for_status()
    return r.json()["access_token"]


# ── GBP API helpers ────────────────────────────────────────────────────────────

async def _fetch_reviews(location_id: str, access_token: str) -> list[dict]:
    """
    Fetch unanswered reviews for a GBP location.
    location_id format: "locations/1234567890"
    """
    url = f"https://mybusiness.googleapis.com/v4/{location_id}/reviews"
    async with httpx.AsyncClient() as http:
        r = await http.get(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            params={"pageSize": 50},
            timeout=15,
        )

    if r.status_code == 404:
        raise ValueError(
            f"GBP location '{location_id}' not found. "
            "Check gbp_location_id in client config."
        )
    if r.status_code == 403:
        raise ValueError(
            "GBP API access denied. Make sure the Google account has access "
            "to this location and GOOGLE_REFRESH_TOKEN is current."
        )
    r.raise_for_status()

    all_reviews = r.json().get("reviews", [])
    # Filter to unanswered only
    return [rev for rev in all_reviews if not rev.get("reviewReply")]


async def _post_reply(location_id: str, review_name: str, reply_text: str, access_token: str) -> None:
    """Post a reply to a GBP review."""
    url = f"https://mybusiness.googleapis.com/v4/{review_name}/reply"
    async with httpx.AsyncClient() as http:
        r = await http.put(
            url,
            headers={
                "Authorization":  f"Bearer {access_token}",
                "Content-Type":   "application/json",
            },
            json={"comment": reply_text},
            timeout=15,
        )
    r.raise_for_status()


# ── Response generation ────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You write Google Business Profile review responses for healthcare practices.

RULES — no exceptions:
- No em dashes (—). Use commas or periods.
- No generic filler: "We appreciate your feedback", "We strive for excellence",
  "Thank you for taking the time"
- No AI phrases: "It means the world to us", "We're thrilled", "We're so glad"
- Sound like a real person, not a corporate PR department
- Use the reviewer's first name if available
- Keep it short: 2-4 sentences for positive, 3-5 for neutral
- Match the practice's brand voice exactly
- For positive reviews: acknowledge what they mentioned specifically + warm close
- For neutral reviews: acknowledge, show you care, invite them to reach out directly

Never write responses for negative reviews — those are flagged for team review only.
"""

def _build_response_prompt(
    review: dict,
    brand: dict,
    response_type: str,
) -> str:
    reviewer   = review.get("reviewer", {}).get("displayName", "")
    first_name = reviewer.split()[0] if reviewer else ""
    rating     = review.get("starRating", "")
    comment    = review.get("comment", "(no comment left)")
    business   = brand.get("business_name", "the practice")
    voice      = brand.get("voice", "") or brand.get("tone_desc", "")

    prompt = f"""\
Write a {response_type} Google Business Profile response for {business}.

Reviewer: {first_name or "the reviewer"}
Star rating: {rating}
Their review: "{comment}"

Brand voice: {voice or "warm, professional, approachable"}

Write a single response — no preamble, no explanation. Just the reply text.
"""
    return prompt


async def _generate_response(
    review: dict,
    brand: dict,
    response_type: str,
    ai_client: anthropic.Anthropic,
) -> str:
    prompt   = _build_response_prompt(review, brand, response_type)
    response = ai_client.messages.create(
        model=settings.anthropic_model,
        max_tokens=300,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


# ── ClickUp flagging ───────────────────────────────────────────────────────────

async def _flag_in_clickup(
    review: dict,
    client_name: str,
    cfg: dict,
    flag_type: str,  # "negative" or "neutral"
) -> str | None:
    """Create a ClickUp task for a review that needs team attention."""
    api_key = os.environ.get("CLICKUP_API_KEY", "").strip()
    list_id = cfg.get("clickup_review_list_id", "")

    if not api_key or not list_id:
        return None

    reviewer = review.get("reviewer", {}).get("displayName", "Unknown")
    rating   = review.get("starRating", "")
    comment  = review.get("comment", "(no comment)")[:300]
    stars    = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}.get(rating, "?")

    if flag_type == "negative":
        task_name = f"⚠️ Negative GBP Review — {client_name} — {reviewer} ({stars}★)"
        desc = (
            f"A negative review needs a personal response from Keegan.\n\n"
            f"Reviewer: {reviewer}\n"
            f"Rating: {stars} stars\n\n"
            f"Review:\n{comment}\n\n"
            f"Do NOT use a template — write a genuine, personal response."
        )
    else:
        task_name = f"GBP Review Response — {client_name} — {reviewer} ({stars}★) — needs approval"
        desc = (
            f"A neutral review response has been drafted. Please review before posting.\n\n"
            f"Reviewer: {reviewer}\n"
            f"Rating: {stars} stars\n\n"
            f"Review:\n{comment}"
        )

    async with httpx.AsyncClient() as http:
        r = await http.post(
            f"https://api.clickup.com/api/v2/list/{list_id}/task",
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            json={
                "name":        task_name,
                "description": desc,
                "assignees":   [KEEGAN_CLICKUP_ID],
            },
            timeout=15,
        )

    if r.status_code in (200, 201):
        return r.json().get("url", "")
    return None


# ── Data loaders ───────────────────────────────────────────────────────────────

async def _load_brand(notion: NotionClient, cfg: dict) -> dict:
    brand: dict = {}
    bg_db = cfg.get("brand_guidelines_db_id", "")
    if bg_db:
        rows = await notion._client.request(
            path=f"databases/{bg_db}/query", method="POST", body={"page_size": 1}
        )
        if rows.get("results"):
            props = rows["results"][0].get("properties", {})
            def _rt(p): return "".join(x.get("text", {}).get("content", "") for x in p.get("rich_text", []))
            brand["voice"]     = _rt(props.get("Voice & Tone", {}))
            brand["tone_desc"] = _rt(props.get("Tone of Voice", {}))

    brand["business_name"] = cfg.get("name", "")
    return brand


# ── Star rating helpers ────────────────────────────────────────────────────────

def _stars(rating_str: str) -> int:
    return {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}.get(rating_str, 0)

def _sentiment(stars: int) -> str:
    if stars >= 4:  return "positive"
    if stars == 3:  return "neutral"
    return "negative"


# ── Main ───────────────────────────────────────────────────────────────────────

async def run(client_key: str, dry_run: bool) -> None:
    cfg = CLIENTS.get(client_key)
    if not cfg:
        print(f"Client '{client_key}' not found.")
        sys.exit(1)

    location_id = cfg.get("gbp_location_id", "")
    if not location_id:
        print(f"No gbp_location_id set for {client_key}. Run: make seo-activate CLIENT={client_key} GBP_ID=...")
        sys.exit(1)

    client_name = cfg.get("name", client_key)
    notion      = NotionClient(api_key=settings.notion_api_key)
    ai_client   = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    print(f"\n{'='*60}")
    print(f"  GBP Review Responder — {client_name}")
    if dry_run:
        print(f"  DRY RUN — no responses will be posted")
    print(f"{'='*60}\n")

    print("Authenticating with Google...")
    access_token = await _get_access_token()

    print("Fetching unanswered reviews...")
    reviews = await _fetch_reviews(location_id, access_token)
    print(f"  Found {len(reviews)} unanswered review(s)\n")

    if not reviews:
        print("✓ No unanswered reviews — nothing to do.")
        return

    print("Loading brand guidelines...")
    brand = await _load_brand(notion, cfg)

    positive_count = neutral_count = negative_count = 0

    for review in reviews:
        reviewer = review.get("reviewer", {}).get("displayName", "Unknown")
        rating   = review.get("starRating", "")
        stars    = _stars(rating)
        comment  = review.get("comment", "(no comment)")[:100]
        sentiment = _sentiment(stars)

        print(f"Review: {reviewer} — {stars}★ — \"{comment}\"")

        if sentiment == "negative":
            negative_count += 1
            print(f"  → NEGATIVE — flagging for Keegan, NOT auto-responding")
            if not dry_run:
                task_url = await _flag_in_clickup(review, client_name, cfg, "negative")
                if task_url:
                    print(f"  → ClickUp task created: {task_url}")
                else:
                    print(f"  → ⚠ ClickUp task creation failed — check clickup_review_list_id in config")

        elif sentiment == "neutral":
            neutral_count += 1
            print(f"  → NEUTRAL — drafting response for team approval...")
            reply = await _generate_response(review, brand, "neutral", ai_client)
            print(f"  Draft: {reply[:120]}...")
            if not dry_run:
                task_url = await _flag_in_clickup(review, client_name, cfg, "neutral")
                if task_url:
                    print(f"  → ClickUp task created with draft: {task_url}")

        else:  # positive
            positive_count += 1
            print(f"  → POSITIVE — generating response...")
            reply = await _generate_response(review, brand, "positive", ai_client)
            print(f"  Reply: {reply[:120]}...")
            if not dry_run:
                await _post_reply(location_id, review["name"], reply, access_token)
                print(f"  → Posted to GBP")
            else:
                print(f"  → [DRY RUN] Would post to GBP")

        print()

    print(f"{'─'*60}")
    print(f"  Positive: {positive_count} responded {'(dry run)' if dry_run else 'automatically'}")
    print(f"  Neutral:  {neutral_count} drafted, flagged for team approval")
    print(f"  Negative: {negative_count} flagged for Keegan — no auto-response")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Respond to GBP reviews")
    parser.add_argument("--client",  required=True)
    parser.add_argument("--dry-run", action="store_true", help="Preview without posting")
    args = parser.parse_args()
    asyncio.run(run(args.client, args.dry_run))


if __name__ == "__main__":
    main()
