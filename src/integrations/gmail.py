"""
Gmail integration — shared client for backfill + real-time monitor.

Uses OAuth2 refresh token (keegan@rxmedia.io).
Auth: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_GMAIL_REFRESH_TOKEN in .env.
"""
from __future__ import annotations

import base64
import os
import re
from datetime import datetime, timedelta, timezone

import httpx

GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
GMAIL_REFRESH_TOKEN  = os.environ.get("GOOGLE_GMAIL_REFRESH_TOKEN", "").strip()
MY_ADDRESS           = os.environ.get("GMAIL_SENDER_ADDRESS", "keegan@rxmedia.io").strip().lower()

BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

DNL_MARKERS = ["[dnl]", "[do-not-log]", "internal only"]


async def get_access_token() -> str:
    async with httpx.AsyncClient() as http:
        r = await http.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id":     GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "refresh_token": GMAIL_REFRESH_TOKEN,
                "grant_type":    "refresh_token",
            },
            timeout=30.0,
        )
    r.raise_for_status()
    return r.json()["access_token"]


def extract_domain(email: str) -> str:
    m = re.search(r"@([\w\.-]+)", email or "")
    return m.group(1).lower() if m else ""


def build_search_query(emails: list[str], days: int) -> str:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y/%m/%d")
    domains: set[str] = set()
    for e in emails:
        d = extract_domain(e)
        if d and d not in ("gmail.com", "rxmedia.io", "google.com"):
            domains.add(d)

    if domains:
        clauses = " OR ".join(f"from:@{d} OR to:@{d} OR cc:@{d}" for d in domains)
    elif emails:
        clauses = " OR ".join(f"from:{e} OR to:{e}" for e in emails if "@" in e)
    else:
        return ""

    return f"({clauses}) after:{since}"


async def search_messages(
    http: httpx.AsyncClient, token: str, query: str, max_results: int = 1000
) -> list[str]:
    msg_ids: list[str] = []
    page_token: str | None = None

    while len(msg_ids) < max_results:
        params: dict = {"q": query, "maxResults": min(500, max_results - len(msg_ids))}
        if page_token:
            params["pageToken"] = page_token

        r = await http.get(
            f"{BASE}/messages", params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )
        r.raise_for_status()
        data = r.json()
        for m in data.get("messages", []):
            msg_ids.append(m["id"])
        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return msg_ids


async def get_message(
    http: httpx.AsyncClient, token: str, msg_id: str
) -> dict:
    r = await http.get(
        f"{BASE}/messages/{msg_id}",
        params={"format": "full"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json()


def parse_headers(message: dict) -> dict:
    return {
        h["name"].lower(): h["value"]
        for h in message.get("payload", {}).get("headers", [])
    }


def decode_body(payload: dict) -> str:
    mime = payload.get("mimeType", "")
    body = payload.get("body", {})

    if mime == "text/plain" and body.get("data"):
        try:
            return base64.urlsafe_b64decode(body["data"] + "==").decode("utf-8", errors="replace")
        except Exception:
            return ""

    for part in payload.get("parts", []) or []:
        text = decode_body(part)
        if text:
            return text
    return ""


def strip_quoted(text: str) -> str:
    for marker in ("\nOn ", "\n-----Original Message-----", "\n> ", "\n________________________________"):
        idx = text.find(marker)
        if idx > 100:
            text = text[:idx]
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def is_dnl(subject: str, body: str) -> bool:
    combined = (subject + " " + body[:200]).lower()
    return any(m in combined for m in DNL_MARKERS)


def is_automated_noise(subject: str, from_addr: str, body: str) -> bool:
    noise_from = [
        "noreply", "no-reply", "notification@", "notifications@",
        "support@calendly", "calendar-notification", "meet-notification",
        "invites@gmail", "mailer-daemon", "postmaster",
    ]
    if any(p in from_addr.lower() for p in noise_from):
        return True

    noise_subjects = [
        "unsubscribe", "newsletter", "receipt", "order confirmation",
        "your google account", "security alert", "sign-in", "password reset",
    ]
    if any(p in subject.lower() for p in noise_subjects):
        return True

    if len(body.strip()) < 50:
        return True

    return False


def group_threads(messages: list[dict]) -> dict[str, list[dict]]:
    threads: dict[str, list[dict]] = {}
    for m in messages:
        tid = m.get("threadId", m["id"])
        threads.setdefault(tid, []).append(m)
    for tid in threads:
        threads[tid].sort(key=lambda m: int(m.get("internalDate", 0)))
    return threads


def summarize_thread(thread: list[dict], max_chars: int = 4000) -> dict:
    first = thread[0]
    last  = thread[-1]
    first_headers = parse_headers(first)

    subject = first_headers.get("subject", "(no subject)")
    participants: set[str] = set()
    for m in thread:
        h = parse_headers(m)
        for field in ("from", "to", "cc"):
            for addr in re.findall(r"[\w\.-]+@[\w\.-]+", h.get(field, "")):
                participants.add(addr.lower())

    first_date = datetime.fromtimestamp(
        int(first.get("internalDate", 0)) / 1000, tz=timezone.utc
    ).strftime("%Y-%m-%d")
    last_date = datetime.fromtimestamp(
        int(last.get("internalDate", 0)) / 1000, tz=timezone.utc
    ).strftime("%Y-%m-%d")

    body_parts: list[str] = []
    running = 0
    for m in thread:
        h = parse_headers(m)
        sender = h.get("from", "")
        date = datetime.fromtimestamp(
            int(m.get("internalDate", 0)) / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d")
        body = strip_quoted(decode_body(m.get("payload", {})))
        snippet = f"\n--- {date} | {sender} ---\n{body}"
        if running + len(snippet) > max_chars:
            snippet = snippet[: max_chars - running] + "\n[truncated]"
            body_parts.append(snippet)
            break
        body_parts.append(snippet)
        running += len(snippet)

    last_from = parse_headers(last).get("from", "").lower()
    direction = "inbound" if MY_ADDRESS not in last_from else "outbound"

    return {
        "thread_id":     first.get("threadId"),
        "subject":       subject,
        "first_date":    first_date,
        "last_date":     last_date,
        "message_count": len(thread),
        "participants":  sorted(participants),
        "direction":     direction,
        "body":          "".join(body_parts),
    }
