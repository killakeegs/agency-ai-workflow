#!/usr/bin/env python3
"""
google_auth.py — One-time OAuth flow for Google Business Profile API

Run this once to authorize rxmediamanager@gmail.com and save a refresh token.
The refresh token is stored in .env as GOOGLE_REFRESH_TOKEN and used by all
subsequent Business Profile API calls without requiring re-authorization.

Usage:
    python3 scripts/google_auth.py

What happens:
  1. Starts a local HTTP server on port 8080 to catch the OAuth redirect
  2. Opens a browser window to Google's OAuth consent screen
  3. You log in as rxmediamanager@gmail.com and approve access
  4. Browser redirects to localhost:8080 — script catches the auth code automatically
  5. Refresh token is written to .env automatically
"""
from __future__ import annotations

import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings

CLIENT_ID     = settings.google_client_id
CLIENT_SECRET = settings.google_client_secret

SCOPES = [
    "https://www.googleapis.com/auth/business.manage",
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
    if not CLIENT_ID or not CLIENT_SECRET:
        print("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set in .env")
        sys.exit(1)

    # Step 1 — Start local server to catch redirect
    server = HTTPServer(("localhost", PORT), _OAuthHandler)

    def _serve():
        server.handle_request()  # handle exactly one request then stop

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()

    # Step 2 — Build auth URL
    params = {
        "client_id":     CLIENT_ID,
        "redirect_uri":  REDIRECT_URI,
        "response_type": "code",
        "scope":         " ".join(SCOPES),
        "access_type":   "offline",
        "prompt":        "consent",  # force refresh token even if previously authorized
    }
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)

    print("\n" + "="*60)
    print("  Google Business Profile — OAuth Authorization")
    print("="*60)
    print(f"\nLocal redirect server listening on port {PORT}.")
    print("Opening browser. Log in as: rxmediamanager@gmail.com\n")

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
    env_path = Path(__file__).parent.parent / ".env"
    env_text = env_path.read_text()

    if "GOOGLE_REFRESH_TOKEN=" in env_text:
        lines = env_text.splitlines()
        new_lines = []
        for line in lines:
            if line.startswith("GOOGLE_REFRESH_TOKEN="):
                new_lines.append(f"GOOGLE_REFRESH_TOKEN={refresh_token}")
            else:
                new_lines.append(line)
        env_path.write_text("\n".join(new_lines) + "\n")
    else:
        env_path.write_text(env_text + f"\nGOOGLE_REFRESH_TOKEN={refresh_token}\n")

    print("✓ Saved to .env as GOOGLE_REFRESH_TOKEN")
    print("\nYou're set. The Business Profile API is now authorized.")
    print("Run 'make competitor-research CLIENT=x ENRICH=1' to pull GBP data.\n")


if __name__ == "__main__":
    main()
