import json
import os
import stat
import time
from pathlib import Path
from unittest import mock

import pytest

from booth import auth
from booth.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        client_id="cid",
        client_secret="csecret",
        redirect_uri="https://localhost:8000",
        token_file=tmp_path / "secrets" / "yahoo_token.json",
        league_id="890283",
        game_code="nfl",
    )


def _resp(status: int, payload: dict):
    r = mock.Mock(status_code=status, text=json.dumps(payload))
    r.json.return_value = payload
    return r


def test_authorization_url_uses_registered_redirect(settings):
    url = auth.authorization_url(settings)
    assert url.startswith(auth.AUTHORIZE_URL)
    assert "redirect_uri=https%3A%2F%2Flocalhost%3A8000" in url
    assert "response_type=code" in url
    assert "csecret" not in url


@pytest.mark.parametrize(
    "pasted",
    [
        "https://localhost:8000/?code=abc123",
        "https://localhost:8000/?code=abc123&state=x",
        "localhost:8000/?code=abc123",
        "  abc123  ",
    ],
)
def test_extract_code(pasted):
    assert auth.extract_code(pasted) == "abc123"


def test_extract_code_reports_yahoo_error():
    with pytest.raises(auth.AuthError, match="access_denied"):
        auth.extract_code("https://localhost:8000/?error=access_denied")


def test_extract_code_requires_code_param():
    with pytest.raises(auth.AuthError):
        auth.extract_code("https://localhost:8000/")


def test_exchange_code_saves_tokens_privately_without_secret(settings):
    payload = {"access_token": "AT", "refresh_token": "RT", "token_type": "bearer", "expires_in": 3600}
    with mock.patch.object(auth.requests, "post", return_value=_resp(200, payload)) as post:
        auth.exchange_code(settings, "abc123")
    _, kwargs = post.call_args
    assert kwargs["auth"] == ("cid", "csecret")
    assert kwargs["data"]["grant_type"] == "authorization_code"
    assert kwargs["data"]["redirect_uri"] == "https://localhost:8000"

    saved = json.loads(settings.token_file.read_text())
    assert saved["access_token"] == "AT" and saved["refresh_token"] == "RT"
    assert "csecret" not in settings.token_file.read_text()
    assert stat.S_IMODE(os.stat(settings.token_file).st_mode) == 0o600


def test_refresh_keeps_old_refresh_token_when_omitted(settings):
    auth.save_tokens(settings.token_file, {"access_token": "old", "refresh_token": "RT", "token_time": 0})
    with mock.patch.object(auth.requests, "post", return_value=_resp(200, {"access_token": "new"})) as post:
        tokens = auth.refresh(settings)
    assert post.call_args.kwargs["data"]["grant_type"] == "refresh_token"
    assert tokens["access_token"] == "new"
    assert json.loads(settings.token_file.read_text())["refresh_token"] == "RT"


def test_refresh_failure_raises_auth_error(settings):
    auth.save_tokens(settings.token_file, {"access_token": "old", "refresh_token": "RT", "token_time": 0})
    with mock.patch.object(auth.requests, "post", return_value=_resp(400, {"error": "invalid_grant"})):
        with pytest.raises(auth.AuthError, match="invalid_grant"):
            auth.refresh(settings)


def test_missing_token_file_says_to_run_auth(settings):
    with pytest.raises(auth.AuthError, match="booth auth"):
        auth.load_tokens(settings.token_file)


def test_is_fresh():
    now = time.time()
    assert auth.is_fresh({"token_time": now - 60, "expires_in": 3600}, now)
    assert not auth.is_fresh({"token_time": now - 3400, "expires_in": 3600}, now)


def test_get_session_builds_oauth2_without_writing_secret(settings):
    auth.save_tokens(settings.token_file, {"access_token": "old", "refresh_token": "RT", "token_time": 0})
    payload = {"access_token": "AT2", "refresh_token": "RT", "token_type": "bearer", "expires_in": 3600}
    with mock.patch.object(auth.requests, "post", return_value=_resp(200, payload)):
        sc = auth.get_session(settings)
    assert sc.access_token == "AT2"
    assert sc.consumer_key == "cid"
    assert "csecret" not in settings.token_file.read_text()
    assert not (Path.cwd() / "secrets.json").exists()
