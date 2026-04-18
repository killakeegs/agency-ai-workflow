"""
Google Calendar integration — read-only access to keegan@rxmedia.io calendar.

Uses the same OAuth refresh token as Gmail (GOOGLE_GMAIL_REFRESH_TOKEN).
Requires calendar.readonly scope (re-run scripts/setup/google_auth.py --gmail
after adding the scope).
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

import httpx

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
REFRESH_TOKEN = os.environ.get("GOOGLE_GMAIL_REFRESH_TOKEN", "").strip()

BASE = "https://www.googleapis.com/calendar/v3"


async def get_access_token() -> str:
    async with httpx.AsyncClient() as http:
        r = await http.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "refresh_token": REFRESH_TOKEN,
                "grant_type": "refresh_token",
            },
            timeout=30.0,
        )
    r.raise_for_status()
    return r.json()["access_token"]


async def list_events_today(
    http: httpx.AsyncClient,
    token: str,
    calendar_id: str = "primary",
    lookback_hours: int = 0,
    lookahead_hours: int = 24,
) -> list[dict]:
    """List calendar events in a window around now."""
    now = datetime.now(timezone.utc)
    time_min = (now - timedelta(hours=lookback_hours)).isoformat()
    time_max = (now + timedelta(hours=lookahead_hours)).isoformat()

    r = await http.get(
        f"{BASE}/calendars/{calendar_id}/events",
        params={
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": 50,
        },
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json().get("items", [])


def extract_attendee_emails(event: dict) -> list[str]:
    emails = []
    for a in event.get("attendees", []) or []:
        e = (a.get("email") or "").lower()
        if e:
            emails.append(e)
    # Also include organizer
    org = (event.get("organizer", {}).get("email") or "").lower()
    if org and org not in emails:
        emails.append(org)
    return emails


def extract_event_datetime(event: dict) -> tuple[datetime | None, str]:
    """Return (start datetime UTC, formatted time string)."""
    start = event.get("start", {})
    dt_str = start.get("dateTime") or start.get("date")
    if not dt_str:
        return None, ""
    try:
        if "T" in dt_str:
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(dt_str + "T00:00:00+00:00")
    except ValueError:
        return None, ""
    # Local time formatting (assume PDT/PST)
    local_dt = dt.astimezone()
    time_str = local_dt.strftime("%I:%M %p").lstrip("0").lower()
    return dt, time_str


RXMEDIA_DOMAIN = "rxmedia.io"
INTERNAL_TITLE_KEYWORDS = ["standup", "team meeting", "internal", "rxmedia team", "strategy session", "synch", "weekly synch", "team synch"]
ONBOARDING_KEYWORDS = ["onboarding", "kickoff", "kick-off", "kick off"]
SALES_KEYWORDS = ["discovery", "intro call", "intro ", "sales", "proposal", "demo", "prospect"]

# Personal/non-work keywords — skip prep entirely
PERSONAL_KEYWORDS = [
    "birthday", "bday", "wedding", "marriage", "yoga", "meditation", "class",
    "wine", "cheese", "golf", "lunch", "dinner", "brunch", "drinks", "party",
    "license", "doctor", "dentist", "appointment", "haircut", "gym",
    "date night", "anniversary", "flight",
]


def _match_client_by_title(title: str, clients: dict) -> str | None:
    """Match a meeting to a client by looking for the client name in the title."""
    title_lower = title.lower()
    # Sort by name length DESC so longest match wins (avoid "Crown" matching "Crown Behavioral Health" incorrectly)
    candidates = [(key, cfg) for key, cfg in clients.items() if not cfg.get("internal")]
    candidates.sort(key=lambda x: -len(x[1].get("name", "")))
    for key, cfg in candidates:
        name = cfg.get("name", "").lower()
        if not name or len(name) < 4:
            continue
        # Exact phrase match (handles "Summit Therapy", "Twin River Berries", etc.)
        if name in title_lower:
            return key
        # Also try matching slugified key (e.g., "tru_living_recovery" → "tru living recovery")
        slug = key.replace("_", " ")
        if len(slug) >= 8 and slug in title_lower:
            return key
    return None


def classify_meeting(
    event: dict,
    clients: dict,
) -> tuple[str, str | None]:
    """Classify a calendar event.

    Returns: (type, client_key or None)
      type is one of: "personal", "internal", "client_recurring", "onboarding", "sales", "unknown"
    """
    title = (event.get("summary") or "")
    title_lower = title.lower()
    attendees = extract_attendee_emails(event)

    external_attendees = [e for e in attendees if not e.endswith(f"@{RXMEDIA_DOMAIN}")]

    # Personal: title matches personal keywords (birthday, wedding, yoga, etc.)
    if any(kw in title_lower for kw in PERSONAL_KEYWORDS):
        return "personal", None

    # Internal: only RxMedia attendees OR title matches internal keywords with no external
    if not external_attendees:
        return "internal", None
    if any(kw in title_lower for kw in INTERNAL_TITLE_KEYWORDS) and not external_attendees:
        return "internal", None

    # Match to known client — try title first, then attendees
    client_key = _match_client_by_title(title, clients)
    if not client_key:
        client_key = _match_client_by_attendees(attendees, clients)

    # Onboarding: title contains onboarding/kickoff
    if any(kw in title_lower for kw in ONBOARDING_KEYWORDS):
        return "onboarding", client_key

    # Client match → recurring client meeting
    if client_key:
        return "client_recurring", client_key

    # Sales: title matches sales keywords
    if any(kw in title_lower for kw in SALES_KEYWORDS):
        return "sales", None

    # External attendees but no client match and no sales signal → unknown
    # (safer default — "unknown" triggers minimal prep instead of treating personal stuff as sales)
    return "unknown", None


def _match_client_by_attendees(attendees: list[str], clients: dict) -> str | None:
    """Match meeting attendees to a client registry entry."""
    for email in attendees:
        if "@" not in email:
            continue
        domain = email.split("@")[-1]
        email_lower = email.lower()
        for key, cfg in clients.items():
            if cfg.get("internal"):
                continue
            # Direct email match
            for field in ("email", "primary_contact_email"):
                if (cfg.get(field) or "").lower() == email_lower:
                    return key
            # Domain match (skip common domains)
            if domain in ("gmail.com", "yahoo.com", "hotmail.com", "outlook.com"):
                continue
            for field in ("email", "primary_contact_email"):
                cfg_email = (cfg.get(field) or "").lower()
                if cfg_email and cfg_email.endswith(f"@{domain}"):
                    return key
    return None
