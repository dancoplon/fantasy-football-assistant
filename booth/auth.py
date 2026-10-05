"""Yahoo OAuth 2.0: one-time authorization, token storage, and refresh.

Flow:
  1. `booth auth` prints/opens Yahoo's consent URL.
  2. After you approve, Yahoo redirects to https://localhost:8000/?code=...
     Nothing listens there, so the page fails to load. That's expected:
     copy the URL from the address bar and paste it into the terminal.
  3. The code is exchanged for an access token (valid 1 hour) and a refresh
     token, saved to YAHOO_TOKEN_FILE with owner-only permissions.
  4. Every job calls `get_session()`, which refreshes the access token first
     (per the PRD: refresh at job start) and hands yahoo_fantasy_api a session.

Client ID/Secret stay in .env; the token file holds tokens only.
"""

from __future__ import annotations

import json
import logging
import os
import time
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from booth.config import Settings

AUTHORIZE_URL = "https://api.login.yahoo.com/oauth2/request_auth"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"

# Yahoo access tokens last 3600s; treat them as stale a few minutes early.
TOKEN_TTL_SECONDS = 3600
REFRESH_MARGIN_SECONDS = 300


class AuthError(RuntimeError):
    """Raised when Booth can't get a valid Yahoo token. Jobs should alert, not skip silently."""


def authorization_url(settings: Settings) -> str:
    query = urlencode(
        {
            "client_id": settings.client_id,
            "redirect_uri": settings.redirect_uri,
            "response_type": "code",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def extract_code(pasted: str) -> str:
    """Accept either the full redirect URL or the bare code."""
    pasted = pasted.strip()
    if not pasted:
        raise AuthError("Nothing pasted.")
    if "://" in pasted or pasted.startswith("localhost"):
        query = parse_qs(urlparse(pasted if "://" in pasted else f"https://{pasted}").query)
        if "error" in query:
            raise AuthError(f"Yahoo returned an error: {query['error'][0]}")
        if "code" not in query:
            raise AuthError("That URL has no ?code= parameter. Copy the whole address bar.")
        return query["code"][0]
    return pasted


def _request_token(settings: Settings, data: dict) -> dict:
    data = {"redirect_uri": settings.redirect_uri, **data}
    try:
        resp = requests.post(
            TOKEN_URL,
            data=data,
            auth=(settings.client_id, settings.client_secret),
            timeout=30,
        )
    except requests.RequestException as exc:
        raise AuthError(f"Could not reach Yahoo's token endpoint: {exc}") from exc
    if resp.status_code != 200:
        # The body names the problem (invalid_grant, invalid_client, ...) and contains no secrets.
        raise AuthError(f"Yahoo token request failed ({resp.status_code}): {resp.text[:300]}")
    payload = resp.json()
    return {
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token"),
        "token_type": payload.get("token_type", "bearer"),
        "expires_in": int(payload.get("expires_in", TOKEN_TTL_SECONDS)),
        "token_time": time.time(),
        "xoauth_yahoo_guid": payload.get("xoauth_yahoo_guid"),
    }


def save_tokens(path: Path, tokens: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(tokens, f, indent=2)
    os.replace(tmp, path)


def load_tokens(path: Path) -> dict:
    if not path.exists():
        raise AuthError(f"No token file at {path}. Run `uv run booth auth` once to authorize.")
    with path.open() as f:
        return json.load(f)


def exchange_code(settings: Settings, code: str) -> dict:
    tokens = _request_token(settings, {"grant_type": "authorization_code", "code": code})
    if not tokens.get("refresh_token"):
        raise AuthError("Yahoo did not return a refresh token.")
    save_tokens(settings.token_file, tokens)
    return tokens


def refresh(settings: Settings, tokens: dict | None = None) -> dict:
    tokens = tokens or load_tokens(settings.token_file)
    if not tokens.get("refresh_token"):
        raise AuthError("Token file has no refresh token. Run `uv run booth auth` again.")
    new = _request_token(
        settings, {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}
    )
    # Yahoo normally returns the same refresh token; keep the old one if it doesn't send one.
    new["refresh_token"] = new.get("refresh_token") or tokens["refresh_token"]
    save_tokens(settings.token_file, new)
    return new


def is_fresh(tokens: dict, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    ttl = int(tokens.get("expires_in", TOKEN_TTL_SECONDS))
    return now - float(tokens.get("token_time", 0)) < ttl - REFRESH_MARGIN_SECONDS


def authorize_interactive(settings: Settings, open_browser: bool = True) -> dict:
    url = authorization_url(settings)
    print("Opening Yahoo's consent page. If it doesn't open, visit:\n")
    print(f"  {url}\n")
    if open_browser:
        webbrowser.open(url)
    print(
        "After you click Agree, the browser goes to https://localhost:8000/?code=...\n"
        "and shows a 'can't connect' error. That's expected.\n"
    )
    pasted = input("Paste the full URL from the address bar (or just the code): ")
    return exchange_code(settings, extract_code(pasted))


def get_session(settings: Settings, force_refresh: bool = True):
    """Return a yahoo_oauth.OAuth2 object ready for yahoo_fantasy_api.

    Refreshes on every call by default so each scheduled job starts with a full hour.
    """
    from yahoo_oauth import OAuth2

    logging.getLogger("yahoo_oauth").setLevel(logging.WARNING)

    tokens = load_tokens(settings.token_file)
    if force_refresh or not is_fresh(tokens):
        tokens = refresh(settings, tokens)

    # Passing tokens as kwargs (no from_file) keeps yahoo_oauth from writing its own
    # file, which would copy the client secret alongside the tokens.
    return OAuth2(
        settings.client_id,
        settings.client_secret,
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        token_type=tokens["token_type"],
        token_time=tokens["token_time"],
        callback_uri=settings.redirect_uri,
        store_file=False,
        browser_callback=False,
    )
