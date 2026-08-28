"""
Unit tests for AngelOneClient's on-disk session persistence: a successful
login() caches jwtToken/refreshToken/feedToken to a file, and on the next
process a call to restore_session() reuses that file if it's there,
verifies it with a real getProfile call, and falls back to a fresh login()
if the saved session is missing, corrupt, for a different account, or
rejected by the API.

Fully offline: AngelOneClient.session.request is replaced with a fake below
(login() and request() both route every HTTP call through it now that
AngelOneClient._send() paces/retries against SmartAPI's rate limits - see
client.py and tests/test_client_rate_limiting.py), so this never touches
the network or needs real credentials. Client-side rate-limit pacing is
disabled here via ANGELONE_RATE_LIMIT_DISABLED so these tests stay fast and
deterministic - that behavior has its own dedicated test file.

Run:
    python -m pytest tests/test_session_persistence.py -v
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from angelone_mcp.client import AngelOneClient, _RateLimiter  # noqa: E402


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def _login_payload(jwt="jwt-token", refresh="refresh-token", feed="feed-token"):
    return {"status": True, "data": {"jwtToken": jwt, "refreshToken": refresh, "feedToken": feed}}


def _profile_ok_payload(client_code="C123"):
    return {"status": True, "data": {"clientcode": client_code, "name": "Test User"}}


def _dispatch_by_url(routes: dict):
    """Build a fake `session.request(method, url, **kwargs)` that returns a
    canned response based on which SmartAPI path the url targets. `routes`
    maps a substring of the URL to either a response/exception factory (a
    zero-arg callable) or a plain FakeResponse. Every real call in this
    file - login, get_profile, logout - goes through session.request now
    (see AngelOneClient._send()), so one dispatcher covers all of them."""

    def _fake(method, url, **kwargs):
        for substring, response_or_factory in routes.items():
            if substring in url:
                return response_or_factory() if callable(response_or_factory) else response_or_factory
        raise AssertionError(f"test didn't expect a call to this URL: {url}")

    return _fake


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    """Build an AngelOneClient wired to an isolated, per-test session file,
    without running __init__ (which would try a real network call to detect
    the public IP). All the plumbing __init__ would normally set up is
    filled in by hand with harmless local values instead.

    Rate-limit pacing is disabled by default (ANGELONE_RATE_LIMIT_DISABLED)
    since these tests are about session persistence, not pacing - a real
    _RateLimiter is still attached (AngelOneClient._send() unconditionally
    calls self._rate_limiter.wait(), which becomes a no-op while pacing is
    disabled) so nothing breaks."""

    def _make(client_code="C123", session_file=None, persist=None, rate_limit_disabled=True):
        monkeypatch.setenv("ANGELONE_API_KEY", "test-key")
        monkeypatch.setenv("ANGELONE_CLIENT_CODE", client_code)
        monkeypatch.setenv("ANGELONE_PIN", "1234")
        monkeypatch.setenv("ANGELONE_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
        if session_file is None:
            session_file = str(tmp_path / f"session-{client_code}.json")
        monkeypatch.setenv("ANGELONE_SESSION_FILE", session_file)
        if persist is None:
            monkeypatch.delenv("ANGELONE_SESSION_PERSIST", raising=False)
        else:
            monkeypatch.setenv("ANGELONE_SESSION_PERSIST", persist)
        monkeypatch.setenv("ANGELONE_RATE_LIMIT_DISABLED", "true" if rate_limit_disabled else "false")

        import requests

        client = AngelOneClient.__new__(AngelOneClient)
        client.api_key = "test-key"
        client.client_code = client_code
        client.pin = "1234"
        client.totp_secret = "JBSWY3DPEHPK3PXP"
        client.jwt_token = None
        client.refresh_token = None
        client.feed_token = None
        client.session = requests.Session()
        client._rate_limiter = _RateLimiter()
        client._client_local_ip = "127.0.0.1"
        client._client_public_ip = "127.0.0.1"
        client._client_mac = "00:00:00:00:00:00"
        return client

    return _make


# --------------------------------------------------------------------- #
# login() persists a session
# --------------------------------------------------------------------- #

def test_login_writes_session_file(make_client):
    client = make_client()
    client.session.request = _dispatch_by_url({
        "loginByPassword": lambda: FakeResponse(_login_payload()),
    })

    client.login()

    path = Path(os.environ["ANGELONE_SESSION_FILE"])
    assert path.exists()
    saved = json.loads(path.read_text())
    assert saved["client_code"] == "C123"
    assert saved["jwt_token"] == "jwt-token"
    assert saved["refresh_token"] == "refresh-token"
    assert saved["feed_token"] == "feed-token"


def test_login_does_not_persist_when_disabled(make_client):
    client = make_client(persist="false")
    client.session.request = _dispatch_by_url({
        "loginByPassword": lambda: FakeResponse(_login_payload()),
    })

    client.login()

    assert not Path(os.environ["ANGELONE_SESSION_FILE"]).exists()


def test_logout_deletes_session_file(make_client):
    client = make_client()
    client.session.request = _dispatch_by_url({
        "loginByPassword": lambda: FakeResponse(_login_payload()),
        "logout": lambda: FakeResponse({"status": True, "data": {}}),
    })
    client.login()
    path = Path(os.environ["ANGELONE_SESSION_FILE"])
    assert path.exists()

    client.logout()

    assert not path.exists()


# --------------------------------------------------------------------- #
# restore_session()
# --------------------------------------------------------------------- #

def test_restore_session_with_no_saved_file_does_not_log_in(make_client):
    client = make_client()
    calls = {"n": 0}

    def fake_request(method, url, **kw):
        calls["n"] += 1
        return FakeResponse(_login_payload())

    client.session.request = fake_request

    result = client.restore_session()

    assert result["source"] == "no_saved_session"
    assert client.jwt_token is None
    assert calls["n"] == 0  # stayed lazy - no network calls at all


def test_restore_session_reuses_valid_saved_session(make_client):
    client = make_client()
    Path(os.environ["ANGELONE_SESSION_FILE"]).write_text(json.dumps({
        "client_code": "C123",
        "jwt_token": "saved-jwt",
        "refresh_token": "saved-refresh",
        "feed_token": "saved-feed",
    }))

    login_calls = []
    profile_calls = []

    def fake_request(method, url, **kw):
        if "loginByPassword" in url:
            login_calls.append(1)
            return FakeResponse(_login_payload("fresh-jwt"))
        profile_calls.append((method, url))
        return FakeResponse(_profile_ok_payload())

    client.session.request = fake_request

    result = client.restore_session()

    assert result["source"] == "restored_session"
    assert client.jwt_token == "saved-jwt"  # untouched - no fresh login happened
    assert len(profile_calls) == 1  # getProfile was used to verify it
    assert len(login_calls) == 0  # login() never called


def test_restore_session_falls_back_to_login_on_non_auth_api_error(make_client):
    """A saved session that get_profile rejects for a reason request()
    doesn't treat as auth-failure (so its own single-retry-and-relogin path
    doesn't kick in) must still fall back to a fresh login via
    restore_session()'s own except-and-retry."""
    client = make_client()
    Path(os.environ["ANGELONE_SESSION_FILE"]).write_text(json.dumps({
        "client_code": "C123",
        "jwt_token": "stale-jwt",
        "refresh_token": "stale-refresh",
        "feed_token": "stale-feed",
    }))

    client.session.request = _dispatch_by_url({
        "loginByPassword": lambda: FakeResponse(
            _login_payload("fresh-jwt", "fresh-refresh", "fresh-feed")
        ),
        "getProfile": lambda: FakeResponse(
            {"status": False, "message": "Something went wrong", "errorcode": "AB9999"}
        ),
    })

    result = client.restore_session()

    assert result["source"] == "fresh_login_after_invalid_saved_session"
    assert client.jwt_token == "fresh-jwt"
    # the fresh login must have overwritten the stale session on disk too
    saved = json.loads(Path(os.environ["ANGELONE_SESSION_FILE"]).read_text())
    assert saved["jwt_token"] == "fresh-jwt"


def test_restore_session_falls_back_to_login_on_expired_token_401(make_client):
    """A saved session that's genuinely expired (401/TokenException) is
    handled by request()'s existing single-retry-with-relogin logic, which
    restore_session() rides on top of."""
    client = make_client()
    Path(os.environ["ANGELONE_SESSION_FILE"]).write_text(json.dumps({
        "client_code": "C123",
        "jwt_token": "expired-jwt",
        "refresh_token": "expired-refresh",
        "feed_token": "expired-feed",
    }))

    profile_call_count = {"n": 0}

    def fake_request(method, url, **kw):
        if "loginByPassword" in url:
            return FakeResponse(_login_payload("fresh-jwt"))
        profile_call_count["n"] += 1
        if profile_call_count["n"] == 1:
            return FakeResponse({"status": False, "errorcode": "AG8001", "message": "expired"}, status_code=403)
        return FakeResponse(_profile_ok_payload())

    client.session.request = fake_request

    result = client.restore_session()

    assert result["source"] == "restored_session"  # get_profile ultimately succeeded (after its own internal relogin)
    assert client.jwt_token == "fresh-jwt"
    assert profile_call_count["n"] == 2  # first attempt (403) + retry after auto-relogin


def test_restore_session_ignores_session_file_from_a_different_account(make_client):
    client = make_client(client_code="C123")
    Path(os.environ["ANGELONE_SESSION_FILE"]).write_text(json.dumps({
        "client_code": "SOME-OTHER-ACCOUNT",
        "jwt_token": "not-mine",
        "refresh_token": "not-mine",
        "feed_token": "not-mine",
    }))
    client.session.request = _dispatch_by_url({
        "loginByPassword": lambda: FakeResponse(_login_payload("fresh-jwt")),
        "getProfile": lambda: FakeResponse(_profile_ok_payload()),
    })

    result = client.restore_session()

    assert result["source"] == "no_saved_session"
    assert client.jwt_token is None  # never adopted the other account's token


def test_restore_session_ignores_corrupt_session_file(make_client):
    client = make_client()
    Path(os.environ["ANGELONE_SESSION_FILE"]).write_text("{not valid json")
    client.session.request = _dispatch_by_url({
        "loginByPassword": lambda: FakeResponse(_login_payload("fresh-jwt")),
        "getProfile": lambda: FakeResponse(_profile_ok_payload()),
    })

    result = client.restore_session()

    assert result["source"] == "no_saved_session"
    assert client.jwt_token is None


def test_restore_session_disabled_by_env_var_ignores_saved_file(make_client):
    client = make_client(persist="false")
    Path(os.environ["ANGELONE_SESSION_FILE"]).write_text(json.dumps({
        "client_code": "C123",
        "jwt_token": "saved-jwt",
        "refresh_token": "saved-refresh",
        "feed_token": "saved-feed",
    }))
    client.session.request = _dispatch_by_url({
        "loginByPassword": lambda: FakeResponse(_login_payload("fresh-jwt")),
        "getProfile": lambda: FakeResponse(_profile_ok_payload()),
    })

    result = client.restore_session()

    assert result["source"] == "no_saved_session"
    assert client.jwt_token is None


# --------------------------------------------------------------------- #
# default session-file path
# --------------------------------------------------------------------- #

def test_default_session_file_path_is_stable_and_scoped_per_account(monkeypatch, tmp_path):
    monkeypatch.delenv("ANGELONE_SESSION_FILE", raising=False)
    monkeypatch.setenv("ANGELONE_SESSION_PERSIST", "true")

    client_a1 = AngelOneClient.__new__(AngelOneClient)
    client_a1.client_code = "ACCOUNT-A"
    client_a2 = AngelOneClient.__new__(AngelOneClient)
    client_a2.client_code = "ACCOUNT-A"
    client_b = AngelOneClient.__new__(AngelOneClient)
    client_b.client_code = "ACCOUNT-B"

    path_a1 = client_a1._session_file_path()
    path_a2 = client_a2._session_file_path()
    path_b = client_b._session_file_path()

    assert path_a1 == path_a2  # same account -> same path every time
    assert path_a1 != path_b  # different accounts never collide
    assert "ACCOUNT-A" not in str(path_a1)  # client code isn't leaked into the filename


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
