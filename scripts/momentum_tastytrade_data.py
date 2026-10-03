#!/usr/bin/env python3
"""
TastyTrade market data for the momentum strategy's option picker.

Data only - never places an order, no order-execution code exists here.
Reuses the SPX Scalp project's TastyTrade account (same OAuth credentials,
copied into their own file at ~/.trading-scanners/tastytrade.env so the two
projects stay independent - momentum-strategy doesn't reach into the
Algo Trading repo at runtime).

Fetches, per candidate option contract near spot, all via the DXLink
streaming API (not the REST market-data snapshot - see note below):
  - bid / ask, via the Quote event.
  - day volume, via the Trade event's day_volume field.
  - delta / IV / theta, via the Greeks event.
  - open interest, via the Summary event.

Why streaming for everything, not the REST snapshot
(get_market_data_by_type): confirmed by direct testing that the REST
snapshot returns zero rows for real, liquid equity-option symbols (tried
multiple near-the-money NVDA strikes with real open interest, always came
back empty) - it works fine for the SPX project's use case (SPX index
options), so this looks like an equity-options-specific gap in that
particular endpoint on this account, not a genuine data-availability
problem. The streaming API returns real values for the exact same symbols,
including bid/ask, so that's what this module uses throughout instead.

Why not reuse src.tastytrade_client.TastytradeClient from the SPX project
directly: that class is 0DTE/SPX-specific (default symbol="SPX", expiry
assumed to be today) and built around the REST snapshot for bid/ask/mark
only, not greeks/OI - this strategy needs multi-week expiries and
delta/IV/OI filtering, which needs the streamer either way.
"""

import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path

from tastytrade import Session
from tastytrade.dxfeed import Greeks, Quote, Summary, Trade
from tastytrade.instruments import OptionType, get_option_chain
from tastytrade.streamer import DXLinkStreamer

TASTYTRADE_ENV_FILE = Path.home() / ".trading-scanners" / "tastytrade.env"
GREEKS_TIMEOUT_SECONDS = 8  # per contract, waiting for a streamer snapshot


def _load_credentials():
    if not TASTYTRADE_ENV_FILE.exists():
        raise RuntimeError(f"No TastyTrade credentials file at {TASTYTRADE_ENV_FILE}")
    values = {}
    for line in TASTYTRADE_ENV_FILE.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            values[k.strip()] = v.strip()
    secret, token = values.get("TT_CLIENT_SECRET"), values.get("TT_REFRESH_TOKEN")
    if not secret or not token:
        raise RuntimeError(f"TT_CLIENT_SECRET / TT_REFRESH_TOKEN missing in {TASTYTRADE_ENV_FILE}")
    return secret, token


async def _fetch_candidates_async(symbol, spot_price, expiry_date):
    """expiry_date: a date object for the target expiration (e.g. the next
    monthly OpEx). Returns a list of dicts, one per strike near spot, with
    whatever fields resolved (missing fields are None rather than raising -
    a single contract's data failing shouldn't drop every other strike)."""
    secret, token = _load_credentials()
    session = Session(provider_secret=secret, refresh_token=token)
    await session.refresh(force=True)

    chain = await get_option_chain(session, symbol)
    if expiry_date not in chain:
        available = sorted(chain.keys())
        raise RuntimeError(f"{expiry_date} not in {symbol}'s chain - available: {available[:6]}...")

    calls_near_spot = [
        opt for opt in chain[expiry_date]
        if opt.option_type == OptionType.CALL
        and spot_price * 0.85 <= float(opt.strike_price) <= spot_price * 1.25
    ]
    if not calls_near_spot:
        raise RuntimeError(f"No {symbol} {expiry_date} call strikes found near spot {spot_price}")

    streamer_symbols = [opt.streamer_symbol for opt in calls_near_spot]
    want = set(streamer_symbols)

    # All four fetched via the streamer (see module docstring for why the
    # REST snapshot isn't used at all here). Collected together in one
    # subscription pass rather than four sequential ones.
    greeks_by_symbol, oi_by_symbol, quote_by_symbol, trade_by_symbol = {}, {}, {}, {}
    async with DXLinkStreamer(session) as streamer:
        await streamer.subscribe(Greeks, streamer_symbols)
        await streamer.subscribe(Summary, streamer_symbols)
        await streamer.subscribe(Quote, streamer_symbols)
        await streamer.subscribe(Trade, streamer_symbols)

        deadline = asyncio.get_event_loop().time() + GREEKS_TIMEOUT_SECONDS
        event_sources = [
            (Greeks, greeks_by_symbol),
            (Summary, oi_by_symbol),
            (Quote, quote_by_symbol),
            (Trade, trade_by_symbol),
        ]
        while asyncio.get_event_loop().time() < deadline:
            # Delta is the one hard requirement everything else is filtered
            # against downstream, so stop as soon as every candidate has a
            # greeks reading - the other three are opportunistic within
            # whatever time that takes.
            if want <= set(greeks_by_symbol.keys()):
                break
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            for event_class, bucket in event_sources:
                try:
                    ev = await asyncio.wait_for(streamer.get_event(event_class), timeout=0.2)
                    bucket[ev.event_symbol] = ev
                except asyncio.TimeoutError:
                    continue

    results = []
    for opt in calls_near_spot:
        sym = opt.streamer_symbol
        g = greeks_by_symbol.get(sym)
        s = oi_by_symbol.get(sym)
        q = quote_by_symbol.get(sym)
        t = trade_by_symbol.get(sym)
        bid = float(q.bid_price) if q and q.bid_price is not None else None
        ask = float(q.ask_price) if q and q.ask_price is not None else None
        results.append({
            "strike": float(opt.strike_price),
            "streamer_symbol": sym,
            "delta": float(g.delta) if g and g.delta is not None else None,
            "iv": float(g.volatility) if g and g.volatility is not None else None,
            "theta": float(g.theta) if g and g.theta is not None else None,
            "volume": float(t.day_volume) if t and t.day_volume is not None else None,
            "openInterest": s.open_interest if s and s.open_interest is not None else None,
            "bid": bid,
            "ask": ask,
            "mark": (bid + ask) / 2 if bid is not None and ask is not None else None,
        })
    return results


def fetch_candidates(symbol, spot_price, expiry_date):
    """Sync wrapper - runs the async TastyTrade calls in a fresh event loop.
    Never raises for missing per-contract data (those fields are just None);
    raises only for connection/credential/chain-lookup failures, which the
    caller (momentum_option_picker.py) already wraps in a try/except."""
    return asyncio.run(_fetch_candidates_async(symbol, spot_price, expiry_date))


if __name__ == "__main__":
    import sys
    import json
    if len(sys.argv) < 3:
        print("Usage: momentum_tastytrade_data.py SYMBOL SPOT_PRICE [YYYY-MM-DD]")
        sys.exit(1)
    symbol, spot = sys.argv[1], float(sys.argv[2])
    expiry = (
        datetime.strptime(sys.argv[3], "%Y-%m-%d").date()
        if len(sys.argv) > 3
        else date.today() + timedelta(days=21)
    )
    print(json.dumps(fetch_candidates(symbol, spot, expiry), indent=2, default=str))
