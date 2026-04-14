#!/usr/bin/env python3
"""
auth_google_drive.py — One-time Google Drive OAuth authorization

Run this once to authorize access to your Google Drive.
It will open a browser window, you log in and click Allow,
and a token is saved to ~/.google/gdrive-token.json.

That token is then used by migrate_from_gdrive.py automatically.

Usage:
    python scripts/auth_google_drive.py
"""
import json
import sys
from pathlib import Path

CREDENTIALS_PATH = Path.home() / ".google" / "credentials.json"
TOKEN_PATH = Path.home() / ".google" / "gdrive-token.json"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def main():
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
    except ImportError:
        print("ERROR: Google auth libraries not installed.")
        print("Run: pip install google-auth-oauthlib google-auth-httplib2 google-api-python-client")
        sys.exit(1)

    creds = None

    # Load existing token if available
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    # Refresh or re-authorize if needed
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("Refreshing existing token...")
            creds.refresh(Request())
        else:
            if not CREDENTIALS_PATH.exists():
                print(f"ERROR: Credentials file not found at {CREDENTIALS_PATH}")
                sys.exit(1)
            print("Opening browser for Google authorization...")
            print("Log in with your Google account and click Allow.\n")
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
            creds = flow.run_local_server(port=0)

        # Save token for future use
        TOKEN_PATH.write_text(creds.to_json())
        print(f"\nToken saved to {TOKEN_PATH}")

    print("\nGoogle Drive authorization successful!")
    print(f"Token stored at: {TOKEN_PATH}")
    print("\nYou can now run: python scripts/migrate_from_gdrive.py")


if __name__ == "__main__":
    main()
