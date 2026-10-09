#!/usr/bin/env python3
"""One-time Google Drive OAuth setup — RUN ON YOUR MAC, not in the container.

The container has no browser, so the OAuth dance happens here where your
browser lives. This opens Google sign-in, you approve read-only Drive access,
and the token is saved to data/drive_token.json for the container to use.

Usage:
    python3 scripts/drive_auth_local.py

Requires: pip install google-auth-oauthlib google-auth
Expects:  data/drive_client.json (OAuth client secrets from Google Cloud Console)
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SECRETS = REPO / "data" / "drive_client.json"
TOKEN = REPO / "data" / "drive_token.json"
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents.readonly",
]


def main() -> int:
    if not SECRETS.exists():
        print(f"Missing {SECRETS} — create the OAuth client in Google Cloud "
              "Console first (see chat instructions).", file=sys.stderr)
        return 1
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("pip install google-auth-oauthlib google-auth", file=sys.stderr)
        return 1

    flow = InstalledAppFlow.from_client_secrets_file(str(SECRETS), SCOPES)
    # Opens your browser; Google redirects back to localhost to deliver the code.
    creds = flow.run_local_server(port=0, prompt="consent")
    TOKEN.parent.mkdir(parents=True, exist_ok=True)
    TOKEN.write_text(creds.to_json())
    print(f"Done — token saved to {TOKEN}")
    print("The container will pick it up automatically via data/drive_token.json.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
