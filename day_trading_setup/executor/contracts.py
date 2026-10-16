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


def size_fraction(grade):
    """Fraction of the account to spend on premium for an entry of this grade."""
    return settings.SIZE_BY_GRADE.get(grade or "", settings.SIZE_DEFAULT)


def contracts_for(nlv: float, premium: float, fraction: float = None) -> int:
    """Whole contracts whose total premium is about `fraction` of the account."""
    if premium is None or premium <= 0:
        return 0
    n = math.floor(nlv * (fraction or settings.SIZE_DEFAULT) / (premium * 100))
    return max(n, settings.MIN_CONTRACTS)


def trim_quantity(open_qty: int) -> int:
    """Contracts to sell at the 2R alert: half rounded down. A single contract is
    held (0) so it can ride the trail, like the other half of a larger position."""
    if open_qty <= 1:
        return 0
    return max(1, math.floor(open_qty * settings.TRIM_FRACTION))


def strike_ladder(strikes, entry: float, right: str, n_otm: int):
    """At-the-money strike, then strikes further out of the money (higher for
    calls, lower for puts), nearest first. Never in the money (2026-10-16): the
    first rung is the nearest strike at or out of the money, where a strike that
    is in the money by no more than ATM_TOLERANCE_PCT of the price counts as at
    the money."""
    tol = entry * settings.ATM_TOLERANCE_PCT / 100
    if right == "C":
        ok = sorted(k for k in set(strikes) if k >= entry - tol)
    else:
        ok = sorted((k for k in set(strikes) if k <= entry + tol), reverse=True)
    return ok[:n_otm + 1]


def stop_pct(entry: float, stop: float) -> float:
    """Distance from the entry to the TradingView stop, as a % of the entry price."""
    return abs(entry - stop) / entry * 100 if entry else 0.0


def covers_spread(quote, move) -> bool:
    """True if |delta| x the stock move to the target is at least
    MIN_TARGET_TO_SPREAD x the option's bid/ask spread (always True with no move)."""
    if not move:
        return True
    d, bid, ask = quote.get("delta"), quote.get("bid"), quote.get("ask")
    if d is None or bid is None or ask is None:
        return False
    return abs(d) * move >= settings.MIN_TARGET_TO_SPREAD * (ask - bid)


def choose_strike(quotes: dict, entry: float, right: str, budget: float, move: float = None, min_delta: float = None):
    """Pick the strike and quantity for one entry.

    quotes: {strike: {"bid", "ask", "delta", "open_interest"}} for the ladder and the
    nearest strikes. Returns (strike, qty, rule, delta) or (None, 0, reason, None).

      1. nearest-the-money strike on the ladder buying >= TARGET_CONTRACTS, delta >= MIN_DELTA
      2. else the same buying >= MIN_CONTRACTS_TO_TRIM
      3. else 1 contract of the first liquid strike on the ladder (held through 2R)

    Every candidate is on the ladder: at the money or out of the money, never in.

    move: the stock's move from entry to the 2R target. When given, every rule
    also requires the strike to pass covers_spread().
    min_delta: delta floor (default MIN_DELTA; MIN_DELTA_0DTE on expiry day).
    """
    floor = settings.MIN_DELTA if min_delta is None else min_delta
    ladder = strike_ladder(quotes.keys(), entry, right, settings.STRIKES_TO_SCAN)

    def qty_at(k):
        ask = quotes[k].get("ask")
        return math.floor(budget / (ask * 100)) if ask and ask > 0 else 0

    def ok(k):
        d = quotes[k].get("delta")
        return liquid(quotes[k]) and d is not None and abs(d) >= floor and covers_spread(quotes[k], move)

    for need, rule in ((settings.TARGET_CONTRACTS, f"{settings.TARGET_CONTRACTS}+ contracts"),
                       (settings.MIN_CONTRACTS_TO_TRIM, f"{settings.MIN_CONTRACTS_TO_TRIM}+ contracts (no strike gave {settings.TARGET_CONTRACTS} at delta {floor}+)")):
        for k in ladder:
            if ok(k) and qty_at(k) >= need:
                return k, qty_at(k), rule, quotes[k].get("delta")
    any_liquid = False
    for k in ladder:
        if liquid(quotes[k]):
            any_liquid = True
            if covers_spread(quotes[k], move):
                return k, max(1, qty_at(k)), f"1 contract (none cheap enough for 2 at delta {floor}+), held through 2R", quotes[k].get("delta")
    if any_liquid:
        return None, 0, f"2R target too small for the option spread (needs gain at target >= {settings.MIN_TARGET_TO_SPREAD:g}x spread)", None
    return None, 0, "no liquid strike at or out of the money", None


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
