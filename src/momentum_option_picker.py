"""
Contract selection for scripts/run_momentum_paper_trader.py, matching the
"live trading plan" criteria in
Algo Trading/docs/momentum_strategy_backtest_record.html: nearest monthly
(3rd-Friday) OpEx >= config.MOMENTUM_TARGET_DTE_MIN calendar days out,
filtered to delta > MIN_DELTA / volume > MIN_VOLUME / open interest >
MIN_OPEN_INTEREST, cheapest surviving call (long-only - Variant H never
shorts, so puts are never considered).
"""
from datetime import date, timedelta

from tastytrade.instruments import OptionType

from src import config


def is_monthly_expiry(expiry_date):
    """Standard monthly options expire the 3rd Friday of the month -
    determined directly from the calendar date rather than trusting the
    SDK's own (undocumented, version-dependent) expiration-type string."""
    return expiry_date.weekday() == 4 and 15 <= expiry_date.day <= 21


def _monthly_expiries_on_or_after(chain, min_dte_days, today=None):
    today = today or date.today()
    cutoff = today + timedelta(days=min_dte_days)
    monthlies = sorted(d for d in chain.keys() if is_monthly_expiry(d))
    return [d for d in monthlies if d >= cutoff]


def pick_contract(tt_client, ticker, target_dte_min=None):
    """Returns a dict describing the cheapest call clearing the delta/
    volume/open-interest filters at the nearest qualifying monthly expiry:
        {strike, expiry (date), streamer_symbol, ask, bid, delta, theta,
         iv, open_interest, volume}
    or None if nothing qualifies at either of the first two monthly
    expiries tried (matches the doc's "fall back to the next monthly
    expiry once, otherwise skip" rule).
    """
    target_dte_min = target_dte_min if target_dte_min is not None else config.MOMENTUM_TARGET_DTE_MIN
    chain = tt_client.get_option_chain(symbol=ticker)
    candidates = _monthly_expiries_on_or_after(chain, target_dte_min)
    if not candidates:
        return None

    for expiry in candidates[:2]:
        calls = [o for o in chain[expiry] if o.option_type == OptionType.CALL]
        if not calls:
            continue
        symbol_to_option = {o.streamer_symbol: o for o in calls}
        snapshot = tt_client.get_option_market_snapshot(list(symbol_to_option.keys()))

        survivors = []
        for streamer_symbol, option in symbol_to_option.items():
            data = snapshot.get(streamer_symbol) or {}
            delta, volume, oi, ask = (
                data.get("delta"), data.get("volume"), data.get("open_interest"), data.get("ask"),
            )
            if delta is None or volume is None or oi is None or ask is None:
                continue  # data unavailable for this strike this run - skip, don't guess
            if (
                delta > config.MOMENTUM_MIN_DELTA
                and volume > config.MOMENTUM_MIN_VOLUME
                and oi > config.MOMENTUM_MIN_OPEN_INTEREST
            ):
                survivors.append((ask, option, streamer_symbol, data))

        if not survivors:
            continue

        ask, option, streamer_symbol, data = min(survivors, key=lambda s: s[0])
        return {
            "strike": float(option.strike_price),
            "expiry": expiry,
            "streamer_symbol": streamer_symbol,
            "ask": ask,
            "bid": data.get("bid"),
            "delta": data.get("delta"),
            "theta": data.get("theta"),
            "iv": data.get("iv"),
            "open_interest": data.get("open_interest"),
            "volume": data.get("volume"),
        }

    return None
