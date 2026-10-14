"""Contract choice and sizing - pure functions, no broker calls."""
import math
from datetime import date, timedelta

from day_trading_setup.executor import settings


def week_friday(today: date) -> date:
    return today + timedelta(days=(4 - today.weekday()) % 7)


def pick_expiry(expiries, today: date):
    """This week's last listed expiry on or before Friday (normally Friday itself;
    Thursday when Friday is a market holiday), including today (0DTE on Friday).
    Falls back to the nearest expiry after today if the ticker has no weekly
    this week. Returns None if nothing is listed."""
    friday = week_friday(today)
    this_week = sorted(d for d in expiries if today <= d <= friday)
    if this_week:
        return this_week[-1]
    later = sorted(d for d in expiries if d >= today)
    return later[0] if later else None


def strikes_by_distance(strikes, price: float, limit: int):
    """Strikes ordered nearest-first to the TradingView entry price."""
    return sorted(set(strikes), key=lambda k: (abs(k - price), k))[:limit]


def liquid(quote) -> bool:
    if not quote:
        return False
    bid, ask, oi = quote.get("bid"), quote.get("ask"), quote.get("open_interest")
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return False
    mid = (bid + ask) / 2
    if (ask - bid) / mid > settings.MAX_SPREAD_FRACTION:
        return False
    return oi is None or oi >= settings.MIN_OPEN_INTEREST


def contracts_for(nlv: float, premium: float) -> int:
    """Whole contracts whose total premium is about RISK_FRACTION of the account."""
    if premium is None or premium <= 0:
        return 0
    n = math.floor(nlv * settings.RISK_FRACTION / (premium * 100))
    return max(n, settings.MIN_CONTRACTS)


def trim_quantity(open_qty: int) -> int:
    """Contracts to sell at the 2R alert: half rounded down; a single contract whole."""
    if open_qty <= 0:
        return 0
    if open_qty == 1:
        return 1
    return max(1, math.floor(open_qty * settings.TRIM_FRACTION))


def sessions_held(entry_day: date, today: date) -> int:
    """Weekday sessions from the entry day to today, inclusive (holidays not excluded:
    used only by the backup sweep, TradingView's TIME alert is the primary exit)."""
    if today < entry_day:
        return 0
    n, d = 0, entry_day
    while d <= today:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n
