#!/usr/bin/env python3
"""
google_auth.py — One-time OAuth flow for Google APIs

Supports two modes:
  1. Default: authorize rxmediamanager@gmail.com for GBP, GSC, GA4
  2. --gmail: authorize keegan@rxmedia.io for Gmail sending only

Refresh tokens are stored in .env:
  - GOOGLE_REFRESH_TOKEN — rxmediamanager (GBP, GSC, GA4)
  - GOOGLE_GMAIL_REFRESH_TOKEN — keegan@rxmedia.io (Gmail send)

Usage:
    python3 scripts/setup/google_auth.py            # GBP + GSC + GA4 (rxmediamanager)
    python3 scripts/setup/google_auth.py --gmail     # Gmail send (keegan@rxmedia.io)
"""
from __future__ import annotations

import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.config import settings

CLIENT_ID     = settings.google_client_id
CLIENT_SECRET = settings.google_client_secret

SCOPES_DEFAULT = [
    "https://www.googleapis.com/auth/business.manage",       # GBP performance data
    "https://www.googleapis.com/auth/webmasters.readonly",   # Google Search Console
    "https://www.googleapis.com/auth/analytics.readonly",    # Google Analytics 4
]

SCOPES_GMAIL = [
    "https://www.googleapis.com/auth/gmail.send",            # Send emails via Gmail API
    "https://www.googleapis.com/auth/gmail.readonly",        # Read emails (for monitoring)
    "https://www.googleapis.com/auth/gmail.compose",         # Create drafts (meeting processor follow-up emails)
    "https://www.googleapis.com/auth/drive.readonly",        # Read Google Drive folders (migrate_client.py)
    "https://www.googleapis.com/auth/documents.readonly",    # Read Google Docs content
    "https://www.googleapis.com/auth/calendar.readonly",     # Read calendar events (meeting prep)
]

PORT = 8080
REDIRECT_URI = f"http://localhost:{PORT}"

# Shared state between HTTP handler and main thread
_auth_code: list[str] = []
_server_error: list[str] = []


class _OAuthHandler(BaseHTTPRequestHandler):
    """Catches the redirect from Google and extracts the auth code."""

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if "error" in params:
            _server_error.append(params["error"][0])
            self._respond("Authorization failed: " + params["error"][0])
        elif "code" in params:
            _auth_code.append(params["code"][0])
            self._respond(
                "Authorization successful! You can close this tab and return to the terminal."
            )
        else:
            self._respond("Unexpected response — no code or error found.")

    def _respond(self, message: str) -> None:
        body = f"<html><body><h2>{message}</h2></body></html>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # silence server logs
        pass


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Google OAuth setup")
    parser.add_argument("--gmail", action="store_true",
                        help="Authorize Gmail send/read for keegan@rxmedia.io (separate token)")
    args = parser.parse_args()

    gmail_mode = args.gmail

    if not CLIENT_ID or not CLIENT_SECRET:
        print("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set in .env")
        sys.exit(1)

    scopes    = SCOPES_GMAIL if gmail_mode else SCOPES_DEFAULT
    env_key   = "GOOGLE_GMAIL_REFRESH_TOKEN" if gmail_mode else "GOOGLE_REFRESH_TOKEN"
    login_as  = "keegan@rxmedia.io" if gmail_mode else "rxmediamanager@gmail.com"
    label     = "Gmail (send + read)" if gmail_mode else "GBP + Search Console + Analytics"

    # Step 1 — Start local server to catch redirect
    server = HTTPServer(("localhost", PORT), _OAuthHandler)

    def _serve():
        server.handle_request()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()

    # Step 2 — Build auth URL
    auth_params = {
        "client_id":     CLIENT_ID,
        "redirect_uri":  REDIRECT_URI,
        "response_type": "code",
        "scope":         " ".join(scopes),
        "access_type":   "offline",
        "prompt":        "consent",
    }
    if gmail_mode:
        auth_params["login_hint"] = login_as
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(auth_params)

    print("\n" + "="*60)
    print(f"  Google APIs — OAuth Authorization")
    print(f"  ({label})")
    print("="*60)
    print(f"\nLocal redirect server listening on port {PORT}.")
    print(f"Opening browser. Log in as: {login_as}")
    print(f"Approve ALL scopes shown\n")

    webbrowser.open(auth_url)

    print("If the browser didn't open, visit this URL manually:")
    print(f"\n  {auth_url}\n")
    print("Waiting for redirect from Google...")

    thread.join(timeout=120)
    server.server_close()

    if _server_error:
        print(f"\nAuthorization error: {_server_error[0]}")
        sys.exit(1)

    if not _auth_code:
        print("\nTimed out waiting for authorization. Try again.")
        sys.exit(1)

    code = _auth_code[0]
    print(f"\n✓ Auth code received.")

    # Step 3 — Exchange code for tokens
    resp = httpx.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code":          code,
            "redirect_uri":  REDIRECT_URI,
            "grant_type":    "authorization_code",
        },
    )

    if resp.status_code != 200:
        print(f"\nError exchanging code: {resp.status_code}")
        print(resp.text)
        sys.exit(1)

    tokens = resp.json()
    refresh_token = tokens.get("refresh_token", "")

    if not refresh_token:
        print("\nNo refresh token returned. Try revoking access at:")
        print("  https://myaccount.google.com/permissions")
        print("Then re-run this script.")
        sys.exit(1)

    print(f"✓ Refresh token obtained: {refresh_token[:20]}...")

    # Step 4 — Write to .env
    env_path = Path(__file__).parent.parent.parent / ".env"
    env_text = env_path.read_text()

    if f"{env_key}=" in env_text:
        lines = env_text.splitlines()
        new_lines = []
        for line in lines:
            if line.startswith(f"{env_key}="):
                new_lines.append(f"{env_key}={refresh_token}")
            else:
                new_lines.append(line)
        env_path.write_text("\n".join(new_lines) + "\n")
    else:
        env_path.write_text(env_text.rstrip("\n") + f"\n{env_key}={refresh_token}\n")

    print(f"✓ Saved to .env as {env_key}")
    if gmail_mode:
        print(f"\nGmail API authorized for {login_as}.")
        print("Rex can now send follow-up emails on Keegan's behalf.\n")
    else:
        print("\nGoogle APIs authorized. GBP, GSC, and GA4 are ready.")
        print("Run 'make competitor-research CLIENT=x ENRICH=1' to pull GBP data.\n")


if __name__ == "__main__":
    main()
