"""
Unit tests for AngelOneClient's client-side rate limiting: every HTTP call
is paced against SmartAPI's documented per-endpoint limits (ROUTE_MIN_INTERVAL
in client.py), and a call that gets rate-limited anyway (HTTP 403/429,
"Access denied because of exceeding access rate") is retried with backoff
instead of being surfaced immediately or mistaken for an auth failure.

Fully offline: AngelOneClient.session.request is replaced with a fake, so
this never touches the network or needs real credentials. To keep this fast
and deterministic, tests either use a temporary route with a tiny
ROUTE_MIN_INTERVAL, or monkeypatch the backoff base down to a few
milliseconds - never sleep for anything close to the real ~1.2s/0.4s/etc.
production intervals.

Run:
    python -m pytest tests/test_client_rate_limiting.py -v
"""

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import angelone_mcp.client as client_module  # noqa: E402
from angelone_mcp.client import (  # noqa: E402
    AngelOneAPIError,
    AngelOneClient,
    ROUTES,
    _RateLimiter,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def _ok_payload():
    return {"status": True, "data": {}}


def _rate_limited_payload():
    return {"status": False, "message": "Access denied because of exceeding access rate", "errorcode": "AB1004"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ANGELONE_API_KEY", "test-key")
    monkeypatch.setenv("ANGELONE_CLIENT_CODE", "C123")
    monkeypatch.setenv("ANGELONE_PIN", "1234")
    monkeypatch.setenv("ANGELONE_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
    monkeypatch.delenv("ANGELONE_RATE_LIMIT_DISABLED", raising=False)

    import requests

    c = AngelOneClient.__new__(AngelOneClient)
    c.api_key = "test-key"
    c.client_code = "C123"
    c.pin = "1234"
    c.totp_secret = "JBSWY3DPEHPK3PXP"
    c.jwt_token = "already-authenticated"  # skip the lazy login() in request()
    c.refresh_token = None
    c.feed_token = None
    c.session = requests.Session()
    c._rate_limiter = _RateLimiter()
    c._client_local_ip = "127.0.0.1"
    c._client_public_ip = "127.0.0.1"
    c._client_mac = "00:00:00:00:00:00"
    return c


# --------------------------------------------------------------------- #
# _RateLimiter itself
# --------------------------------------------------------------------- #

def test_rate_limiter_paces_repeat_calls_to_the_same_route(monkeypatch):
    monkeypatch.setitem(client_module.ROUTE_MIN_INTERVAL, "_test_route", 0.2)
    limiter = _RateLimiter()

    start = time.monotonic()
    limiter.wait("_test_route")
    first_elapsed = time.monotonic() - start
    limiter.wait("_test_route")
    second_elapsed = time.monotonic() - start

    assert first_elapsed < 0.05, "the first call to a route should never be delayed"
    assert second_elapsed >= 0.18, "a second call made immediately after should be paced to ~the minimum interval"


def test_rate_limiter_does_not_pace_different_routes_against_each_other(monkeypatch):
    monkeypatch.setitem(client_module.ROUTE_MIN_INTERVAL, "_test_route_a", 0.3)
    monkeypatch.setitem(client_module.ROUTE_MIN_INTERVAL, "_test_route_b", 0.3)
    limiter = _RateLimiter()

    start = time.monotonic()
    limiter.wait("_test_route_a")
    limiter.wait("_test_route_b")
    elapsed = time.monotonic() - start

    assert elapsed < 0.05, "distinct routes hit distinct SmartAPI endpoints and must never wait on each other"


def test_rate_limiter_respects_disabled_env_var(monkeypatch):
    monkeypatch.setitem(client_module.ROUTE_MIN_INTERVAL, "_test_route", 0.3)
    monkeypatch.setenv("ANGELONE_RATE_LIMIT_DISABLED", "true")
    limiter = _RateLimiter()

    start = time.monotonic()
    limiter.wait("_test_route")
    limiter.wait("_test_route")
    elapsed = time.monotonic() - start

    assert elapsed < 0.05, "ANGELONE_RATE_LIMIT_DISABLED=true should make wait() a no-op"


def test_rate_limiter_falls_back_to_default_interval_for_undocumented_routes():
    limiter = _RateLimiter()
    assert client_module.ROUTE_MIN_INTERVAL.get("some_totally_unknown_route") is None
    # doesn't raise, and uses DEFAULT_MIN_INTERVAL - just confirm it doesn't KeyError
    limiter.wait("some_totally_unknown_route")


# --------------------------------------------------------------------- #
# _is_rate_limited()
# --------------------------------------------------------------------- #

def test_is_rate_limited_detects_documented_403_response():
    assert AngelOneClient._is_rate_limited(403, _rate_limited_payload())


def test_is_rate_limited_detects_429():
    assert AngelOneClient._is_rate_limited(429, {"status": False, "message": "anything"})


def test_is_rate_limited_ignores_genuine_auth_failure():
    assert not AngelOneClient._is_rate_limited(
        403, {"status": False, "message": "Invalid Token", "errorcode": "AG8001"}
    )


def test_is_rate_limited_ignores_success():
    assert not AngelOneClient._is_rate_limited(200, _ok_payload())


# --------------------------------------------------------------------- #
# _send() backoff/retry on a live rate-limit response
# --------------------------------------------------------------------- #

def test_send_retries_on_rate_limit_and_eventually_succeeds(client, monkeypatch):
    monkeypatch.setattr(client_module, "RATE_LIMIT_BACKOFF_BASE_SECONDS", 0.01)
    # Keep the pacing wait tiny too - this test is about the retry loop, not
    # about order_book's real ~1.2s documented interval, and the same route
    # gets hit multiple times below.
    monkeypatch.setitem(client_module.ROUTE_MIN_INTERVAL, "order_book", 0.01)
    call_count = {"n": 0}

    def fake_request(method, url, **kw):
        call_count["n"] += 1
        if call_count["n"] < 3:
            return FakeResponse(_rate_limited_payload(), status_code=403)
        return FakeResponse(_ok_payload())

    client.session.request = fake_request

    status_code, data = client._send("GET", "https://example.invalid/x", "order_book")

    assert call_count["n"] == 3
    assert status_code == 200
    assert data["status"] is True


def test_send_gives_up_after_max_retries(client, monkeypatch):
    monkeypatch.setattr(client_module, "RATE_LIMIT_BACKOFF_BASE_SECONDS", 0.01)
    monkeypatch.setitem(client_module.ROUTE_MIN_INTERVAL, "order_book", 0.01)
    call_count = {"n": 0}

    def fake_request(method, url, **kw):
        call_count["n"] += 1
        return FakeResponse(_rate_limited_payload(), status_code=403)

    client.session.request = fake_request

    status_code, data = client._send("GET", "https://example.invalid/x", "order_book")

    assert call_count["n"] == client_module.MAX_RATE_LIMIT_RETRIES + 1  # initial attempt + retries
    assert status_code == 403
    assert AngelOneClient._is_rate_limited(status_code, data)


def test_request_raises_clear_error_after_exhausting_rate_limit_retries(client, monkeypatch):
    """A route that's still rate-limited after _send()'s own retries must
    surface as a clear AngelOneAPIError, not get misclassified as an auth
    failure and trigger a pointless re-login."""
    monkeypatch.setattr(client_module, "RATE_LIMIT_BACKOFF_BASE_SECONDS", 0.01)
    monkeypatch.setitem(client_module.ROUTE_MIN_INTERVAL, "order_book", 0.01)
    login_calls = {"n": 0}

    def fake_request(method, url, **kw):
        if "loginByPassword" in url:
            login_calls["n"] += 1
            return FakeResponse({"status": True, "data": {"jwtToken": "x", "refreshToken": "y", "feedToken": "z"}})
        return FakeResponse(_rate_limited_payload(), status_code=403)

    client.session.request = fake_request

    with pytest.raises(AngelOneAPIError, match="rate limited"):
        client.request("GET", "order_book")

    assert login_calls["n"] == 0, "a rate-limited 403 must never trigger a re-login attempt"


def test_request_still_treats_genuine_401_as_auth_failure_and_relogs_in(client, monkeypatch):
    """Make sure distinguishing rate-limit 403s from auth failures didn't
    break the original re-login-on-expired-token behavior."""
    monkeypatch.setattr(client_module, "RATE_LIMIT_BACKOFF_BASE_SECONDS", 0.01)
    monkeypatch.setitem(client_module.ROUTE_MIN_INTERVAL, "order_book", 0.01)
    call_log = []

    def fake_request(method, url, **kw):
        if "loginByPassword" in url:
            call_log.append("login")
            return FakeResponse({"status": True, "data": {"jwtToken": "new-jwt", "refreshToken": "r", "feedToken": "f"}})
        call_log.append("order_book")
        if call_log.count("order_book") == 1:
            return FakeResponse({"status": False, "errorcode": "AG8001", "message": "expired"}, status_code=401)
        return FakeResponse(_ok_payload())

    client.session.request = fake_request

    data = client.request("GET", "order_book")

    assert data["status"] is True
    assert call_log == ["order_book", "login", "order_book"]
    assert client.jwt_token == "new-jwt"


# --------------------------------------------------------------------- #
# every documented route has a sane interval
# --------------------------------------------------------------------- #

def test_every_route_has_a_positive_min_interval_or_uses_the_default():
    for route_key in ROUTES:
        interval = client_module.ROUTE_MIN_INTERVAL.get(route_key, client_module.DEFAULT_MIN_INTERVAL)
        assert interval > 0, f"route {route_key!r} has a non-positive rate-limit interval"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
