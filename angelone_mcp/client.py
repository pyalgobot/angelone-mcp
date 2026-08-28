"""
Thin HTTP client for Angel One's SmartAPI (https://smartapi.angelone.in/docs).

Handles:
  - TOTP-based login (clientcode + pin + totp -> jwtToken/refreshToken/feedToken)
  - Required security headers on every request
  - Automatic re-login on token expiry (403 / TokenException)
  - Client-side pacing + backoff/retry against SmartAPI's documented
    per-endpoint rate limits (see ROUTE_MIN_INTERVAL below)
  - Every documented REST route, grouped by feature area
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import pyotp
import requests

ROOT_URL = "https://apiconnect.angelone.in"

ROUTES = {
    "login": "/rest/auth/angelbroking/user/v1/loginByPassword",
    "logout": "/rest/secure/angelbroking/user/v1/logout",
    "generate_token": "/rest/auth/angelbroking/jwt/v1/generateTokens",
    "profile": "/rest/secure/angelbroking/user/v1/getProfile",
    "place_order": "/rest/secure/angelbroking/order/v1/placeOrder",
    "modify_order": "/rest/secure/angelbroking/order/v1/modifyOrder",
    "cancel_order": "/rest/secure/angelbroking/order/v1/cancelOrder",
    "order_book": "/rest/secure/angelbroking/order/v1/getOrderBook",
    "ltp_data": "/rest/secure/angelbroking/order/v1/getLtpData",
    "trade_book": "/rest/secure/angelbroking/order/v1/getTradeBook",
    "rms_limit": "/rest/secure/angelbroking/user/v1/getRMS",
    "holding": "/rest/secure/angelbroking/portfolio/v1/getHolding",
    "all_holding": "/rest/secure/angelbroking/portfolio/v1/getAllHolding",
    "position": "/rest/secure/angelbroking/order/v1/getPosition",
    "convert_position": "/rest/secure/angelbroking/order/v1/convertPosition",
    "gtt_create": "/gtt-service/rest/secure/angelbroking/gtt/v1/createRule",
    "gtt_modify": "/gtt-service/rest/secure/angelbroking/gtt/v1/modifyRule",
    "gtt_cancel": "/gtt-service/rest/secure/angelbroking/gtt/v1/cancelRule",
    "gtt_details": "/rest/secure/angelbroking/gtt/v1/ruleDetails",
    "gtt_list": "/rest/secure/angelbroking/gtt/v1/ruleList",
    "candle_data": "/rest/secure/angelbroking/historical/v1/getCandleData",
    "oi_data": "/rest/secure/angelbroking/historical/v1/getOIData",
    "market_data": "/rest/secure/angelbroking/market/v1/quote",
    "search_scrip": "/rest/secure/angelbroking/order/v1/searchScrip",
    "individual_order_details": "/rest/secure/angelbroking/order/v1/details/{unique_order_id}",
    "margin_api": "/rest/secure/angelbroking/margin/v1/batch",
    "estimate_charges": "/rest/secure/angelbroking/brokerage/v1/estimateCharges",
    "verify_dis": "/rest/secure/angelbroking/edis/v1/verifyDis",
    "generate_tpin": "/rest/secure/angelbroking/edis/v1/generateTPIN",
    "tran_status": "/rest/secure/angelbroking/edis/v1/getTranStatus",
    "option_greek": "/rest/secure/angelbroking/marketData/v1/optionGreek",
    "gainers_losers": "/rest/secure/angelbroking/marketData/v1/gainersLosers",
    "put_call_ratio": "/rest/secure/angelbroking/marketData/v1/putCallRatio",
    "oi_buildup": "/rest/secure/angelbroking/marketData/v1/OIBuildup",
    "nse_intraday": "/rest/secure/angelbroking/marketData/v1/nseIntraday",
    "bse_intraday": "/rest/secure/angelbroking/marketData/v1/bseIntraday",
}

# Minimum seconds between consecutive calls to the *same* route, derived
# from SmartAPI's documented per-endpoint rate limits
# (https://smartapi.angelone.in/docs/RateLimit) as 1 / (requests-per-second
# limit), plus a ~20% safety margin. Rate limits are per SmartAPI endpoint,
# not global, so distinct routes are never paced against each other - only
# repeats of the same route are. Routes not individually documented (the
# marketData/* endpoints, eDIS) fall back to DEFAULT_MIN_INTERVAL, the most
# conservative (1 req/sec) bucket.
ROUTE_MIN_INTERVAL = {
    # 1 req/sec
    "login": 1.2,
    "logout": 1.2,
    "generate_token": 1.2,
    "order_book": 1.2,
    "trade_book": 1.2,
    "position": 1.2,
    "holding": 1.2,
    "all_holding": 1.2,
    "search_scrip": 1.2,
    # 2 req/sec
    "rms_limit": 0.6,
    # 3 req/sec
    "profile": 0.4,
    "candle_data": 0.4,
    "oi_data": 0.4,
    # 10 req/sec
    "individual_order_details": 0.15,
    "ltp_data": 0.15,
    "market_data": 0.15,
    "convert_position": 0.15,
    "gtt_create": 0.15,
    "gtt_modify": 0.15,
    "gtt_cancel": 0.15,
    "gtt_details": 0.15,
    "gtt_list": 0.15,
    "margin_api": 0.15,
    # 20 req/sec
    "place_order": 0.06,
    "modify_order": 0.06,
    "cancel_order": 0.06,
}
DEFAULT_MIN_INTERVAL = 1.2  # conservative fallback for any route not listed above

# When SmartAPI reports its own rate limit was hit anyway (HTTP 403 or 429,
# "Access denied because of exceeding access rate"), back off and retry
# rather than surfacing the error immediately - or, worse, having
# request()'s auth-failure check mistake a rate-limit 403 for an expired
# token and force an unnecessary, budget-consuming re-login.
RATE_LIMIT_MARKERS = ("exceeding access rate", "rate limit", "too many requests")
MAX_RATE_LIMIT_RETRIES = 3
RATE_LIMIT_BACKOFF_BASE_SECONDS = 1.5


def _rate_limit_disabled() -> bool:
    return os.environ.get("ANGELONE_RATE_LIMIT_DISABLED", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


class _RateLimiter:
    """Paces calls per SmartAPI route to ROUTE_MIN_INTERVAL. Thread-safe:
    reserves the next allowed slot for a route under a short lock, then
    sleeps outside the lock, so a wait on one route never blocks a
    concurrent call to a different route (or even the same route, beyond
    correctly serializing it)."""

    def __init__(self) -> None:
        self._next_allowed_at: dict = {}
        self._lock = threading.Lock()

    def wait(self, route_key: str) -> None:
        if _rate_limit_disabled():
            return
        min_interval = ROUTE_MIN_INTERVAL.get(route_key, DEFAULT_MIN_INTERVAL)
        with self._lock:
            now = time.monotonic()
            earliest = self._next_allowed_at.get(route_key, 0.0)
            start_at = max(now, earliest)
            self._next_allowed_at[route_key] = start_at + min_interval
        sleep_for = start_at - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)


class AngelOneAPIError(RuntimeError):
    """Raised whenever SmartAPI returns status=false or an HTTP error."""


class AngelOneClient:
    """
    Lazily authenticates on first use and re-authenticates once if a call
    fails due to an expired/invalid session token.

    Required environment variables:
      ANGELONE_API_KEY        - API key from the SmartAPI developer app
      ANGELONE_CLIENT_CODE    - Angel One client / trading account code
      ANGELONE_PIN            - Login PIN (used as "password" by loginByPassword)
      ANGELONE_TOTP_SECRET    - Base32 TOTP secret configured for the account
                                 (the same secret used to set up the authenticator app)

    Optional environment variables (outbound HTTP proxy support):
      ANGELONE_HTTP_PROXY     - proxy URL used for plain http:// requests
                                 (e.g. http://user:pass@proxyhost:8080)
      ANGELONE_HTTPS_PROXY    - proxy URL used for https:// requests (SmartAPI
                                 is https-only, so this is the one that matters
                                 in practice). Falls back to ANGELONE_HTTP_PROXY
                                 if not set.
      ANGELONE_NO_PROXY       - comma-separated hosts to bypass the proxy for
                                 (passed through as the standard NO_PROXY value)

      If neither ANGELONE_HTTP_PROXY nor ANGELONE_HTTPS_PROXY is set, the
      standard HTTP_PROXY/HTTPS_PROXY/http_proxy/https_proxy environment
      variables are used instead (requests' normal behavior). The ANGELONE_*
      variables exist because MCP clients often launch this server with an
      explicit, minimal env block that doesn't inherit the parent shell's
      proxy settings - see README.md.

    Optional environment variables (session persistence across restarts):
      ANGELONE_SESSION_PERSIST  - "false"/"0"/"no"/"off" disables persistence
                                    entirely (default: enabled).
      ANGELONE_SESSION_FILE     - override the path used to persist the
                                    session. Default: a file named after a
                                    hash of the client code, under the OS
                                    temp directory (e.g. /tmp on Linux/macOS,
                                    %TEMP% on Windows).

      When enabled, a successful login() writes jwtToken/refreshToken/
      feedToken to that file (best-effort; failures to read/write it are
      never fatal - they just fall back to a fresh login). On the next
      process start, call restore_session() (server.py does this once at
      startup, not on every import) to load the saved tokens and verify them
      with a real getProfile call; if that call fails for any reason, the
      saved tokens are discarded and a fresh login() is performed instead.
      logout() deletes the persisted file. The file holds a live session
      token (bearer-equivalent access to the account, though not the PIN or
      TOTP secret) - it's written with owner-only permissions where the OS
      supports it, but treat it as sensitive and don't relax that.

    Optional environment variables (rate limiting):
      ANGELONE_RATE_LIMIT_DISABLED - "true"/"1"/"yes"/"on" disables the
                                       client-side pacing described below
                                       (default: enabled).

      Every call is paced against SmartAPI's documented per-endpoint rate
      limits (see ROUTE_MIN_INTERVAL above; default: enabled, and safe to
      leave enabled - it only ever delays a repeat call to the same
      endpoint made faster than that endpoint's own documented limit
      allows, which is never desirable anyway). If SmartAPI reports a rate
      limit was hit regardless, the call backs off and retries a few times
      before giving up - see MAX_RATE_LIMIT_RETRIES/RATE_LIMIT_MARKERS.
    """

    def __init__(self) -> None:
        self.api_key = os.environ.get("ANGELONE_API_KEY", "")
        self.client_code = os.environ.get("ANGELONE_CLIENT_CODE", "")
        self.pin = os.environ.get("ANGELONE_PIN", "")
        self.totp_secret = os.environ.get("ANGELONE_TOTP_SECRET", "")

        self.jwt_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.feed_token: Optional[str] = None

        self.session = requests.Session()
        self._rate_limiter = _RateLimiter()
        self._configure_proxy()
        self._client_local_ip = self._detect_local_ip()
        self._client_public_ip = self._detect_public_ip()
        mac_hex = "%012x" % uuid.getnode()
        self._client_mac = ":".join(mac_hex[i:i + 2] for i in range(0, 12, 2))

    def _configure_proxy(self) -> None:
        """Wire up outbound HTTP(S) proxy support on self.session.

        Explicit ANGELONE_HTTP_PROXY / ANGELONE_HTTPS_PROXY env vars take
        priority; if neither is set, requests' default behavior (honoring
        HTTP_PROXY/HTTPS_PROXY/NO_PROXY from the environment) is left alone.
        """
        http_proxy = os.environ.get("ANGELONE_HTTP_PROXY")
        https_proxy = os.environ.get("ANGELONE_HTTPS_PROXY") or http_proxy
        no_proxy = os.environ.get("ANGELONE_NO_PROXY")

        if not http_proxy and not https_proxy:
            return  # let requests fall back to the standard *_PROXY env vars

        proxies = {}
        if http_proxy:
            proxies["http"] = http_proxy
        if https_proxy:
            proxies["https"] = https_proxy
        self.session.proxies.update(proxies)

        if no_proxy:
            # requests reads NO_PROXY from the environment (via urllib's
            # proxy_bypass), so set it there too if the caller provided an
            # angelone-specific override.
            os.environ.setdefault("NO_PROXY", no_proxy)
            os.environ["NO_PROXY"] = no_proxy

    # ------------------------------------------------------------------ #
    # session persistence (survive a process restart)
    # ------------------------------------------------------------------ #

    def _session_persist_enabled(self) -> bool:
        return os.environ.get("ANGELONE_SESSION_PERSIST", "true").strip().lower() not in (
            "0", "false", "no", "off",
        )

    def _session_file_path(self) -> Optional[Path]:
        override = os.environ.get("ANGELONE_SESSION_FILE")
        if override:
            return Path(override)
        if not self.client_code:
            return None
        # Hash the client code rather than using it verbatim in the filename
        # so the account identifier isn't sitting in plain sight in a shared
        # temp directory listing.
        digest = hashlib.sha256(self.client_code.encode("utf-8")).hexdigest()[:16]
        return Path(tempfile.gettempdir()) / f".angelone_mcp_session_{digest}.json"

    def _save_session(self) -> None:
        """Best-effort: persist the current tokens to disk. Never raises -
        a failure here should degrade to "log in again next time", not crash
        an otherwise-successful login."""
        if not self._session_persist_enabled() or not self.jwt_token:
            return
        path = self._session_file_path()
        if not path:
            return
        payload = {
            "client_code": self.client_code,
            "jwt_token": self.jwt_token,
            "refresh_token": self.refresh_token,
            "feed_token": self.feed_token,
            "saved_at": time.time(),
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            tmp_path.write_text(json.dumps(payload), encoding="utf-8")
            try:
                os.chmod(tmp_path, 0o600)  # best-effort; no-op on most Windows filesystems
            except OSError:
                pass
            tmp_path.replace(path)
        except OSError:
            pass

    def _load_session_from_disk(self) -> bool:
        """Load a previously saved session into memory, unverified. Returns
        True if a plausible session was found and loaded."""
        if not self._session_persist_enabled():
            return False
        path = self._session_file_path()
        if not path or not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict) or data.get("client_code") != self.client_code:
            return False  # missing, corrupt, or belongs to a different account
        jwt_token = data.get("jwt_token")
        if not jwt_token:
            return False
        self.jwt_token = jwt_token
        self.refresh_token = data.get("refresh_token")
        self.feed_token = data.get("feed_token")
        return True

    def _clear_session_file(self) -> None:
        path = self._session_file_path()
        if path and path.exists():
            try:
                path.unlink()
            except OSError:
                pass

    def restore_session(self) -> dict:
        """Try to reuse a session persisted by a previous run, verifying it
        with a real getProfile call; fall back to a fresh login if there's
        nothing saved, or if the saved tokens turn out to be invalid/expired.

        Intended to be called once, explicitly, at server startup (see
        server.py's main()) - not from __init__, so simply constructing an
        AngelOneClient (e.g. for tests that only list tools) never makes a
        network call.
        """
        if not self._load_session_from_disk():
            return {"status": True, "source": "no_saved_session"}

        try:
            self.get_profile()
            return {"status": True, "source": "restored_session"}
        except Exception:
            self.jwt_token = None
            self.refresh_token = None
            self.feed_token = None
            self.login()
            return {"status": True, "source": "fresh_login_after_invalid_saved_session"}

    # ------------------------------------------------------------------ #
    # low-level plumbing
    # ------------------------------------------------------------------ #

    @staticmethod
    def _detect_local_ip() -> str:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"

    def _detect_public_ip(self) -> str:
        try:
            r = self.session.get("https://api.ipify.org", timeout=3)
            return r.text.strip()
        except Exception:
            return "106.193.147.98"

    def _headers(self) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": self._client_local_ip,
            "X-ClientPublicIP": self._client_public_ip,
            "X-MACAddress": self._client_mac,
            "X-PrivateKey": self.api_key,
        }
        if self.jwt_token:
            headers["Authorization"] = f"Bearer {self.jwt_token}"
        return headers

    def _check_credentials(self) -> None:
        missing = [
            name
            for name, val in [
                ("ANGELONE_API_KEY", self.api_key),
                ("ANGELONE_CLIENT_CODE", self.client_code),
                ("ANGELONE_PIN", self.pin),
                ("ANGELONE_TOTP_SECRET", self.totp_secret),
            ]
            if not val
        ]
        if missing:
            raise AngelOneAPIError(
                "Missing required environment variable(s): " + ", ".join(missing)
            )

    def login(self) -> dict:
        """Authenticate using clientcode + pin + a freshly generated TOTP."""
        self._check_credentials()
        totp = pyotp.TOTP(self.totp_secret).now()
        payload = {
            "clientcode": self.client_code,
            "password": self.pin,
            "totp": totp,
        }
        url = ROOT_URL + ROUTES["login"]
        _status_code, data = self._send(
            "POST", url, "login", json=payload, headers=self._headers(), timeout=10
        )
        if not data.get("status"):
            raise AngelOneAPIError(f"Login failed: {data.get('message')} ({data.get('errorcode')})")
        self.jwt_token = data["data"]["jwtToken"]
        self.refresh_token = data["data"]["refreshToken"]
        self.feed_token = data["data"]["feedToken"]
        self._save_session()
        return data

    def logout(self) -> dict:
        result = self.request("POST", "logout", json={"clientcode": self.client_code})
        self.jwt_token = None
        self.refresh_token = None
        self.feed_token = None
        self._clear_session_file()
        return result

    @staticmethod
    def _parse(resp: requests.Response) -> dict:
        try:
            return resp.json()
        except ValueError as e:
            raise AngelOneAPIError(
                f"Non-JSON response (HTTP {resp.status_code}): {resp.text[:500]}"
            ) from e

    @staticmethod
    def _is_rate_limited(status_code: int, data: dict) -> bool:
        if status_code == 429:
            return True
        message = " ".join(
            str(data.get(k, "")) for k in ("message", "error", "errormessage")
        ).lower()
        return any(marker in message for marker in RATE_LIMIT_MARKERS)

    def _send(self, method: str, url: str, route_key: str, timeout: int = 15, **kwargs) -> tuple:
        """Rate-limit-aware HTTP call: paces itself against
        ROUTE_MIN_INTERVAL for route_key via self._rate_limiter, and if
        SmartAPI reports its own rate limit was hit anyway, backs off and
        retries (same headers/body, fresh pacing wait each attempt) instead
        of surfacing the error immediately. Returns (status_code, parsed_json)
        - the caller decides what a given status/payload combination means
        (auth failure, business-logic error, success, etc.)."""
        backoff = RATE_LIMIT_BACKOFF_BASE_SECONDS
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            self._rate_limiter.wait(route_key)
            resp = self.session.request(method, url, timeout=timeout, **kwargs)
            data = self._parse(resp)
            if self._is_rate_limited(resp.status_code, data) and attempt < MAX_RATE_LIMIT_RETRIES:
                time.sleep(backoff)
                backoff *= 2
                continue
            return resp.status_code, data

    def request(
        self,
        method: str,
        route_key: str,
        json: Optional[dict] = None,
        path_params: Optional[dict] = None,
        params: Optional[dict] = None,
        _retried: bool = False,
    ) -> dict:
        """Make an authenticated request to a named route, auto re-logging-in once on auth failure."""
        if not self.jwt_token:
            self.login()

        path = ROUTES[route_key]
        if path_params:
            path = path.format(**path_params)
        url = ROOT_URL + path

        status_code, data = self._send(
            method, url, route_key, json=json, params=params, headers=self._headers()
        )

        if self._is_rate_limited(status_code, data):
            # _send() already retried MAX_RATE_LIMIT_RETRIES times; still
            # rate-limited means give up cleanly rather than mistaking this
            # 403 for an expired token and burning a needless re-login.
            raise AngelOneAPIError(
                f"{route_key} failed: rate limited by SmartAPI after "
                f"{MAX_RATE_LIMIT_RETRIES} retries ({data.get('message') or 'exceeding access rate'})"
            )

        auth_failed = (
            status_code in (401, 403)
            or data.get("errorcode") in ("AG8001", "AG8002", "AG8003")
            or data.get("error_type") == "TokenException"
        )
        if auth_failed and not _retried:
            self.login()
            return self.request(method, route_key, json=json, path_params=path_params, params=params, _retried=True)

        if data.get("status") is False:
            raise AngelOneAPIError(
                f"{route_key} failed: {data.get('message')} (errorcode={data.get('errorcode')})"
            )
        return data

    # ------------------------------------------------------------------ #
    # convenience wrappers, one per documented endpoint
    # ------------------------------------------------------------------ #

    def get_profile(self) -> dict:
        return self.request("GET", "profile")

    def place_order(self, **order_params) -> dict:
        clean = {k: v for k, v in order_params.items() if v is not None}
        return self.request("POST", "place_order", json=clean)

    def modify_order(self, **order_params) -> dict:
        clean = {k: v for k, v in order_params.items() if v is not None}
        return self.request("POST", "modify_order", json=clean)

    def cancel_order(self, variety: str, order_id: str) -> dict:
        return self.request("POST", "cancel_order", json={"variety": variety, "orderid": order_id})

    def order_book(self) -> dict:
        return self.request("GET", "order_book")

    def trade_book(self) -> dict:
        return self.request("GET", "trade_book")

    def ltp_data(self, exchange: str, tradingsymbol: str, symboltoken: str) -> dict:
        return self.request(
            "POST",
            "ltp_data",
            json={"exchange": exchange, "tradingsymbol": tradingsymbol, "symboltoken": symboltoken},
        )

    def rms_limit(self) -> dict:
        return self.request("GET", "rms_limit")

    def position(self) -> dict:
        return self.request("GET", "position")

    def holding(self) -> dict:
        return self.request("GET", "holding")

    def all_holding(self) -> dict:
        return self.request("GET", "all_holding")

    def convert_position(self, **params) -> dict:
        clean = {k: v for k, v in params.items() if v is not None}
        return self.request("POST", "convert_position", json=clean)

    def gtt_create_rule(self, **params) -> dict:
        clean = {k: v for k, v in params.items() if v is not None}
        return self.request("POST", "gtt_create", json=clean)

    def gtt_modify_rule(self, **params) -> dict:
        clean = {k: v for k, v in params.items() if v is not None}
        return self.request("POST", "gtt_modify", json=clean)

    def gtt_cancel_rule(self, **params) -> dict:
        clean = {k: v for k, v in params.items() if v is not None}
        return self.request("POST", "gtt_cancel", json=clean)

    def gtt_details(self, rule_id: str) -> dict:
        return self.request("POST", "gtt_details", json={"id": rule_id})

    def gtt_list(self, status: list, page: int = 1, count: int = 25) -> dict:
        return self.request("POST", "gtt_list", json={"status": status, "page": page, "count": count})

    def candle_data(self, **params) -> dict:
        clean = {k: v for k, v in params.items() if v is not None}
        return self.request("POST", "candle_data", json=clean)

    def oi_data(self, **params) -> dict:
        clean = {k: v for k, v in params.items() if v is not None}
        return self.request("POST", "oi_data", json=clean)

    def market_data(self, mode: str, exchange_tokens: dict) -> dict:
        return self.request("POST", "market_data", json={"mode": mode, "exchangeTokens": exchange_tokens})

    def search_scrip(self, exchange: str, searchscrip: str) -> dict:
        return self.request("POST", "search_scrip", json={"exchange": exchange, "searchscrip": searchscrip})

    def individual_order_details(self, unique_order_id: str) -> dict:
        return self.request("GET", "individual_order_details", path_params={"unique_order_id": unique_order_id})

    def margin_api(self, positions: list) -> dict:
        return self.request("POST", "margin_api", json={"positions": positions})

    def estimate_charges(self, order_list: list) -> dict:
        return self.request("POST", "estimate_charges", json={"orders": order_list})

    def option_greek(self, name: str, expirydate: str) -> dict:
        return self.request("POST", "option_greek", json={"name": name, "expirydate": expirydate})

    def gainers_losers(self, datatype: str, expirytype: str) -> dict:
        return self.request("POST", "gainers_losers", json={"datatype": datatype, "expirytype": expirytype})

    def put_call_ratio(self) -> dict:
        return self.request("GET", "put_call_ratio")

    def oi_buildup(self, datatype: str, expirytype: str) -> dict:
        return self.request("POST", "oi_buildup", json={"datatype": datatype, "expirytype": expirytype})

    def nse_intraday(self) -> dict:
        return self.request("GET", "nse_intraday")

    def bse_intraday(self) -> dict:
        return self.request("GET", "bse_intraday")
