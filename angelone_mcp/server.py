"""
MCP server for Angel One's SmartAPI.

Exposes trading, portfolio, market-data, GTT, and margin/brokerage
operations as MCP tools. Authentication (TOTP login + auto re-login on
expiry) is handled transparently by AngelOneClient - callers never need
to pass tokens around.

On startup, main() calls client.restore_session() to reuse a session
persisted by a previous run (verified with a real getProfile call before
being trusted; falls back to a fresh login otherwise) - see
AngelOneClient's docstring in client.py for the ANGELONE_SESSION_* env vars
that control this.

Run:
    python -m angelone_mcp.server

Required env vars: ANGELONE_API_KEY, ANGELONE_CLIENT_CODE, ANGELONE_PIN,
ANGELONE_TOTP_SECRET  (see README.md)
"""

import json
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from .client import AngelOneAPIError, AngelOneClient

mcp = FastMCP("angelone-smartapi")
client = AngelOneClient()


def _fmt(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)


def _safe(fn, *args, **kwargs) -> str:
    try:
        return _fmt(fn(*args, **kwargs))
    except AngelOneAPIError as e:
        return _fmt({"status": False, "error": str(e)})


# --------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------- #

@mcp.tool()
def login() -> str:
    """Explicitly (re)authenticate with Angel One using the configured
    client code, PIN, and TOTP secret. Normally not needed - the client
    logs in automatically on first use - but useful to force a fresh
    session or verify credentials are configured correctly."""
    return _safe(client.login)


@mcp.tool()
def logout() -> str:
    """Terminate the current Angel One trading session."""
    return _safe(client.logout)


@mcp.tool()
def get_profile() -> str:
    """Get the logged-in user's profile: client code, name, email, exchanges
    enabled, products enabled, and broker."""
    return _safe(client.get_profile)


# --------------------------------------------------------------------- #
# Orders
# --------------------------------------------------------------------- #

@mcp.tool()
def place_order(
    variety: str,
    tradingsymbol: str,
    symboltoken: str,
    transactiontype: str,
    exchange: str,
    ordertype: str,
    producttype: str,
    quantity: str,
    duration: str = "DAY",
    price: Optional[str] = "0",
    triggerprice: Optional[str] = None,
    squareoff: Optional[str] = None,
    stoploss: Optional[str] = None,
    trailingStopLoss: Optional[str] = None,
    disclosedquantity: Optional[str] = None,
    ordertag: Optional[str] = None,
) -> str:
    """Place an order.

    variety: NORMAL | STOPLOSS | AMO | ROBO
    transactiontype: BUY | SELL
    exchange: NSE | BSE | NFO | MCX | BFO | CDS
    ordertype: MARKET | LIMIT | STOPLOSS_LIMIT | STOPLOSS_MARKET
    producttype: DELIVERY | CARRYFORWARD | MARGIN | INTRADAY | BO
    duration: DAY | IOC
    price/triggerprice: required for LIMIT/STOPLOSS order types (as strings, e.g. "199.50")
    squareoff/stoploss/trailingStopLoss: only used when variety=ROBO (bracket order)
    """
    return _safe(
        client.place_order,
        variety=variety,
        tradingsymbol=tradingsymbol,
        symboltoken=symboltoken,
        transactiontype=transactiontype,
        exchange=exchange,
        ordertype=ordertype,
        producttype=producttype,
        duration=duration,
        price=price,
        triggerprice=triggerprice,
        squareoff=squareoff,
        stoploss=stoploss,
        trailingStopLoss=trailingStopLoss,
        quantity=quantity,
        disclosedquantity=disclosedquantity,
        ordertag=ordertag,
    )


@mcp.tool()
def modify_order(
    variety: str,
    orderid: str,
    ordertype: str,
    producttype: str,
    duration: str,
    quantity: str,
    tradingsymbol: str,
    symboltoken: str,
    exchange: str,
    price: Optional[str] = None,
    triggerprice: Optional[str] = None,
) -> str:
    """Modify an existing open order. All identifying fields (tradingsymbol,
    symboltoken, exchange) must match the original order."""
    return _safe(
        client.modify_order,
        variety=variety,
        orderid=orderid,
        ordertype=ordertype,
        producttype=producttype,
        duration=duration,
        price=price,
        triggerprice=triggerprice,
        quantity=quantity,
        tradingsymbol=tradingsymbol,
        symboltoken=symboltoken,
        exchange=exchange,
    )


@mcp.tool()
def cancel_order(variety: str, order_id: str) -> str:
    """Cancel an open order by its order id. variety: NORMAL | STOPLOSS | AMO | ROBO"""
    return _safe(client.cancel_order, variety, order_id)


@mcp.tool()
def get_order_book() -> str:
    """Get all orders placed today, with their current status."""
    return _safe(client.order_book)


@mcp.tool()
def get_trade_book() -> str:
    """Get all executed trades for the day."""
    return _safe(client.trade_book)


@mcp.tool()
def get_individual_order_details(unique_order_id: str) -> str:
    """Get full lifecycle detail/history for a single order by its unique order id."""
    return _safe(client.individual_order_details, unique_order_id)


# --------------------------------------------------------------------- #
# Portfolio / positions / funds
# --------------------------------------------------------------------- #

@mcp.tool()
def get_positions() -> str:
    """Get the day's open and net positions (intraday + carryforward)."""
    return _safe(client.position)


@mcp.tool()
def get_holdings() -> str:
    """Get the equity holdings currently in the demat account."""
    return _safe(client.holding)


@mcp.tool()
def get_all_holdings() -> str:
    """Get holdings plus a portfolio-level summary (total investment, current value, P&L)."""
    return _safe(client.all_holding)


@mcp.tool()
def get_rms_limit() -> str:
    """Get available margin / funds (RMS limits): net cash, available margin,
    utilised margin, etc."""
    return _safe(client.rms_limit)


@mcp.tool()
def convert_position(
    exchange: str,
    symboltoken: str,
    tradingsymbol: str,
    transactiontype: str,
    quantity: str,
    oldproducttype: str,
    newproducttype: str,
    type: str = "DAY",
) -> str:
    """Convert a position from one product type to another (e.g. INTRADAY -> DELIVERY)."""
    return _safe(
        client.convert_position,
        exchange=exchange,
        symboltoken=symboltoken,
        tradingsymbol=tradingsymbol,
        transactiontype=transactiontype,
        quantity=quantity,
        oldproducttype=oldproducttype,
        newproducttype=newproducttype,
        type=type,
    )


# --------------------------------------------------------------------- #
# GTT (Good Till Triggered) rules
# --------------------------------------------------------------------- #

@mcp.tool()
def gtt_create_rule(
    tradingsymbol: str,
    symboltoken: str,
    exchange: str,
    producttype: str,
    transactiontype: str,
    price: float,
    qty: int,
    triggerprice: float,
    disclosedqty: Optional[int] = None,
    timeperiod: Optional[int] = 365,
) -> str:
    """Create a GTT (Good Till Triggered) rule that auto-places an order when
    the trigger price is hit."""
    return _safe(
        client.gtt_create_rule,
        tradingsymbol=tradingsymbol,
        symboltoken=symboltoken,
        exchange=exchange,
        producttype=producttype,
        transactiontype=transactiontype,
        price=price,
        qty=qty,
        disclosedqty=disclosedqty,
        triggerprice=triggerprice,
        timeperiod=timeperiod,
    )


@mcp.tool()
def gtt_modify_rule(
    id: str,
    symboltoken: str,
    exchange: str,
    tradingsymbol: str,
    qty: int,
    price: float,
    triggerprice: float,
    disclosedqty: Optional[int] = None,
    timeperiod: Optional[int] = 365,
) -> str:
    """Modify an existing GTT rule."""
    return _safe(
        client.gtt_modify_rule,
        id=id,
        symboltoken=symboltoken,
        exchange=exchange,
        tradingsymbol=tradingsymbol,
        qty=qty,
        price=price,
        disclosedqty=disclosedqty,
        triggerprice=triggerprice,
        timeperiod=timeperiod,
    )


@mcp.tool()
def gtt_cancel_rule(id: str, symboltoken: str, exchange: str) -> str:
    """Cancel a GTT rule by its id."""
    return _safe(client.gtt_cancel_rule, id=id, symboltoken=symboltoken, exchange=exchange)


@mcp.tool()
def gtt_details(rule_id: str) -> str:
    """Get details of a single GTT rule by id."""
    return _safe(client.gtt_details, rule_id)


@mcp.tool()
def gtt_list(status: list[str], page: int = 1, count: int = 25) -> str:
    """List GTT rules. status is a list of any of: NEW, CANCELLED, ACTIVE,
    SENTTOEXCHANGE, FORALL, REJECTED, EXPIRED, DELETED."""
    return _safe(client.gtt_list, status, page, count)


# --------------------------------------------------------------------- #
# Market data
# --------------------------------------------------------------------- #

@mcp.tool()
def get_ltp(exchange: str, tradingsymbol: str, symboltoken: str) -> str:
    """Get the last traded price (LTP) for a single instrument."""
    return _safe(client.ltp_data, exchange, tradingsymbol, symboltoken)


@mcp.tool()
def get_market_quote(mode: str, exchange_tokens: dict[str, list[str]]) -> str:
    """Get market quotes for up to 50 instruments per exchange in one call.

    mode: LTP | OHLC | FULL
    exchange_tokens: e.g. {"NSE": ["3045", "881"], "NFO": ["58662"]} - a map of
    exchange -> list of symbol tokens.
    """
    return _safe(client.market_data, mode, exchange_tokens)


@mcp.tool()
def search_scrip(exchange: str, searchscrip: str) -> str:
    """Search for the tradingsymbol and symboltoken of an instrument by name,
    e.g. exchange='NSE', searchscrip='INFY'. Use this to resolve symboltoken
    before placing orders or requesting quotes."""
    return _safe(client.search_scrip, exchange, searchscrip)


@mcp.tool()
def get_candle_data(
    exchange: str,
    symboltoken: str,
    interval: str,
    fromdate: str,
    todate: str,
) -> str:
    """Get historical OHLCV candle data.

    interval: ONE_MINUTE | THREE_MINUTE | FIVE_MINUTE | TEN_MINUTE | FIFTEEN_MINUTE |
      THIRTY_MINUTE | ONE_HOUR | ONE_DAY
    fromdate/todate format: "YYYY-MM-DD HH:MM" (e.g. "2024-01-01 09:15")
    """
    return _safe(
        client.candle_data,
        exchange=exchange,
        symboltoken=symboltoken,
        interval=interval,
        fromdate=fromdate,
        todate=todate,
    )


@mcp.tool()
def get_oi_data(
    exchange: str,
    symboltoken: str,
    interval: str,
    fromdate: str,
    todate: str,
) -> str:
    """Get historical open-interest (OI) data for F&O instruments. Same
    interval/date format as get_candle_data."""
    return _safe(
        client.oi_data,
        exchange=exchange,
        symboltoken=symboltoken,
        interval=interval,
        fromdate=fromdate,
        todate=todate,
    )


@mcp.tool()
def get_option_greeks(name: str, expirydate: str) -> str:
    """Get option greeks (delta, gamma, theta, vega, IV) for all strikes of an
    underlying. name: e.g. 'NIFTY'. expirydate format: '25MAR2024'."""
    return _safe(client.option_greek, name, expirydate)


@mcp.tool()
def get_gainers_losers(datatype: str, expirytype: str) -> str:
    """Get top F&O gainers/losers.

    datatype: PercPriceGainers | PercPriceLosers | PercOIGainers | PercOILosers
    expirytype: NEAR | NEXT | FAR
    """
    return _safe(client.gainers_losers, datatype, expirytype)


@mcp.tool()
def get_put_call_ratio() -> str:
    """Get the current put-call ratio (PCR) across index/stock option contracts."""
    return _safe(client.put_call_ratio)


@mcp.tool()
def get_oi_buildup(datatype: str, expirytype: str) -> str:
    """Get open-interest buildup data (long/short buildup, unwinding, etc.)
    for F&O contracts.

    datatype: Long Built Up | Short Built Up | Short Covering | Long Unwinding
    expirytype: NEAR | NEXT | FAR
    """
    return _safe(client.oi_buildup, datatype, expirytype)


@mcp.tool()
def get_nse_intraday_data() -> str:
    """Get NSE intraday most-active-by-volume/value data."""
    return _safe(client.nse_intraday)


@mcp.tool()
def get_bse_intraday_data() -> str:
    """Get BSE intraday most-active-by-volume/value data."""
    return _safe(client.bse_intraday)


# --------------------------------------------------------------------- #
# Margin & brokerage
# --------------------------------------------------------------------- #

@mcp.tool()
def get_margin(positions: list[dict]) -> str:
    """Calculate span + exposure margin required for a basket of positions
    before placing them.

    Each position dict needs: exchange, qty, price, productType, token,
    tradeType (BUY/SELL), orderType.
    """
    return _safe(client.margin_api, positions)


@mcp.tool()
def estimate_charges(orders: list[dict]) -> str:
    """Estimate brokerage and other charges for a basket of prospective orders.

    Each order dict needs: product_type, transaction_type, quantity, price,
    exchange, symbol_name, token.
    """
    return _safe(client.estimate_charges, orders)


def main() -> None:
    # Reuse a session persisted by a previous run if one exists, verifying it
    # with a real getProfile call and falling back to a fresh login if it's
    # missing/expired/invalid. Deliberately not done at import time (e.g. in
    # AngelOneClient.__init__ or at module scope above) so that importing
    # this module - as tests that only need mcp.list_tools() do - never
    # makes a network call.
    client.restore_session()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
