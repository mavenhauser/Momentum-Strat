#!/usr/bin/env python3
"""
Momentum strategy option picker.

Given a ticker Variant H just signaled on (long-only, so always a call) and
a spot price, finds the qualifying long call contract per the user's stated
criteria and computes a suggested contract quantity sized to 1% of account
net liquidation. Never places an order - returns a suggestion dict for a
human to act on (via the enriched Telegram alert in
momentum_strategy_scanner.py, or standalone via the CLI entry point below).

Criteria:
  - Delta > 0.25
  - Volume > 500 (that day)
  - Open interest > 1000
  - IV < 100% preferred (soft flag, not a hard filter)
  - Expiration: nearest monthly OpEx (3rd Friday) that is >= MIN_DAYS_TO_EXPIRY
    (14 calendar days) out - e.g. entering early in a month, that month's own
    OpEx (~3 weeks out) is fine; no need to skip ahead to the next month.
  - Among contracts clearing delta/volume, picks the one closest to the
    MIN_DELTA (0.25) threshold itself - the cheapest/most-leveraged
    qualifying contract, not a higher target delta.

Market data (chain structure, delta/IV/theta, volume, open interest,
bid/ask) comes from TastyTrade (momentum_tastytrade_data.py) - IBKR's
market data was unreliable across repeated tests (mostly empty/NaN, one
anomalous success), so this account's IBKR connection is used only for
what it's reliably good for: the actual paper account's net liquidation
value, for position sizing. Execution stays manual either way.

Requires:
  - IBKR TWS or IB Gateway running with the API enabled (paper account,
    port 7497 by default) - for account NLV only.
  - TastyTrade OAuth credentials at ~/.trading-scanners/tastytrade.env - for
    all market data. See momentum_tastytrade_data.py for details.

Never executes trades. No order-placement code exists in this file.
"""

import sys
from datetime import datetime, timedelta
from ib_insync import IB

import momentum_tastytrade_data as tt_data

IBKR_HOST = "127.0.0.1"
IBKR_PORT = 7497  # paper TWS
IBKR_CLIENT_ID = 55

MIN_DELTA = 0.25
MIN_VOLUME = 500
MIN_OPEN_INTEREST = 1000
MAX_IV_PREFERRED = 1.00  # 100%, soft preference not hard filter
MIN_DAYS_TO_EXPIRY = 14  # nearest monthly OpEx is fine even if only ~2-3 weeks out
POSITION_RISK_PCT = 0.01  # 1% of account NLV per position


def connect():
    # ib_insync needs a "current" event loop in this thread, but the
    # TastyTrade fetch (asyncio.run(), called first in pick_for_signal)
    # tears its own loop down on exit, leaving none active - set a fresh one
    # before connecting, or ib.connect() raises "no current event loop".
    import asyncio
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    ib = IB()
    ib.connect(IBKR_HOST, IBKR_PORT, clientId=IBKR_CLIENT_ID, timeout=10)
    ib.reqMarketDataType(3)  # fall back to delayed data if live isn't subscribed
    return ib


def get_account_nlv(ib):
    accounts = ib.managedAccounts()
    if not accounts:
        raise RuntimeError("No managed accounts found on this IBKR connection")
    summary = ib.accountSummary(accounts[0])
    for s in summary:
        if s.tag == "NetLiquidation":
            return float(s.value), s.currency, accounts[0]
    raise RuntimeError("NetLiquidation not found in account summary")


def _third_friday_of_month(year, month):
    d = datetime(year, month, 1).date()
    days_to_friday = (4 - d.weekday()) % 7  # Friday = weekday 4
    return d + timedelta(days=days_to_friday + 14)


def next_opex_at_least(days_out):
    """Nearest standard monthly (3rd-Friday) expiration >= days_out from
    today. Computed directly rather than queried, since standard monthlies
    exist for essentially every optionable large-cap - e.g. entering early
    in a month, that month's own OpEx is fine, no need to jump ahead."""
    today = datetime.now().date()
    year, month = today.year, today.month
    for _ in range(6):  # 6 months of headroom is plenty
        third_friday = _third_friday_of_month(year, month)
        if (third_friday - today).days >= days_out:
            return third_friday
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return None


def pick_call_option(symbol, spot_price):
    """Returns a dict with the selected contract + diagnostics, or a dict
    with an "error" key explaining what's missing/why nothing qualified.
    Market data comes from TastyTrade (see momentum_tastytrade_data.py) -
    no IBKR involvement in this function."""
    expiry = next_opex_at_least(MIN_DAYS_TO_EXPIRY)
    if not expiry:
        return {"error": f"No monthly OpEx >= {MIN_DAYS_TO_EXPIRY} days out found for {symbol}"}

    try:
        results = tt_data.fetch_candidates(symbol, spot_price, expiry)
    except Exception as e:
        return {"error": f"TastyTrade data fetch failed for {symbol} {expiry}: {e}"}

    if not results:
        return {"error": f"No candidate strikes found for {symbol} {expiry} near spot {spot_price}"}

    qualifying = [
        r for r in results
        if r["delta"] is not None and r["delta"] > MIN_DELTA
        and (r["volume"] or 0) > MIN_VOLUME
        and (r["openInterest"] or 0) > MIN_OPEN_INTEREST
    ]
    if not qualifying:
        return {
            "error": (
                f"No contract for {symbol} {expiry} cleared delta>{MIN_DELTA} "
                f"/ vol>{MIN_VOLUME} / OI>{MIN_OPEN_INTEREST}"
            ),
            "all_candidates": results,
        }

    qualifying.sort(key=lambda r: r["delta"])  # cheapest/most-leveraged contract that still clears MIN_DELTA
    best = dict(qualifying[0])
    best["expiry"] = expiry.isoformat()
    best["symbol"] = symbol
    best["iv_flag"] = "high" if (best["iv"] or 0) > MAX_IV_PREFERRED else "ok"
    best["oi_verified"] = True  # already a hard filter above - TastyTrade OI has been reliable in testing
    return best


def suggest_quantity(contract, account_nlv):
    """1% of account NLV risked, sized off the option's ask premium. Returns
    (qty, note) - qty is None when the ask price isn't available, with a
    note explaining why (kept defensive even though TastyTrade's ask has
    been reliable in testing, unlike IBKR's)."""
    ask = contract.get("ask")
    if not ask or ask != ask or ask <= 0:  # ask != ask excludes NaN
        return None, "Quantity unavailable - live ask price didn't come through. Check the live ask manually and size for now."
    risk_amount = account_nlv * POSITION_RISK_PCT
    return max(int(risk_amount // (ask * 100)), 0), "OK"


def pick_for_signal(symbol, spot_price):
    """Entry point for the scanner to call: pulls the option pick from
    TastyTrade data, account NLV from IBKR, always returns a dict (never
    raises) so a data/connection failure can't crash the alert pipeline -
    callers should check for an "error" key."""
    result = pick_call_option(symbol, spot_price)
    if "error" in result:
        return result

    try:
        ib = connect()
    except Exception as e:
        result["error"] = f"Could not connect to IBKR TWS on {IBKR_HOST}:{IBKR_PORT} for account NLV: {e}"
        return result
    try:
        nlv, ccy, account = get_account_nlv(ib)
        qty, qty_note = suggest_quantity(result, nlv)
        result["suggested_qty"] = qty
        result["qty_note"] = qty_note
        result["account_nlv"] = nlv
        result["account_ccy"] = ccy
        result["account"] = account
        return result
    except Exception as e:
        result["error"] = f"Got option pick but IBKR account NLV lookup failed: {e}"
        return result
    finally:
        ib.disconnect()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: momentum_option_picker.py SYMBOL SPOT_PRICE")
        sys.exit(1)
    import json
    print(json.dumps(pick_for_signal(sys.argv[1], float(sys.argv[2])), indent=2, default=str))
