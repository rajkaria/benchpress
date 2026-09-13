"""One-shot Gmail OAuth for the scratch account, using the Desktop client's loopback redirect.

    uv run python scripts/gmail_oauth.py

Reads GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET from .env, opens the consent URL, catches the
redirect on http://127.0.0.1:8765/, exchanges the code, and writes GMAIL_REFRESH_TOKEN (and
GMAIL_ADDRESS) into .env. Scope: https://mail.google.com/ (needed for messages.import and
batchDelete, which seeding and reset use). No Playground, no web client required.
"""

from __future__ import annotations

import http.server
import os
import re
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

import httpx
from dotenv import load_dotenv

PORT = 8765
REDIRECT = f"http://127.0.0.1:{PORT}/"
SCOPE = "https://mail.google.com/"
_code: dict[str, str] = {}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" in query and query.get("state", [""])[0] == _code.get("state"):
            _code["code"] = query["code"][0]
            body = b"Benchpress: Gmail authorized. You can close this tab."
        else:
            body = b"Benchpress: missing or mismatched code; re-run the script."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - http.server API
        return


def _write_env(path: Path, key: str, value: str) -> None:
    text = path.read_text() if path.exists() else ""
    if re.search(rf"^{key}=", text, flags=re.M):
        text = re.sub(rf"^{key}=.*$", f"{key}={value}", text, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n{key}={value}\n"
    path.write_text(text)


def main() -> int:
    env_path = Path.cwd() / ".env"
    load_dotenv(env_path)
    client_id = os.environ.get("GMAIL_CLIENT_ID", "")
    client_secret = os.environ.get("GMAIL_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        print("GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET missing in .env", file=sys.stderr)
        return 2
    _code["state"] = secrets.token_urlsafe(16)
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": _code["state"],
    }
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    server = http.server.HTTPServer(("127.0.0.1", PORT), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print("Open this URL, sign in as the scratch Gmail account, and accept")
    print("(choose 'Continue' on the unverified-app screen):\n")
    print(url + "\n")
    webbrowser.open(url)
    print(f"Waiting for the redirect on {REDIRECT} ...")
    while "code" not in _code:
        threading.Event().wait(0.5)
    server.shutdown()
    token = httpx.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": _code["code"],
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": REDIRECT,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    payload = token.json()
    refresh = payload.get("refresh_token")
    if token.status_code != 200 or not refresh:
        print(f"token exchange failed: {token.status_code} {token.text[:300]}", file=sys.stderr)
        return 1
    profile = httpx.get(
        "https://gmail.googleapis.com/gmail/v1/users/me/profile",
        headers={"Authorization": f"Bearer {payload['access_token']}"},
        timeout=30,
    ).json()
    address = str(profile.get("emailAddress", os.environ.get("GMAIL_ADDRESS", "")))
    _write_env(env_path, "GMAIL_REFRESH_TOKEN", str(refresh))
    if address:
        _write_env(env_path, "GMAIL_ADDRESS", address)
    print(f"ok: refresh token saved to .env for {address}; messages in mailbox: {profile.get('messagesTotal')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
