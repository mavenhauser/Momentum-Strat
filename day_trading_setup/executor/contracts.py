"""Contract choice and sizing - pure functions, no broker calls."""
import math
import re
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


def liquid(quote, early: bool = False) -> bool:
    """early: before EARLY_LIQUIDITY_UNTIL, when the delayed feed still shows the
    opening minute - no volume test, wider spread allowed, open interest required."""
    if not quote:
        return False
    bid, ask, oi, vol = quote.get("bid"), quote.get("ask"), quote.get("open_interest"), quote.get("volume")
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return False
    mid = (bid + ask) / 2
    if mid < settings.MIN_OPTION_PRICE:
        return False
    if (ask - bid) / mid > (settings.EARLY_MAX_SPREAD_FRACTION if early else settings.MAX_SPREAD_FRACTION):
        return False
    if early:
        return oi is not None and oi >= settings.MIN_OPEN_INTEREST
    if vol is None or not vol >= settings.MIN_OPTION_VOLUME:     # also rejects NaN
        return False
    return oi is None or oi >= settings.MIN_OPEN_INTEREST


def option_r(delta, entry, stop):
    """The option's R per contract in premium: what it loses if the stock goes
    from entry to its stop, approximated as |delta| x the stop distance (user,
    2026-11-09: 2R measured on the option, shown in notifications; trading still
    trims at the stock's 2R until the Nov 17 review). None if unknown."""
    if delta is None or entry is None or stop is None or delta != delta:
        return None
    r = abs(delta) * abs(entry - stop)
    return r if r > 0 else None


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


def choose_strike(quotes: dict, entry: float, right: str, budget: float, move: float = None, min_delta: float = None,
                  early: bool = False):
    """Pick the strike and quantity for one entry.

    quotes: {strike: {"bid", "ask", "delta", "open_interest", "volume"}} for the ladder and the
    nearest strikes. Returns (strike, qty, rule, delta) or (None, 0, reason, None).

      1. nearest-the-money strike on the ladder buying >= TARGET_CONTRACTS, delta >= MIN_DELTA
      2. else the same buying >= MIN_CONTRACTS_TO_TRIM
      3. else no trade: a single contract can't be split at 2R (user decision, 2026-11-03)

    Every candidate is on the ladder: at the money or out of the money, never in.

    move: the stock's move from entry to the 2R target. When given, every rule
    also requires the strike to pass covers_spread().
    min_delta: delta floor (default MIN_DELTA; MIN_DELTA_0DTE on expiry day).
    early: opening liquidity test (see liquid()).
    """
    floor = settings.MIN_DELTA if min_delta is None else min_delta
    ladder = strike_ladder(quotes.keys(), entry, right, settings.STRIKES_TO_SCAN)

    def qty_at(k):
        ask = quotes[k].get("ask")
        return math.floor(budget / (ask * 100)) if ask and ask > 0 else 0

    def ok(k):
        d = quotes[k].get("delta")
        return liquid(quotes[k], early) and d is not None and abs(d) >= floor and covers_spread(quotes[k], move)

    for need, rule in ((settings.TARGET_CONTRACTS, f"{settings.TARGET_CONTRACTS}+ contracts"),
                       (settings.MIN_CONTRACTS_TO_TRIM, f"{settings.MIN_CONTRACTS_TO_TRIM}+ contracts (no strike gave {settings.TARGET_CONTRACTS} at delta {floor}+)")):
        for k in ladder:
            if ok(k) and qty_at(k) >= need:
                return k, qty_at(k), rule, quotes[k].get("delta")
    candidates = [k for k in ladder if liquid(quotes[k], early)]
    if any(covers_spread(quotes[k], move) for k in candidates):
        return None, 0, f"under {settings.MIN_CONTRACTS_TO_TRIM} contracts: no liquid strike at delta {floor}+ buys {settings.MIN_CONTRACTS_TO_TRIM} within ${budget:,.0f}, and 1 can't be split at 2R", None
    if candidates:
        return None, 0, f"2R target too small for the option spread (needs gain at target >= {settings.MIN_TARGET_TO_SPREAD:g}x spread)", None
    if early:
        return None, 0, f"no liquid strike at or out of the money (opening test: price >= ${settings.MIN_OPTION_PRICE:.2f}, spread <= {settings.EARLY_MAX_SPREAD_FRACTION:.0%}, OI >= {settings.MIN_OPEN_INTEREST})", None
    return None, 0, f"no liquid strike at or out of the money (price >= ${settings.MIN_OPTION_PRICE:.2f}, spread <= {settings.MAX_SPREAD_FRACTION:.0%}, OI >= {settings.MIN_OPEN_INTEREST}, volume >= {settings.MIN_OPTION_VOLUME})", None


def loss_streak(closed, day) -> int:
    """Stop-outs in a row at the end of `day`'s closed trades (in closing order). A loss is
    a trade that never reached 2R and closed on a stop, or on a time stop at a loss (since
    2026-11-07: TSLA's +$1,884 time stop on Nov 6 counted as a stop-out); anything else resets."""
    streak = 0
    for t in sorted((t for t in closed if (t.get("closed_at") or "")[:10] == day.isoformat()), key=lambda t: t["closed_at"]):
        why = t.get("close_reason") or ""
        pnl = t.get("pnl_confirmed", t.get("pnl_usd"))
        lost = not t.get("trimmed") and ("STOP" in why or ("time stop" in why and (pnl is None or pnl < 0)))
        streak = streak + 1 if lost else 0
    return streak


def swing_entry(t) -> bool:
    """True for an entry at `t` that would be a Friday swing (FRIDAY_SWING_FROM onwards,
    next week's expiry)."""
    return t.weekday() == 4 and (t.hour, t.minute) >= settings.FRIDAY_SWING_FROM


def weekend_swing(pos) -> bool:
    """True for a position opened as a Friday swing (next week's expiry, FRIDAY_SWING_SIZE)."""
    return swing_entry(datetime.fromisoformat(pos["opened_at"]))


def late_entry(t) -> bool:
    """True for an entry at `t` from ENTRY_CUTOFF on (Monday-Thursday; Fridays stop at
    FRIDAY_ENTRY_END): taken only for a LATE_ENTRY_GRADES setup or on strong market
    momentum (market_momentum)."""
    return t.weekday() < 4 and (t.hour, t.minute) >= settings.ENTRY_CUTOFF


def half_day(day: date) -> bool:
    """True on a 13:00 ET early close (settings.HALF_DAYS): no new entries."""
    return day.isoformat() in settings.HALF_DAYS


def sweep_at(day: date) -> tuple:
    """The New York time of the day's end-of-day sweep."""
    return settings.HALF_DAY_SWEEP_AT if half_day(day) else settings.SWEEP_AT


def in_session(t) -> bool:
    """True while the market is open at `t`: no weekends or holidays, shut at 13:00 on a half day."""
    if t.weekday() >= 5 or t.date().isoformat() in settings.MARKET_HOLIDAYS:
        return False
    return (9, 30) <= (t.hour, t.minute) < (settings.HALF_DAY_CLOSE if half_day(t.date()) else (16, 0))


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


def with_qqq(sig) -> bool:
    """True if the entry alert's QQQ tag shows the market moving the trade's way: QQQ up
    from the open and above its VWAP for a long, down and below for a short. True without
    the tag (alerts before v15.0), so a missing tag never blocks a trade."""
    if not sig.qqq:
        return True
    day, _hour, above = sig.qqq
    sgn = 1 if sig.side == "LONG" else -1
    return sgn * day > 0 and above == (sgn > 0)


def prev_session(day: date) -> date:
    """The trading day before `day` (weekends and settings.MARKET_HOLIDAYS skipped)."""
    d = day - timedelta(days=1)
    while d.weekday() >= 5 or d.isoformat() in settings.MARKET_HOLIDAYS:
        d -= timedelta(days=1)
    return d


def qqq_prev_close(state, day: date):
    """QQQ's close on the trading day before `day`: the last QQQ 5-minute bar the executor got
    that day (state["qqq_close"]), else None (also when that day's bar is missing)."""
    rec = (state.get("qqq_close") or {}).get(prev_session(day).isoformat())
    return rec.get("close") if rec else None


def qqq_day_ref(state, sig, day: date):
    """QQQ_UP_ON_DAY: for an entry whose alert shows QQQ against the trade from its open, the
    previous close if QQQ is still on the trade's side of it (up on the day for a long, down for
    a short), else None. QQQ now = today's open (from the QQQ bar) moved by the alert's % from
    the open."""
    if not settings.QQQ_UP_ON_DAY or not sig.qqq:
        return None
    pct = sig.qqq[0]
    sgn = 1 if sig.side == "LONG" else -1
    if sgn * pct >= 0:
        return None
    bar = state.get("qqq_bar") or {}
    pc = qqq_prev_close(state, day)
    if not bar.get("open") or not pc or (bar.get("t") or "")[:10] != day.isoformat():
        return None
    now = bar["open"] * (1 + pct / 100)
    return pc if sgn * (now - pc) > 0 else None


def is_opening(sig) -> bool:
    """True for DTS Entry's 09:35 opening entry (v22.0): level OPEN."""
    return sig.kind == "ENTRY" and sig.level == "OPEN"


def openings_today(state, day) -> int:
    """Opening entries opened on `day`, open or closed."""
    return sum(1 for p in list(state["positions"].values()) + list(state["closed"])
               if p.get("opening") and (p.get("opened_at") or "")[:10] == day.isoformat())


def _stock_r(pos, px):
    risk = abs((pos.get("tv_entry") or 0) - (pos.get("tv_stop") or 0))
    if not risk or px is None:
        return 0.0
    return (px - pos["tv_entry"]) * (1 if pos["side"] == "LONG" else -1) / risk


def mark_price(pos, marks):
    """The latest MARK price for this position's stock, only if it came after the position
    opened (marks are kept per stock, and since v27.0 come only on 0.25R moves, so one left
    by an earlier trade in the same stock can be the newest for a while). Else None."""
    mk = (marks or {}).get(pos.get("ticker"))
    if not mk or not pos.get("opened_at"):
        return None
    try:
        if datetime.fromisoformat(mk["t"]) < datetime.fromisoformat(pos["opened_at"]):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return mk["price"]


def _sale_stock_price(pos, ev, marks):
    """Stock price behind a sale: recorded with the sale (since 2026-11-26), else the price in the
    TradingView alert that triggered it, else the latest mark, else the entry."""
    if ev.get("stock_px") is not None:
        return ev["stock_px"]
    m = re.search(r"@ ([0-9][0-9,]*(?:\.[0-9]+)?)", ev.get("alert") or "")
    if m and pos["ticker"] in (ev.get("alert") or "") and not (ev.get("alert") or "").startswith("DTS: QQQ"):
        return float(m.group(1).replace(",", ""))
    mk = mark_price(pos, marks)
    return mk if mk is not None else pos.get("tv_entry")


def trade_units(pos, marks):
    """This trade's P&L in day-rule units: stock R of each sale and of the open rest (at its
    latest mark), by the share of contracts, times its premium size / DAY_UNIT_SIZE."""
    qty = pos.get("qty") or 0
    if not qty or not pos.get("tv_entry") or not pos.get("tv_stop"):
        return 0.0
    r = 0.0
    for ev in pos.get("events", []):
        if str(ev.get("action", "")).startswith("SELL") and ev.get("qty"):
            r += ev["qty"] / qty * _stock_r(pos, _sale_stock_price(pos, ev, marks))
    if pos.get("open_qty", 0) > 0:
        mk = mark_price(pos, marks)
        r += pos["open_qty"] / qty * _stock_r(pos, mk if mk is not None else pos["tv_entry"])
    return r * (pos.get("size_fraction") or settings.DAY_UNIT_SIZE) / settings.DAY_UNIT_SIZE


def day_units(state, day, marks=None):
    """The day's P&L in units: every trade opened on `day`, open or closed."""
    marks = state.get("marks", {}) if marks is None else marks
    return sum(trade_units(p, marks) for p in list(state["positions"].values()) + list(state["closed"])
               if (p.get("opened_at") or "")[:10] == day.isoformat())


def day_rule_due(state, day, units):
    """Update the day's peak / armed flag; return the reason to stop the day, or None."""
    if not settings.DAY_RULES:
        return None
    d = state.setdefault("day_rules", {})
    if d.get("date") != day.isoformat():
        d.clear(); d.update(date=day.isoformat(), peak=units, armed=False)
    d["peak"] = max(d.get("peak", units), units)
    if units >= settings.DAY_PROTECT_ARM_U:
        d["armed"] = True
    if units <= -settings.DAY_LOSS_LIMIT_U:
        return f"daily loss limit: the day reached {units:+.1f}u (limit -{settings.DAY_LOSS_LIMIT_U:g}u)"
    if d.get("armed") and units <= settings.DAY_PROTECT_FLOOR_U:
        return (f"green day protected: up {d['peak']:+.1f}u at best, back to {units:+.1f}u "
                f"(sell at +{settings.DAY_PROTECT_FLOOR_U:g}u once up +{settings.DAY_PROTECT_ARM_U:g}u)")
    return None


def day_stopped(state, day):
    """The reason the day rules stopped `day`, or None."""
    s = state.get("day_stop") or {}
    return s.get("why") if s.get("date") == day.isoformat() else None


def qqq_open_exit_due(pos, qqq_close, qqq_open, t) -> bool:
    """True if a QQQ 5-minute close at `t` is back through QQQ's 09:30 open against this
    position (below for a long, above for a short) and the position should be sold
    (QQQ_OPEN_EXIT): opened today before that bar's close, nothing sold at 2R yet."""
    if not settings.QQQ_OPEN_EXIT or pos.get("opening") or pos.get("trimmed") or pos.get("open_qty", 0) < pos.get("qty", 0):
        return False
    opened = datetime.fromisoformat(pos["opened_at"])
    if opened.date() != t.date() or opened >= t:
        return False
    sgn = 1 if pos["side"] == "LONG" else -1
    ref = pos.get("qqq_ref") or qqq_open           # QQQ_UP_ON_DAY entries: the previous close
    return sgn * (qqq_close - ref) < 0


def close_before_overnight(pos) -> bool:
    """True if the 15:56 sweep should sell this position rather than hold it overnight:
    it has not reached 2R (runners past 2R, stop at break-even, are held). Since 2026-11-04
    a Friday-afternoon swing or a late A+ OUTSIDE entry is no exception."""
    return settings.HOLD_OVERNIGHT_ONLY_PAST_2R and not pos.get("trimmed")


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
    if not settings.ZERO_DTE_TIME_STOP_MIN:          # off since 2026-11-24
        return False
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
