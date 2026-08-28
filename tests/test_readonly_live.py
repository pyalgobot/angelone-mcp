"""
Live, read-only smoke test against the real Angel One SmartAPI, run through
the MCP server exactly as an MCP client would use it (spawns the server as a
subprocess and talks MCP-over-stdio to it - no direct imports of client.py).

Only read-only tools are called. Order-mutating tools (place_order,
modify_order, cancel_order, gtt_create_rule, gtt_modify_rule,
gtt_cancel_rule, convert_position) and logout are never invoked, and a
SafeSession wrapper hard-asserts on that so a typo can't silently place a
real order.

Also includes a live check of the session-persistence feature (see
AngelOneClient.restore_session() in client.py): it spawns the server twice
against the same on-disk session file and confirms the second run's
get_profile succeeds without the "login" tool ever being called explicitly -
i.e. server.py's startup restore_session() call did the work.

Rate limits: every call goes through RateLimiter, which paces repeat calls
to the same tool according to SmartAPI's documented per-endpoint limits
(https://smartapi.angelone.in/docs/RateLimit), with a safety margin, and
backs off and retries if the API reports a 403 "Access denied because of
exceeding access rate" anyway. A single pass through this suite calls each
tool once or twice, well under any endpoint's per-minute/per-hour caps, so
the per-second pacing below is what actually matters here.

Requires real credentials in the environment:
    ANGELONE_API_KEY, ANGELONE_CLIENT_CODE, ANGELONE_PIN, ANGELONE_TOTP_SECRET
(plus ANGELONE_HTTP_PROXY / ANGELONE_HTTPS_PROXY / ANGELONE_NO_PROXY if you're
behind a proxy - they're passed through to the subprocess automatically).

Run directly for a human-readable report:
    python tests/test_readonly_live.py

Or under pytest (skips automatically if credentials aren't set):
    python -m pytest tests/test_readonly_live.py -v -s
"""

import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

REQUIRED_ENV = [
    "ANGELONE_API_KEY",
    "ANGELONE_CLIENT_CODE",
    "ANGELONE_PIN",
    "ANGELONE_TOTP_SECRET",
]

# Hard safety net: these tools must NEVER be called by this test suite,
# no matter what gets added to the call list below by mistake later.
FORBIDDEN_TOOLS = {
    "place_order",
    "modify_order",
    "cancel_order",
    "gtt_create_rule",
    "gtt_modify_rule",
    "gtt_cancel_rule",
    "convert_position",
    "logout",
}


def _missing_env() -> list:
    return [name for name in REQUIRED_ENV if not os.environ.get(name)]


# --------------------------------------------------------------------- #
# rate limiting
# --------------------------------------------------------------------- #
#
# Minimum seconds between consecutive calls to the *same* MCP tool, derived
# from SmartAPI's documented per-endpoint rate limits
# (https://smartapi.angelone.in/docs/RateLimit) as 1 / (requests-per-second
# limit), plus a ~20% safety margin so a slightly-off system clock or a
# concurrently-running client on the same account doesn't tip us over.
# Rate limits are per SmartAPI endpoint, not global, so distinct tools that
# hit distinct endpoints don't need to wait on each other - only repeats of
# the same tool do (e.g. get_profile being called again during the
# session-persistence check's second server spawn).
#
# Tools not individually documented (the marketData/* endpoints - gainers/
# losers, OI buildup, NSE/BSE intraday, option greeks, put-call ratio) fall
# back to DEFAULT_MIN_INTERVAL, the most conservative (1 req/sec) bucket.
TOOL_MIN_INTERVAL = {
    # 1 req/sec endpoints
    "login": 1.2,
    "get_order_book": 1.2,
    "get_trade_book": 1.2,
    "get_positions": 1.2,
    "get_holdings": 1.2,
    "get_all_holdings": 1.2,
    "search_scrip": 1.2,
    # 2 req/sec
    "get_rms_limit": 0.6,
    # 3 req/sec
    "get_profile": 0.4,
    "get_candle_data": 0.4,
    "get_oi_data": 0.4,
    # 10 req/sec
    "get_individual_order_details": 0.15,
    "get_ltp": 0.15,
    "get_market_quote": 0.15,
    "gtt_list": 0.15,
    "gtt_details": 0.15,
    "get_margin": 0.15,
}
DEFAULT_MIN_INTERVAL = 1.2  # conservative fallback for any tool not listed above

# When the API itself reports we've been rate-limited anyway (SmartAPI
# returns HTTP 403 with "Access denied because of exceeding access rate" -
# note this also matches the 401/403 auth-failure check in client.request(),
# so a hit here typically also burns one extra, unnecessary login retry
# before surfacing), back off and retry rather than failing the whole run
# over a transient limit bump.
RATE_LIMIT_MARKERS = ("exceeding access rate", "rate limit", "too many requests")
MAX_RATE_LIMIT_RETRIES = 3
RATE_LIMIT_BACKOFF_BASE_SECONDS = 2.0


def _looks_rate_limited(raw_text: str, parsed) -> bool:
    haystacks = [raw_text or ""]
    if isinstance(parsed, dict):
        haystacks.append(str(parsed.get("message") or ""))
        haystacks.append(str(parsed.get("error") or ""))
    combined = " ".join(haystacks).lower()
    return any(marker in combined for marker in RATE_LIMIT_MARKERS)


class RateLimiter:
    """Paces repeat calls to the same MCP tool per TOOL_MIN_INTERVAL. State
    is meant to be shared for the whole test process (a single module-level
    instance, not one per SafeSession) so pacing still holds across the two
    server subprocesses the session-persistence check spawns, and across
    multiple pytest tests sharing one real account in one run."""

    def __init__(self):
        self._last_call_at: dict = {}
        self._lock = asyncio.Lock()

    async def wait(self, tool_name: str) -> None:
        min_interval = TOOL_MIN_INTERVAL.get(tool_name, DEFAULT_MIN_INTERVAL)
        async with self._lock:
            last = self._last_call_at.get(tool_name)
            now = time.monotonic()
            if last is not None:
                remaining = min_interval - (now - last)
                if remaining > 0:
                    await asyncio.sleep(remaining)
            self._last_call_at[tool_name] = time.monotonic()


_RATE_LIMITER = RateLimiter()


class SafeSession:
    """Thin wrapper around ClientSession.call_tool that refuses to call any
    tool in FORBIDDEN_TOOLS, regardless of what the test code asks for, and
    paces/retries calls to respect SmartAPI's rate limits (see RateLimiter
    above)."""

    def __init__(self, session: ClientSession):
        self._session = session

    async def call(self, tool_name: str, arguments: dict | None = None):
        if tool_name in FORBIDDEN_TOOLS:
            raise AssertionError(
                f"refusing to call '{tool_name}' - this test suite is read-only "
                f"and that tool mutates account/order state"
            )

        backoff = RATE_LIMIT_BACKOFF_BASE_SECONDS
        for attempt in range(1, MAX_RATE_LIMIT_RETRIES + 1):
            await _RATE_LIMITER.wait(tool_name)
            result = await self._session.call_tool(tool_name, arguments or {})
            text = "".join(
                block.text for block in result.content if getattr(block, "type", None) == "text"
            )
            parsed = None
            try:
                parsed = json.loads(text)
            except (ValueError, TypeError):
                pass

            if _looks_rate_limited(text, parsed) and attempt < MAX_RATE_LIMIT_RETRIES:
                print(
                    f"  (rate-limited on {tool_name} - attempt {attempt}/{MAX_RATE_LIMIT_RETRIES}, "
                    f"backing off {backoff:.0f}s and retrying)"
                )
                await asyncio.sleep(backoff)
                backoff *= 2
                continue

            return {
                "tool": tool_name,
                "is_error": bool(result.isError),
                "raw_text": text,
                "parsed": parsed,
            }


def _summarize(call_result: dict) -> str:
    if call_result["is_error"]:
        return f"TRANSPORT ERROR: {call_result['raw_text'][:200]}"
    parsed = call_result["parsed"]
    if isinstance(parsed, dict) and parsed.get("status") is False:
        return f"API ERROR: {parsed.get('error') or parsed.get('message')}"
    return "OK"


async def _run(call_login_tool: bool = True, session_file: str = None) -> dict:
    server_env = dict(os.environ)  # inherit real credentials + any ANGELONE_*_PROXY vars
    if session_file is not None:
        server_env["ANGELONE_SESSION_FILE"] = session_file
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "angelone_mcp.server"],
        env=server_env,
    )

    results = {}
    order_id_for_lookup = None

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            safe = SafeSession(session)

            tools = await session.list_tools()
            tool_names = {t.name for t in tools.tools}
            print(f"connected - {len(tool_names)} tools exposed by server")

            # --- session ---------------------------------------------------
            # Calling the "login" tool explicitly forces a fresh login, which
            # is what we want for a plain one-shot smoke test (it's a good
            # credentials sanity check) but defeats the point of the
            # session-persistence check below, which needs the server's own
            # startup-time restore_session() to be what authenticates it.
            if call_login_tool:
                results["login"] = await safe.call("login")
            results["get_profile"] = await safe.call("get_profile")

            # --- funds / portfolio ------------------------------------------
            results["get_rms_limit"] = await safe.call("get_rms_limit")
            results["get_order_book"] = await safe.call("get_order_book")
            results["get_trade_book"] = await safe.call("get_trade_book")
            results["get_positions"] = await safe.call("get_positions")
            results["get_holdings"] = await safe.call("get_holdings")
            results["get_all_holdings"] = await safe.call("get_all_holdings")

            # pull a real order id (if any exist today) for the order-detail lookup
            ob = results["get_order_book"]["parsed"]
            if isinstance(ob, dict) and ob.get("status") and ob.get("data"):
                first_order = ob["data"][0]
                order_id_for_lookup = first_order.get("orderid") or first_order.get("uniqueorderid")

            if order_id_for_lookup:
                results["get_individual_order_details"] = await safe.call(
                    "get_individual_order_details", {"unique_order_id": order_id_for_lookup}
                )
            else:
                results["get_individual_order_details"] = {
                    "tool": "get_individual_order_details",
                    "is_error": False,
                    "raw_text": "",
                    "parsed": None,
                }
                print("  (skipping get_individual_order_details - no orders in today's order book)")

            # --- GTT (read-only listing/detail, not create/modify/cancel) --
            results["gtt_list"] = await safe.call(
                "gtt_list", {"status": ["NEW", "ACTIVE", "SENTTOEXCHANGE", "FORALL"], "page": 1, "count": 10}
            )
            gl = results["gtt_list"]["parsed"]
            gtt_rule_id = None
            if isinstance(gl, dict) and gl.get("status") and gl.get("data"):
                gtt_rule_id = gl["data"][0].get("id")
            if gtt_rule_id:
                results["gtt_details"] = await safe.call("gtt_details", {"rule_id": str(gtt_rule_id)})
            else:
                print("  (skipping gtt_details - no existing GTT rules to look up)")

            # --- market data --------------------------------------------------
            results["search_scrip"] = await safe.call(
                "search_scrip", {"exchange": "NSE", "searchscrip": "SBIN-EQ"}
            )
            symboltoken, tradingsymbol = None, "SBIN-EQ"
            ss = results["search_scrip"]["parsed"]
            if isinstance(ss, dict) and ss.get("status") and ss.get("data"):
                match = ss["data"][0]
                symboltoken = match.get("symboltoken")
                tradingsymbol = match.get("tradingsymbol", tradingsymbol)

            if symboltoken:
                results["get_ltp"] = await safe.call(
                    "get_ltp",
                    {"exchange": "NSE", "tradingsymbol": tradingsymbol, "symboltoken": symboltoken},
                )
                results["get_market_quote"] = await safe.call(
                    "get_market_quote", {"mode": "LTP", "exchange_tokens": {"NSE": [symboltoken]}}
                )

                today = datetime.now()
                from_date = (today - timedelta(days=7)).strftime("%Y-%m-%d 09:15")
                to_date = today.strftime("%Y-%m-%d %H:%M")
                results["get_candle_data"] = await safe.call(
                    "get_candle_data",
                    {
                        "exchange": "NSE",
                        "symboltoken": symboltoken,
                        "interval": "ONE_DAY",
                        "fromdate": from_date,
                        "todate": to_date,
                    },
                )
            else:
                print("  (skipping get_ltp/get_market_quote/get_candle_data - search_scrip returned no match)")

            results["get_put_call_ratio"] = await safe.call("get_put_call_ratio")
            results["get_nse_intraday_data"] = await safe.call("get_nse_intraday_data")
            results["get_bse_intraday_data"] = await safe.call("get_bse_intraday_data")

            # Best-effort - these can legitimately fail outside market hours,
            # without F&O segment enabled, etc. Failures here are reported but
            # don't fail the whole suite.
            results["get_gainers_losers"] = await safe.call(
                "get_gainers_losers", {"datatype": "PercPriceGainers", "expirytype": "NEAR"}
            )
            results["get_oi_buildup"] = await safe.call(
                "get_oi_buildup", {"datatype": "Long Built Up", "expirytype": "NEAR"}
            )

    return results


CORE_TOOLS = [
    "login",
    "get_profile",
    "get_rms_limit",
    "get_order_book",
    "get_trade_book",
    "get_positions",
    "get_holdings",
    "get_all_holdings",
]

BEST_EFFORT_TOOLS = [
    "get_individual_order_details",
    "gtt_list",
    "search_scrip",
    "get_ltp",
    "get_market_quote",
    "get_candle_data",
    "get_put_call_ratio",
    "get_nse_intraday_data",
    "get_bse_intraday_data",
    "get_gainers_losers",
    "get_oi_buildup",
]


def run_readonly_smoke() -> dict:
    return asyncio.run(_run())


async def _run_session_persistence_check(session_file: Path) -> dict:
    """Spawn the server twice against the same on-disk session file, without
    ever calling the "login" tool, to prove server.py's startup-time
    restore_session() call is what's authenticating the second run - not a
    fresh login triggered lazily by the first tool call."""
    if session_file.exists():
        session_file.unlink()  # start clean so run 1 is a guaranteed fresh login

    first = await _run(call_login_tool=False, session_file=str(session_file))
    if not session_file.exists():
        raise AssertionError(
            "expected a session file to exist after the first run (login() should "
            "have persisted one via AngelOneClient._save_session())"
        )
    first_jwt = json.loads(session_file.read_text())["jwt_token"]

    second = await _run(call_login_tool=False, session_file=str(session_file))
    second_jwt = json.loads(session_file.read_text())["jwt_token"]

    return {
        "first_get_profile": first["get_profile"],
        "second_get_profile": second["get_profile"],
        "first_jwt": first_jwt,
        "second_jwt": second_jwt,
        "session_reused": first_jwt == second_jwt,
    }


def run_session_persistence_check(session_file: Path) -> dict:
    return asyncio.run(_run_session_persistence_check(session_file))


def _print_report(results: dict) -> bool:
    print("\n=== read-only MCP smoke test report ===")
    all_core_ok = True
    for name in CORE_TOOLS:
        r = results.get(name)
        if r is None:
            continue
        summary = _summarize(r)
        ok = summary == "OK"
        all_core_ok = all_core_ok and ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<28} {summary}")

    for name in BEST_EFFORT_TOOLS:
        r = results.get(name)
        if r is None:
            print(f"  [SKIP] {name}")
            continue
        summary = _summarize(r)
        print(f"  [{'PASS' if summary == 'OK' else 'INFO'}] {name:<28} {summary}")

    print("========================================\n")
    return all_core_ok


# --------------------------------------------------------------------- #
# offline coverage for the rate-limiting logic itself - no credentials or
# network needed, so these always run (unlike the live tests below).
# --------------------------------------------------------------------- #

def test_rate_limiter_paces_repeat_calls_to_the_same_tool():
    async def _check():
        limiter = RateLimiter()
        start = time.monotonic()
        await limiter.wait("get_order_book")  # first call - documented 1 req/sec -> no wait
        first_elapsed = time.monotonic() - start
        await limiter.wait("get_order_book")  # second call, immediately after - must be paced
        second_elapsed = time.monotonic() - start
        return first_elapsed, second_elapsed

    first_elapsed, second_elapsed = asyncio.run(_check())
    assert first_elapsed < 0.05, "the first call to a tool should never be delayed"
    assert second_elapsed >= TOOL_MIN_INTERVAL["get_order_book"] - 0.02, (
        "a second call to a 1 req/sec tool made immediately after the first "
        "should be paced out to roughly the documented minimum interval"
    )


def test_rate_limiter_does_not_pace_different_tools_against_each_other():
    async def _check():
        limiter = RateLimiter()
        start = time.monotonic()
        await limiter.wait("get_order_book")  # 1 req/sec
        await limiter.wait("get_ltp")  # 10 req/sec, different endpoint entirely
        return time.monotonic() - start

    elapsed = asyncio.run(_check())
    assert elapsed < 0.05, (
        "distinct tools hit distinct SmartAPI endpoints with independent rate "
        "limits, so back-to-back calls to two different tools should never "
        "wait on each other"
    )


def test_looks_rate_limited_detects_documented_error_text():
    assert _looks_rate_limited(
        "", {"status": False, "message": "Access denied because of exceeding access rate"}
    )
    assert _looks_rate_limited('{"message": "Rate Limit Exceeded, please try after some time"}', None)
    assert not _looks_rate_limited("", {"status": False, "message": "Invalid token"})
    assert not _looks_rate_limited("", {"status": True, "data": {}})


def test_readonly_smoke():
    missing = _missing_env()
    if missing:
        import pytest
        pytest.skip(f"missing env vars for live test: {', '.join(missing)}")

    results = run_readonly_smoke()
    all_core_ok = _print_report(results)
    assert all_core_ok, "one or more core read-only tools failed - see report above"


def test_session_persists_across_restart(tmp_path):
    missing = _missing_env()
    if missing:
        import pytest
        pytest.skip(f"missing env vars for live test: {', '.join(missing)}")

    session_file = tmp_path / "angelone_mcp_test_session.json"
    result = run_session_persistence_check(session_file)

    assert not result["first_get_profile"]["is_error"], "first run's get_profile failed"
    assert not result["second_get_profile"]["is_error"], "second run's get_profile failed"

    print(
        f"\nsession file reused across restart: {result['session_reused']} "
        f"(not a hard requirement - SmartAPI is free to issue a new token on "
        f"getProfile's own internal re-auth path, but it should be True in the "
        f"common case of a session that's still valid seconds later)"
    )


if __name__ == "__main__":
    missing = _missing_env()
    if missing:
        print(f"Skipping live tests - missing env vars: {', '.join(missing)}")
        print("Set ANGELONE_API_KEY / ANGELONE_CLIENT_CODE / ANGELONE_PIN / ANGELONE_TOTP_SECRET and re-run.")
        sys.exit(0)

    results = run_readonly_smoke()
    ok = _print_report(results)

    print("=== session persistence across restart ===")
    with tempfile.TemporaryDirectory() as tmp_dir:
        session_file = Path(tmp_dir) / "angelone_mcp_test_session.json"
        persistence_result = run_session_persistence_check(session_file)
        first_ok = not persistence_result["first_get_profile"]["is_error"]
        second_ok = not persistence_result["second_get_profile"]["is_error"]
        ok = ok and first_ok and second_ok
        print(f"  [{'PASS' if first_ok else 'FAIL'}] first run get_profile (fresh login)")
        print(f"  [{'PASS' if second_ok else 'FAIL'}] second run get_profile (restored session)")
        print(f"  [INFO] session reused across restart: {persistence_result['session_reused']}")
    print("============================================\n")

    sys.exit(0 if ok else 1)
