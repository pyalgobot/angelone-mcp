# angelone-mcp

[![Listed on mcpservers.org](https://mcpservers.org/badge.svg)](https://mcpservers.org/servers/pyalgobot/angelone-mcp.git)

An MCP (Model Context Protocol) server that wraps [Angel One's SmartAPI](https://smartapi.angelone.in/docs) —
trading, portfolio, market data, GTT rules, and margin/brokerage — so any MCP
client (Claude, Claude Code, etc.) can query your account and place orders
through natural conversation.

⚠️ **This places real orders on a real trading account.** Test with small
quantities first, and keep in mind Angel One (like most brokers) does not
let you "undo" a filled order.

## What's included

- `angelone_mcp/client.py` – REST client for every documented SmartAPI route:
  auth, orders, positions/holdings, GTT rules, historical candles/OI,
  quotes, option greeks, gainers/losers, margin calculator, brokerage
  estimator. Handles TOTP login, auto re-login on token expiry, and paces
  itself against SmartAPI's documented rate limits (see "Rate limiting"
  below).
- `angelone_mcp/server.py` – MCP server exposing 32 tools built on top of
  the client (see full list below).

## 1. Prerequisites

- Python 3.10+
- An Angel One trading account with SmartAPI access
- A SmartAPI app created at https://smartapi.angelone.in/ (gives you an API key)
- TOTP set up on your Angel One account, and the **base32 secret** used to
  set up that authenticator (not the 6-digit code — the secret behind it).
  You get this once, when you first scan the QR code to enable TOTP; if you
  don't have it saved, you'll need to reset/reconfigure TOTP on your account
  to get a fresh secret.

## 2. Install

```bash
cd angelone-mcp
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 3. Configure credentials

Set these environment variables (e.g. in a `.env` file you source, or
directly in your MCP client config):

| Variable | Description |
|---|---|
| `ANGELONE_API_KEY` | API key from your SmartAPI app |
| `ANGELONE_CLIENT_CODE` | Your Angel One client/trading account code |
| `ANGELONE_PIN` | Your login PIN |
| `ANGELONE_TOTP_SECRET` | Base32 TOTP secret for your account |

**Never commit these to source control.** Treat `ANGELONE_TOTP_SECRET` and
`ANGELONE_PIN` like passwords — anyone with them plus your API key can trade
on your account.

### Optional: running behind an HTTP proxy

If your machine/network requires an outbound HTTP proxy to reach the
internet, set:

| Variable | Description |
|---|---|
| `ANGELONE_HTTP_PROXY` | Proxy URL used for `http://` requests, e.g. `http://user:pass@proxyhost:8080` |
| `ANGELONE_HTTPS_PROXY` | Proxy URL used for `https://` requests (this is the one that matters — SmartAPI is https-only). Falls back to `ANGELONE_HTTP_PROXY` if unset. |
| `ANGELONE_NO_PROXY` | Optional comma-separated list of hosts to bypass the proxy for |

These are only needed if the standard `HTTP_PROXY` / `HTTPS_PROXY` environment
variables aren't already visible to the server process. That's commonly the
case for MCP servers, since MCP clients usually launch the server with an
explicit `env` block (like the JSON below) instead of inheriting your shell's
environment — so a proxy configured in your shell won't reach the server
unless you either add it to that `env` block yourself under `HTTPS_PROXY`, or
use the `ANGELONE_*` variables above. If neither `ANGELONE_HTTP_PROXY` nor
`ANGELONE_HTTPS_PROXY` is set, the server falls back to the standard
`HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` variables automatically.

## 4. Run it

Standalone (for testing):

```bash
python -m angelone_mcp.server
```

It speaks MCP over stdio, so it's meant to be launched by an MCP client, not
run interactively.

### Claude Desktop / Claude Code config

Add to your MCP client's config (e.g. `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "angelone": {
      "command": "/absolute/path/to/angelone-mcp/.venv/bin/python",
      "args": ["-m", "angelone_mcp.server"],
      "cwd": "/absolute/path/to/angelone-mcp",
      "env": {
        "ANGELONE_API_KEY": "your_api_key",
        "ANGELONE_CLIENT_CODE": "your_client_code",
        "ANGELONE_PIN": "your_pin",
        "ANGELONE_TOTP_SECRET": "your_base32_totp_secret",
        "ANGELONE_HTTPS_PROXY": "http://user:pass@proxyhost:8080"
      }
    }
  }
}
```

## Tools exposed

**Session**
`login`, `logout`, `get_profile`

**Orders**
`place_order`, `modify_order`, `cancel_order`, `get_order_book`,
`get_trade_book`, `get_individual_order_details`

**Portfolio / funds**
`get_positions`, `get_holdings`, `get_all_holdings`, `get_rms_limit`,
`convert_position`

**GTT (Good Till Triggered) rules**
`gtt_create_rule`, `gtt_modify_rule`, `gtt_cancel_rule`, `gtt_details`,
`gtt_list`

**Market data**
`get_ltp`, `get_market_quote`, `search_scrip`, `get_candle_data`,
`get_oi_data`, `get_option_greeks`, `get_gainers_losers`,
`get_put_call_ratio`, `get_oi_buildup`, `get_nse_intraday_data`,
`get_bse_intraday_data`

**Margin & brokerage**
`get_margin`, `estimate_charges`

## How auth works

`AngelOneClient` logs in lazily on the first tool call using
`clientcode` + `pin` + a TOTP generated on the fly from
`ANGELONE_TOTP_SECRET` (via `pyotp`). It caches the resulting `jwtToken`,
`refreshToken`, and `feedToken` in memory for the life of the process. If any
call comes back with a 401/403 or a `TokenException`, it transparently
re-logs-in once and retries — you don't need to call `login` yourself unless
you want to force a fresh session.

Sessions issued by SmartAPI are valid until midnight IST regardless of
activity, so a long-running server may still need a fresh login the next day
— the auto-retry logic handles that automatically on the next call.

### Session persistence across restarts

A successful login is also cached to a file on disk, so a fresh server
process doesn't need a fresh TOTP-based login every time it starts (handy
since TOTP requires the code to be freshly generated — restarting the server
several times in a row otherwise means several real logins in a row).

On startup, before serving any tool calls, the server calls
`AngelOneClient.restore_session()`, which:

1. Looks for a previously saved session file. If there isn't one, it does
   nothing further — the client stays in its normal lazy mode and logs in on
   the first tool call, same as before this feature existed.
2. If a saved session is found, it loads the cached tokens and verifies them
   with a real `getProfile` call.
3. If that verification succeeds, the restored session is used as-is — no
   fresh login needed.
4. If it fails for any reason (expired token, revoked session, corrupt file,
   etc.), the cached tokens are discarded and a normal fresh login runs
   instead.

Every successful login (fresh or via the automatic 401/403 retry described
above) re-saves the session file, so it stays current across the whole time
the server runs, not just at startup. `logout` deletes the file.

| Variable | Description |
|---|---|
| `ANGELONE_SESSION_PERSIST` | Set to `false`/`0`/`no`/`off` to disable session persistence entirely (default: enabled) |
| `ANGELONE_SESSION_FILE` | Override the file path used to persist the session. Default: a file under the OS temp directory, named from a hash of your client code (so multiple accounts on the same machine don't collide) |

The session file holds a live access token — not your PIN or TOTP secret,
but enough to call the API as you until it expires. It's written with
owner-only file permissions where the OS supports it; treat it as sensitive
the same way you'd treat any cached login session.

## Rate limiting

`AngelOneClient` paces every outgoing call against
[SmartAPI's documented per-endpoint rate limits](https://smartapi.angelone.in/docs/RateLimit)
— login and most portfolio reads at 1 request/sec, `getProfile` at 3/sec,
quotes/GTT/order-detail lookups at 10/sec, order placement at 20/sec, and so
on. Limits are per SmartAPI endpoint, not global, so calling different tools
back-to-back is never slowed down by this — only a *repeat* call to the same
endpoint made faster than SmartAPI's own limit allows gets held back, which
you'd want anyway.

If SmartAPI reports its own limit was hit regardless (HTTP 403/429, "Access
denied because of exceeding access rate"), the call backs off and retries a
few times with increasing delay before giving up — and that response no
longer gets misread as an expired session and doesn't trigger a spurious
extra login the way it used to.

This applies to every tool automatically; there's nothing to configure to
get it. To turn client-side pacing off entirely (SmartAPI still enforces its
own limits server-side either way — this only controls whether the client
tries to stay under them proactively):

| Variable | Description |
|---|---|
| `ANGELONE_RATE_LIMIT_DISABLED` | Set to `true`/`1`/`yes`/`on` to disable proactive pacing (default: enabled) |

## Testing

```bash
pip install -e ".[test]"

# Offline: verifies the server registers the expected tools. No credentials
# or network access needed.
python -m pytest tests/test_tool_registration.py -v

# Offline: unit tests for session persistence (login state cached to disk,
# restored + verified via get_profile on restart, falls back to a fresh
# login when the cache is missing/invalid). Uses a fake HTTP layer - no
# credentials or network access needed.
python -m pytest tests/test_session_persistence.py -v

# Offline: unit tests for AngelOneClient's own rate limiting (pacing per
# ROUTE_MIN_INTERVAL, backoff/retry on a 403/429 rate-limit response, and
# that such a response is never misread as an expired session). Uses a fake
# HTTP layer - no credentials or network access needed.
python -m pytest tests/test_client_rate_limiting.py -v

# Live, read-only smoke test against your real account. Calls get_profile,
# get_order_book, get_holdings, search_scrip, get_ltp, etc. through the
# actual MCP server subprocess, plus a check that a session survives a
# restart of the server without calling the "login" tool again. Never calls
# place_order/modify_order/cancel_order/gtt_create_rule/gtt_modify_rule/
# gtt_cancel_rule/convert_position/logout - a SafeSession wrapper
# hard-asserts those are never invoked. On top of the server's own rate
# limiting (see "Rate limiting" above), the test itself also paces its tool
# calls and backs off/retries if the API reports one was hit anyway (see
# "Rate limiting in the live test" below) - belt and suspenders. Requires
# ANGELONE_API_KEY/ANGELONE_CLIENT_CODE/ANGELONE_PIN/ANGELONE_TOTP_SECRET
# to be set; skips automatically if they aren't.
python -m pytest tests/test_readonly_live.py -v -s
# or, for a plain-text report without pytest:
python tests/test_readonly_live.py
```

### Rate limiting in the live test

The live test (`tests/test_readonly_live.py`) calls a real account against
the real SmartAPI. The server it drives already paces itself (see "Rate
limiting" above), but the test adds its own independent pacing on top -
useful because it also exercises things the server-side limiter doesn't see
by itself, like two separate server subprocesses (the session-persistence
check) hitting the same account back to back:

- A `RateLimiter` tracks the last time each MCP tool was called and, before
  calling it again, waits out the rest of that endpoint's minimum interval
  (1/req-per-second-limit, plus a ~20% safety margin). Distinct tools hit
  distinct SmartAPI endpoints with independent limits, so this only ever
  delays a *repeat* call to the same tool (e.g. `get_profile` being called
  again by the second server spawn in the session-persistence check) - a
  normal single pass through the suite, where every tool is called once or
  twice, isn't slowed down by it in practice.
- If SmartAPI reports a rate limit was hit anyway (HTTP 403, "Access denied
  because of exceeding access rate"), the test backs off and retries a
  couple of times with increasing delay instead of failing outright.
- This governs the test suite's own request pace only - it has no effect on
  how the MCP server behaves for a real MCP client (Claude, etc.); SmartAPI
  still enforces its limits server-side either way.

## Notes / limitations

- Order params (`price`, `quantity`, etc.) are passed as strings, matching
  what SmartAPI's `placeOrder` expects.
- `get_margin` and `estimate_charges` take a list of position/order dicts —
  see the SmartAPI docs for exact field names per instrument type
  (https://smartapi.angelone.in/docs/Margin, .../Brokerage).
- Rate limits are enforced by Angel One per endpoint; see
  https://smartapi.angelone.in/docs/RateLimit. This server does not do its
  own client-side rate limiting.
- Not affiliated with or endorsed by Angel One / Angel Broking.
