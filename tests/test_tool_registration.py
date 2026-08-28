"""
Offline smoke test: verifies the MCP server registers the expected tools
without ever hitting the network or requiring real Angel One credentials.

Run:
    python -m pytest tests/test_tool_registration.py -v
or just:
    python tests/test_tool_registration.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Dummy credentials so AngelOneClient() doesn't raise at import time. No
# network call happens just from constructing the client/server - it's all
# lazy. Only set vars that are actually unset, and restore them afterwards
# so these dummies can't leak into other test modules (e.g.
# test_readonly_live.py) sharing this interpreter under the same pytest run.
_DUMMY_ENV = {
    "ANGELONE_API_KEY": "dummy",
    "ANGELONE_CLIENT_CODE": "dummy",
    "ANGELONE_PIN": "0000",
    "ANGELONE_TOTP_SECRET": "JBSWY3DPEHPK3PXP",  # arbitrary valid base32
}
_prior_env = {k: os.environ.get(k) for k in _DUMMY_ENV}
_set_by_us = [k for k, v in _DUMMY_ENV.items() if k not in os.environ]
os.environ.update({k: v for k, v in _DUMMY_ENV.items() if k not in os.environ})

from angelone_mcp.server import mcp  # noqa: E402

for _k in _set_by_us:
    os.environ.pop(_k, None)
del _prior_env, _set_by_us

# Tools that mutate account/order state - this test suite must never call these.
MUTATING_TOOLS = {
    "place_order",
    "modify_order",
    "cancel_order",
    "gtt_create_rule",
    "gtt_modify_rule",
    "gtt_cancel_rule",
    "convert_position",
    "logout",
}

EXPECTED_READ_ONLY_TOOLS = {
    "login",  # authenticates only, doesn't touch orders/positions
    "get_profile",
    "get_order_book",
    "get_trade_book",
    "get_individual_order_details",
    "get_positions",
    "get_holdings",
    "get_all_holdings",
    "get_rms_limit",
    "gtt_details",
    "gtt_list",
    "get_ltp",
    "get_market_quote",
    "search_scrip",
    "get_candle_data",
    "get_oi_data",
    "get_option_greeks",
    "get_gainers_losers",
    "get_put_call_ratio",
    "get_oi_buildup",
    "get_nse_intraday_data",
    "get_bse_intraday_data",
    "get_margin",
    "estimate_charges",
}


def test_all_expected_tools_are_registered():
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}

    missing = EXPECTED_READ_ONLY_TOOLS - names
    assert not missing, f"expected read-only tools missing from server: {missing}"

    missing_mutating = MUTATING_TOOLS - names
    assert not missing_mutating, f"expected mutating tools missing from server: {missing_mutating}"

    print(f"{len(names)} tools registered ({len(EXPECTED_READ_ONLY_TOOLS)} read-only, "
          f"{len(MUTATING_TOOLS)} mutating) - all present")


def test_no_unexpected_new_tools():
    """Fails loudly if someone adds a tool and forgets to classify it above -
    forces a conscious read-only vs mutating decision for every new tool."""
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    known = EXPECTED_READ_ONLY_TOOLS | MUTATING_TOOLS
    unclassified = names - known
    assert not unclassified, (
        f"unclassified tool(s) found: {unclassified} - add them to either "
        f"EXPECTED_READ_ONLY_TOOLS or MUTATING_TOOLS in this test file"
    )


if __name__ == "__main__":
    test_all_expected_tools_are_registered()
    test_no_unexpected_new_tools()
    print("OK")
