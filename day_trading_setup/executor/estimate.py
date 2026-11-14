"""Estimate an option's price now from a delayed quote (user decision, 2026-11-14).

This account's option quotes are about 15 minutes old. The stock's quote on the same
feed is delayed the same way, so the delayed option mid and the delayed stock price
belong together: the option's implied volatility is read from that pair, and the option
is repriced with Black-Scholes at the live stock price from the TradingView alert.
Delta alone under-adjusts big moves (AMD Nov 13: delta gave 5.43, this gives 6.54,
traded 7.28) because delta itself grows as the stock moves.
"""
import math
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
RATE = 0.04
MIN_YEARS = 1 / (365 * 24 * 60)


def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def years_to(expiry, t):
    """Years from t to the 16:00 New York close on the expiry date."""
    end = datetime.combine(expiry, time(16, 0), tzinfo=NY)
    return max((end - t).total_seconds() / (365 * 86400), MIN_YEARS)


def bs_price(s, k, years, vol, right):
    years = max(years, MIN_YEARS)
    vol = max(vol, 0.01)
    sd = vol * math.sqrt(years)
    d1 = (math.log(s / k) + (RATE + vol * vol / 2) * years) / sd
    d2 = d1 - sd
    call = s * _ncdf(d1) - k * math.exp(-RATE * years) * _ncdf(d2)
    return max(call if right == "C" else call - s + k * math.exp(-RATE * years), 0.0)


def bs_delta(s, k, years, vol, right):
    years = max(years, MIN_YEARS)
    vol = max(vol, 0.01)
    d1 = (math.log(s / k) + (RATE + vol * vol / 2) * years) / (vol * math.sqrt(years))
    return _ncdf(d1) if right == "C" else _ncdf(d1) - 1


def implied_vol(price, s, k, years, right):
    """Volatility that gives `price`, or None when the price is outside what any
    volatility can give (e.g. below intrinsic value)."""
    lo, hi = 0.01, 8.0
    if not (bs_price(s, k, years, lo, right) < price < bs_price(s, k, years, hi, right)):
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if bs_price(s, k, years, mid, right) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def price_now(mid, stock_then, stock_now, strike, expiry, right, t, delta=None):
    """The option's estimated price at the live stock price, or None if there's nothing
    to go on. Falls back to mid + delta x the stock's move when no volatility fits."""
    if not mid or mid <= 0 or not stock_then or not stock_now:
        return None
    yrs = years_to(expiry, t)
    # the delayed quote is ~15 minutes older, so it had ~15 minutes more to expiry
    vol = implied_vol(mid, stock_then, strike, yrs + 15 / (365 * 24 * 60), right)
    if vol is not None:
        return round(bs_price(stock_now, strike, yrs, vol, right), 2)
    if delta is not None:
        return round(max(mid + delta * (stock_now - stock_then), 0.01), 2)
    return None


def shift_quote(q, stock_then, stock_now, strike, expiry, right, t):
    """A copy of a quote dict (bid / ask / delta ...) moved to the live stock price: the
    estimated mid with the delayed quote's spread kept, and delta at the live price.
    Unchanged (with "adjusted": False) when it can't be estimated."""
    q = dict(q or {})
    bid, ask = q.get("bid"), q.get("ask")
    if bid is None or ask is None or ask <= 0:
        q["adjusted"] = False
        return q
    mid = (bid + ask) / 2
    est = price_now(mid, stock_then, stock_now, strike, expiry, right, t, q.get("delta"))
    if est is None:
        q["adjusted"] = False
        return q
    half = (ask - bid) / 2
    q.update(bid=round(max(est - half, 0.0), 2), ask=round(est + half, 2), delayed_bid=bid, delayed_ask=ask, adjusted=True)
    vol = implied_vol(mid, stock_then, strike, years_to(expiry, t) + 15 / (365 * 24 * 60), right)
    if vol is not None:
        q["delta"] = bs_delta(stock_now, strike, years_to(expiry, t), vol, right)
    return q
