"""OAuth2 flow for QBO. Run as: python -m src.auth"""
from __future__ import annotations

import base64
import json
import os
import secrets
import sys
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
SCOPE = "com.intuit.quickbooks.accounting"

ROOT = Path(__file__).resolve().parent.parent
TOKENS_PATH = ROOT / "tokens.json"


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _exchange_code(code: str, redirect_uri: str) -> dict:
    client_id = os.environ["QBO_CLIENT_ID"]
    client_secret = os.environ["QBO_CLIENT_SECRET"]
    resp = requests.post(
        TOKEN_URL,
        headers={
            "Authorization": _basic_auth_header(client_id, client_secret),
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def refresh_access_token(refresh_token: str) -> dict:
    client_id = os.environ["QBO_CLIENT_ID"]
    client_secret = os.environ["QBO_CLIENT_SECRET"]
    resp = requests.post(
        TOKEN_URL,
        headers={
            "Authorization": _basic_auth_header(client_id, client_secret),
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def save_tokens(token_resp: dict, realm_id: str) -> None:
    now = int(time.time())
    payload = {
        "access_token": token_resp["access_token"],
        "refresh_token": token_resp["refresh_token"],
        "access_token_expires_at": now + int(token_resp.get("expires_in", 3600)),
        "refresh_token_expires_at": now + int(token_resp.get("x_refresh_token_expires_in", 8726400)),
        "realm_id": realm_id,
        "saved_at": now,
    }
    TOKENS_PATH.write_text(json.dumps(payload, indent=2))
    os.chmod(TOKENS_PATH, 0o600)


def load_tokens() -> dict:
    if not TOKENS_PATH.exists():
        raise FileNotFoundError(
            f"No tokens.json found at {TOKENS_PATH}. Run `python -m src.auth` first."
        )
    return json.loads(TOKENS_PATH.read_text())


def get_valid_access_token() -> tuple[str, str]:
    """Return (access_token, realm_id), refreshing if the access token is near expiry."""
    tokens = load_tokens()
    now = int(time.time())
    if tokens["access_token_expires_at"] - now < 60:
        new_tokens = refresh_access_token(tokens["refresh_token"])
        save_tokens(new_tokens, tokens["realm_id"])
        tokens = load_tokens()
    return tokens["access_token"], tokens["realm_id"]


class _CallbackHandler(BaseHTTPRequestHandler):
    expected_state: str = ""
    result: dict = {}

    def log_message(self, format, *args):  # silence default access log
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = urllib.parse.parse_qs(parsed.query)
        state = params.get("state", [""])[0]
        if state != _CallbackHandler.expected_state:
            print(
                f"\n[state mismatch]\n  received: {state}\n  expected: {_CallbackHandler.expected_state}\n"
                f"This usually means a stale browser tab from a previous run is firing against a fresh server.\n",
                file=sys.stderr,
            )
            self.send_response(400)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"State mismatch. Close this tab, kill the python process, and re-run python -m src.auth.")
            return
        if "error" in params:
            _CallbackHandler.result = {"error": params["error"][0]}
            body = b"Authorization failed. You can close this tab."
        else:
            _CallbackHandler.result = {
                "code": params["code"][0],
                "realm_id": params.get("realmId", [""])[0],
            }
            body = b"Authorization complete. You can close this tab."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body)


def run_oauth_flow() -> None:
    client_id = os.environ.get("QBO_CLIENT_ID")
    redirect_uri = os.environ.get("QBO_REDIRECT_URI", "http://localhost:8765/callback")
    if not client_id or not os.environ.get("QBO_CLIENT_SECRET"):
        print("Missing QBO_CLIENT_ID / QBO_CLIENT_SECRET. Fill in .env first.", file=sys.stderr)
        sys.exit(1)

    # The local callback server always binds to localhost; QBO_REDIRECT_URI is the
    # URI registered in Intuit (may be a public bouncer like GitHub Pages that forwards
    # to localhost). QBO_LOCAL_CALLBACK_PORT controls the local bind.
    parsed = urllib.parse.urlparse(redirect_uri)
    if parsed.hostname in ("localhost", "127.0.0.1"):
        local_port = parsed.port or 8765
    else:
        local_port = int(os.environ.get("QBO_LOCAL_CALLBACK_PORT", "8765"))
    host = "localhost"
    port = local_port

    # Encode the local port in `state` so a public bouncer page can read it and forward
    # the callback to the right localhost port without hardcoding.
    state = f"{local_port}_{secrets.token_urlsafe(24)}"
    _CallbackHandler.expected_state = state
    _CallbackHandler.result = {}

    auth_params = {
        "client_id": client_id,
        "response_type": "code",
        "scope": SCOPE,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    auth_url = f"{AUTH_URL}?{urllib.parse.urlencode(auth_params)}"
    print(f"Opening browser to authorize natu-qbo...\nIf it does not open, visit:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    server = HTTPServer((host, port), _CallbackHandler)
    while not _CallbackHandler.result:
        server.handle_request()

    if "error" in _CallbackHandler.result:
        print(f"OAuth error: {_CallbackHandler.result['error']}", file=sys.stderr)
        sys.exit(1)

    code = _CallbackHandler.result["code"]
    realm_id = _CallbackHandler.result["realm_id"]
    if not realm_id:
        print("No realmId in callback; cannot proceed.", file=sys.stderr)
        sys.exit(1)

    token_resp = _exchange_code(code, redirect_uri)
    save_tokens(token_resp, realm_id)
    print(f"Authorized. realm_id={realm_id}")
    print(f"Tokens saved to {TOKENS_PATH}")


if __name__ == "__main__":
    run_oauth_flow()
