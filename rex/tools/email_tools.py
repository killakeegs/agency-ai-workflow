"""
Email tools for Rex.

Handles sending follow-up emails via Gmail API on behalf of Keegan.
Uses the same Google OAuth refresh token as GBP reviews.

Requires gmail.send scope — re-run scripts/setup/google_auth.py if
the current token doesn't include it.
"""
from __future__ import annotations

import base64
import os
from email.mime.text import MIMEText

import httpx


EMAIL_TOOL_NAMES = {
    "send_follow_up_email",
}

# Default sender — emails always come from Keegan
DEFAULT_SENDER = "keegan@rxmedia.io"


async def _get_access_token() -> str:
    """Exchange Gmail refresh token for a short-lived access token."""
    client_id     = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    # Use the separate Gmail token (keegan@rxmedia.io), fall back to shared token
    refresh_token = (
        os.environ.get("GOOGLE_GMAIL_REFRESH_TOKEN", "").strip()
        or os.environ.get("GOOGLE_REFRESH_TOKEN", "").strip()
    )

    if not all([client_id, client_secret, refresh_token]):
        raise ValueError(
            "Missing Google OAuth credentials for Gmail. Run: "
            "python3 scripts/setup/google_auth.py --gmail"
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


async def send_email(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    sender: str = DEFAULT_SENDER,
) -> dict:
    """
    Send an email via Gmail API.

    Args:
        to: recipient email address
        subject: email subject
        body: plain text email body
        cc: optional CC address (comma-separated for multiple)
        sender: sender email (default: keegan@rxmedia.io)

    Returns:
        dict with 'id' and 'threadId' from Gmail API, or 'error' on failure.
    """
    access_token = await _get_access_token()

    msg = MIMEText(body)
    msg["to"]      = to
    msg["from"]    = sender
    msg["subject"] = subject
    if cc:
        msg["cc"] = cc

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    async with httpx.AsyncClient() as http:
        r = await http.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers={
                "Authorization":  f"Bearer {access_token}",
                "Content-Type":   "application/json",
            },
            json={"raw": raw},
            timeout=15,
        )

    if r.status_code in (200, 201):
        data = r.json()
        return {"id": data.get("id", ""), "threadId": data.get("threadId", "")}
    else:
        return {"error": f"Gmail API error {r.status_code}: {r.text[:200]}"}


async def create_gmail_draft(
    to: str,
    subject: str,
    html_body: str,
    cc: str = "",
    sender: str = DEFAULT_SENDER,
) -> dict:
    """Create a Gmail draft with HTML formatting."""
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText as MT

    access_token = await _get_access_token()

    msg = MIMEMultipart("alternative")
    msg["to"] = to
    msg["from"] = sender
    msg["subject"] = subject
    if cc:
        msg["cc"] = cc

    # Plain text fallback
    import re
    plain = re.sub(r"<[^>]+>", "", html_body).strip()
    msg.attach(MT(plain, "plain"))
    msg.attach(MT(html_body, "html"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    async with httpx.AsyncClient() as http:
        r = await http.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/drafts",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={"message": {"raw": raw}},
            timeout=15,
        )

    if r.status_code in (200, 201):
        data = r.json()
        return {"id": data.get("id", ""), "status": "draft_created"}
    else:
        return {"error": f"Gmail API error {r.status_code}: {r.text[:200]}"}


async def execute_email_tool(name: str, tool_input: dict) -> str:
    """Dispatch an email tool call."""
    if name == "send_follow_up_email":
        to      = tool_input.get("to", "")
        subject = tool_input.get("subject", "")
        body    = tool_input.get("body", "")
        cc      = tool_input.get("cc", "")

        if not to or not subject or not body:
            return "Missing required fields: to, subject, body"

        result = await send_email(to=to, subject=subject, body=body, cc=cc)

        if "error" in result:
            return f"Failed to send email: {result['error']}"

        return f"Email sent successfully to {to}. Gmail message ID: {result['id']}"

    return f"Unknown email tool: {name}"
