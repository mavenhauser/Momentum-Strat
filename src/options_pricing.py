import math
from datetime import time as dtime
from zoneinfo import ZoneInfo

from src import config

EASTERN = ZoneInfo("America/New_York")
SESSION_CLOSE = dtime(16, 0)


def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def black_scholes_price(spot, strike, years_to_expiry, right, sigma=None, r=None):
    """European option theoretical price. right: 'C' or 'P'."""
    sigma = config.ASSUMED_IV if sigma is None else sigma
    r = config.RISK_FREE_RATE if r is None else r

    if years_to_expiry <= 0:
        return max(spot - strike, 0.0) if right == "C" else max(strike - spot, 0.0)

    sqrt_t = math.sqrt(years_to_expiry)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma ** 2) * years_to_expiry) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t

    if right == "C":
        return spot * _norm_cdf(d1) - strike * math.exp(-r * years_to_expiry) * _norm_cdf(d2)
    return strike * math.exp(-r * years_to_expiry) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def years_to_session_close(bar_time):
    """Fraction of a trading year remaining until today's 4pm ET close (0DTE)."""
    bar_time_et = bar_time.astimezone(EASTERN)
    close_et = bar_time_et.replace(
        hour=SESSION_CLOSE.hour, minute=SESSION_CLOSE.minute, second=0, microsecond=0
    )
    minutes_remaining = max((close_et - bar_time_et).total_seconds() / 60.0, 0.0)
    return minutes_remaining / (config.TRADING_MINUTES_PER_DAY * config.TRADING_DAYS_PER_YEAR)


def pick_strike(spot, years_to_expiry, right, premium_range, strike_increment=None, sigma=None):
    """Walk strikes away from ATM (OTM direction) to find one whose BS premium
    fits premium_range. Returns (strike, premium) for the best match found, or
    None if nothing reasonable was found.
    """
    strike_increment = strike_increment or config.STRIKE_INCREMENT
    lo, hi = premium_range
    mid = (lo + hi) / 2
    base_strike = round(spot / strike_increment) * strike_increment
    step = 1 if right == "C" else -1

    best = None
    for offset in range(0, 400):
        strike = base_strike + step * offset * strike_increment
        if strike <= 0:
            break
        premium = black_scholes_price(spot, strike, years_to_expiry, right, sigma=sigma)
        if lo <= premium <= hi:
            return strike, premium
        if best is None or abs(premium - mid) < abs(best[1] - mid):
            best = (strike, premium)
        if offset > 0 and premium < lo * 0.05:
            break  # premiums have collapsed well below range, further OTM won't help
    return best


def select_strike_by_quote(tt_client, spot, right, expiry, premium_range, strike_increment=None, symbol="SPX"):
    """Live-quote counterpart to pick_strike: walk strikes OTM from ATM using
    a real Tastytrade premium (tt_client.get_quote) instead of a
    Black-Scholes estimate, until one lands in premium_range. This is the
    one shared strike-selection rule used by all three strategies (each
    calling it with config.PREMIUM_RANGE). Returns (strike, quote) or None
    if nothing in range was found."""
    strike_increment = strike_increment or config.STRIKE_INCREMENT
    lo, hi = premium_range
    base_strike = round(spot / strike_increment) * strike_increment
    step = strike_increment if right == "C" else -strike_increment

    for offset in range(400):
        strike = base_strike + step * offset
        if strike <= 0:
            break
        quote = tt_client.get_quote(strike, right, expiry, symbol=symbol)
        if quote is None or quote["mark"] is None:
            continue
        if lo <= quote["mark"] <= hi:
            return strike, quote
    return None


def suggest_strikes(spot, right, min_distance=None, strike_increment=None, count=None):
    """Suggest `count` round-number OTM strikes at least min_distance points
    away from spot - for display (e.g. Telegram alerts), independent of any
    premium target.
    """
    min_distance = config.SUGGESTION_MIN_DISTANCE if min_distance is None else min_distance
    strike_increment = config.SUGGESTION_STRIKE_INCREMENT if strike_increment is None else strike_increment
    count = config.SUGGESTION_COUNT if count is None else count

    if right == "C":
        first = math.ceil((spot + min_distance) / strike_increment) * strike_increment
        step = strike_increment
    else:
        first = math.floor((spot - min_distance) / strike_increment) * strike_increment
        step = -strike_increment

    return [first + i * step for i in range(count)]
