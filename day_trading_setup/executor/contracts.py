"""Contract choice and sizing - pure functions, no broker calls."""
import math
from datetime import date, datetime, timedelta

from day_trading_setup.executor import settings


def week_friday(today: date) -> date:
    return today + timedelta(days=(4 - today.weekday()) % 7)


def pick_expiry(expiries, today: date, now_hm=None):
    """This week's last listed expiry on or before Friday (normally Friday itself;
    Thursday when Friday is a market holiday), including today (0DTE on Friday).
    Falls back to the nearest expiry after today if the ticker has no weekly
    this week. Returns None if nothing is listed.

    now_hm: (hour, minute) New York time. If this week's expiry is today and the
    time is outside ZERO_DTE_WINDOW (open to 12:00), next week's expiry is used
    instead: in the afternoon a same-day option has little left to run for."""
    friday = week_friday(today)
    this_week = sorted(d for d in expiries if today <= d <= friday)
    if this_week:
        pick = this_week[-1]
        start, end = settings.ZERO_DTE_WINDOW
        if pick == today and now_hm is not None and not (start <= now_hm < end):
            nxt = week_friday(today + timedelta(days=7))
            next_week = sorted(d for d in expiries if friday < d <= nxt)
            if next_week:
                return next_week[-1]
        return pick
    later = sorted(d for d in expiries if d >= today)
    return later[0] if later else None


def strikes_by_distance(strikes, price: float, limit: int):
    """Strikes ordered nearest-first to the TradingView entry price."""
    return sorted(set(strikes), key=lambda k: (abs(k - price), k))[:limit]


def liquid(quote) -> bool:
    if not quote:
        return False
    bid, ask, oi, vol = quote.get("bid"), quote.get("ask"), quote.get("open_interest"), quote.get("volume")
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return False
    mid = (bid + ask) / 2
    if mid < settings.MIN_OPTION_PRICE:
        return False
    if (ask - bid) / mid > settings.MAX_SPREAD_FRACTION:
        return False
    if vol is None or not vol >= settings.MIN_OPTION_VOLUME:     # also rejects NaN
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

    quotes: {strike: {"bid", "ask", "delta", "open_interest", "volume"}} for the ladder and the
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
    return None, 0, f"no liquid strike at or out of the money (price >= ${settings.MIN_OPTION_PRICE:.2f}, spread <= {settings.MAX_SPREAD_FRACTION:.0%}, OI >= {settings.MIN_OPEN_INTEREST}, volume >= {settings.MIN_OPTION_VOLUME})", None


def loss_streak(closed, day) -> int:
    """Stop-outs in a row at the end of `day`'s closed trades (in closing order). A loss is
    a trade that never reached 2R and closed on a stop or a time stop; anything else resets."""
    streak = 0
    for t in sorted((t for t in closed if (t.get("closed_at") or "")[:10] == day.isoformat()), key=lambda t: t["closed_at"]):
        why = t.get("close_reason") or ""
        lost = not t.get("trimmed") and ("STOP" in why or "time stop" in why)
        streak = streak + 1 if lost else 0
    return streak


def swing_entry(t) -> bool:
    """True for an entry at `t` that would be a Friday swing (FRIDAY_SWING_FROM onwards,
    next week's expiry)."""
    return t.weekday() == 4 and (t.hour, t.minute) >= settings.FRIDAY_SWING_FROM


def weekend_swing(pos) -> bool:
    """True for a position opened as a Friday swing: held over the weekend even before 2R."""
    return swing_entry(datetime.fromisoformat(pos["opened_at"]))


def late_entry(t) -> bool:
    """True for an entry at `t` from ENTRY_CUTOFF on (Monday-Thursday; Fridays stop at
    FRIDAY_ENTRY_END): taken only as a LATE_ENTRY_GRADES overnight hold or on strong
    market momentum (market_momentum)."""
    return t.weekday() < 4 and (t.hour, t.minute) >= settings.ENTRY_CUTOFF


def market_momentum(sig) -> bool:
    """True if the entry alert's QQQ tag shows the market trending the trade's way: QQQ at
    least MOMENTUM_DAY_PCT from the open and MOMENTUM_HOUR_PCT over the last hour in the
    trade's direction, on that side of its VWAP. False without the tag (before v15.0)."""
    if not sig.qqq:
        return False
    day, hour, above = sig.qqq
    sgn = 1 if sig.side == "LONG" else -1
    return (sgn * day >= settings.MOMENTUM_DAY_PCT and sgn * hour >= settings.MOMENTUM_HOUR_PCT
            and above == (sgn > 0))


def close_before_overnight(pos) -> bool:
    """True if the 15:56 sweep should sell this position rather than hold it overnight:
    it has not reached 2R (runners past 2R, stop at break-even, are held) and is neither
    a Friday-afternoon swing nor a high-conviction late entry (pos["late"] == "overnight")."""
    return (settings.HOLD_OVERNIGHT_ONLY_PAST_2R and not pos.get("trimmed")
            and not weekend_swing(pos) and pos.get("late") != "overnight")


def strong_close_holds(pos, strong, now) -> bool:
    """True if today's STRONG CLOSE alert for this ticker (`strong`: {"side", "t"} or None)
    matches the position's direction: the 15:56 sweep holds it overnight (rule C)."""
    return bool(settings.STRONG_CLOSE_HOLD and strong and strong.get("side") == pos.get("side")
                and strong.get("t", "")[:10] == now.date().isoformat())


def next_session(day: date) -> date:
    """The next weekday after `day` (market holidays not excluded)."""
    d = day + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def earnings_before_next_session(report_date, time_of_day, today: date) -> bool:
    """True if a report on `report_date` (time_of_day "BMO" / "AMC" / None) lands between
    today's close and the next session's open, so holding overnight would carry it."""
    if not settings.NO_OVERNIGHT_INTO_EARNINGS or report_date is None:
        return False
    if report_date == today:
        return time_of_day != "BMO"              # BMO today: already out this morning
    return today < report_date <= next_session(today) and time_of_day != "AMC"


def time_stop_due(pos, now) -> bool:
    """True for a same-day (0DTE) position that has not reached 2R within
    ZERO_DTE_TIME_STOP_MIN minutes of entry."""
    opened = datetime.fromisoformat(pos["opened_at"])
    if pos.get("trimmed") or date.fromisoformat(pos["expiry"]) != opened.date():
        return False
    return (now - opened).total_seconds() >= settings.ZERO_DTE_TIME_STOP_MIN * 60


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
