"""One-time helper: authorize your Google account and print a refresh token.

Run this locally (not on the server/Space) once, after creating an OAuth
2.0 Client ID (type "Desktop app") in Google Cloud Console with the
Google Calendar API enabled:

    GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... python scripts/get_refresh_token.py

A browser window will open asking you to sign in and grant calendar
access. Afterwards, copy the printed GOOGLE_REFRESH_TOKEN value into your
.env file (local) or your Hugging Face Space secrets (deployed).

This script is only ever run by hand, on your own machine. It is not part
of the running application and never touches the server.
"""

from __future__ import annotations

import os
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/calendar"]


def main() -> None:
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        print(
            "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET environment variables "
            "before running this script.",
            file=sys.stderr,
        )
        sys.exit(1)

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    # Google only returns a refresh token on the first grant unless consent is forced.
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    if not credentials.refresh_token:
        print(
            "Google did not return a refresh token. Remove this app's access at "
            "https://myaccount.google.com/permissions and run the script again.",
            file=sys.stderr,
        )
        sys.exit(1)

    print("\nAuthorization complete. Add this to your .env / Space secrets:\n")
    print(f"GOOGLE_REFRESH_TOKEN={credentials.refresh_token}")


if __name__ == "__main__":
    main()
