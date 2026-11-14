"""Build one trading day's journal entry from the executor's state and log.

A trade is booked on the day it closes (realized P&L); positions opened that
day and still open are listed separately.

The executor's quotes are 15 minutes delayed (TastyTrade demo feed, found
2026-10-16), so its recorded fills are stale. reprice.py looks up each fill's
true price in 1-minute option history; when a fill has one:

  pnl_real  at market prices: the true 1-minute price at entry and exit, no spread
            (the figure used everywhere since 2026-10-21; also in pnl_mid). Days before
            REPRICE_FROM have no 1-minute prices and use the executor's recorded mid fills.
  pnl_spread  true price + half the spread on buys, - half on sells (reference only)

Each sale is booked on the day it happens (a 2R trim held overnight counts that day).

The executor's own figures are kept as pnl_recorded_mid / pnl_recorded_real.
A fill with no 1-minute price falls back to its recorded quote (noted).

Quotes come from each fill's event (recorded since 2026-10-15); for older
trades they are read back from the executor log. Skipped signals and errors
are included so the day can be reviewed in full.

  python -m day_trading_setup.executor.journal --date 2026-10-14 [--mode dry|paper]

Prints the day as JSON (the shape the Trading Journal artifact reads from its
`days/<date>` documents).
"""
import argparse
import json
import math
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from day_trading_setup.executor import reprice, settings

NY = ZoneInfo("America/New_York")
_QUOTE = re.compile(r"(TastyTrade|IBKR|Broker)[^0-9]*([0-9][0-9.]*)/([0-9][0-9.]*)\)")
_LOGTS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ ")


def _ny(ts):
    return datetime.fromisoformat(ts).astimezone(NY)


def _hm(dt, day):
    return dt.strftime("%H:%M") if dt.date() == day else dt.strftime("%a %d %b %H:%M")


def _log_lines():
    f = settings.LOG_DIR / "dts_executor.log"
    out = []
    if not f.exists():
        return out
    for line in f.read_text(errors="replace").splitlines():
        m = _LOGTS.match(line)
        if m and "notify:" in line:
            out.append((datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").astimezone(), line))
    return out


def _quote_from_log(lines, ticker, when, words):
    """Quote on the first notify line within ~2 minutes after `when` that names the
    ticker and one of `words` (BOUGHT / SOLD / CLOSED)."""
    lo, hi = when - timedelta(seconds=5), when + timedelta(seconds=150)
    for ts, line in lines:
        if lo <= ts <= hi and f" {ticker} " in line and any(w in line for w in words):
            m = _QUOTE.search(line)
            if m:
                return float(m.group(2)), float(m.group(3)), m.group(1)
    return None, None, None


def _exit_kind(ev):
    why = ev.get("why") or (re.search(r"\((.*)\)$", ev["action"]) or [None, ""])[1]
    m = re.match(r"(\d+(?:\.\d+)?R)( trim|$)", why)
    if m:
        return m.group(1)
    m = re.match(r"TradingView (\w+)", why)
    if m:
        return m.group(1)
    if why.startswith("time limit"):
        return "TIME LIMIT"
    return "SWEEP" if "sweep" in why else why[:20]


def _events(trade, lines):
    """(buy, sells) with qty, price, bid, ask filled in."""
    buy, sells = None, []
    for ev in reprice.fills(trade):
        qty = ev.get("qty") or int(ev["action"].split()[1])
        price = ev.get("price")
        if price is None:
            p = ev["action"].split()[3]
            price = None if p == "None" else float(p)
        bid, ask, src = ev.get("bid"), ev.get("ask"), ev.get("quote_source")
        when = _ny(ev["t"]).astimezone()
        if ev["action"].startswith("BUY"):
            if bid is None:
                bid, ask, src = _quote_from_log(lines, trade["ticker"], when, ("BOUGHT",))
            buy = dict(qty=qty, price=price, bid=bid, ask=ask, source=src, t=ev["t"])
        else:
            if bid is None:
                bid, ask, src = _quote_from_log(lines, trade["ticker"], when, ("SOLD", "CLOSED"))
            sells.append(dict(qty=qty, price=price, bid=bid, ask=ask, source=src, t=ev["t"], kind=_exit_kind(ev)))
    return buy, sells


def _sized_off(trade, lines, buy):
    if trade.get("nlv_source"):
        return trade["nlv_source"], trade.get("nlv")
    when = _ny(buy["t"]).astimezone()
    for ts, line in lines:
        if when - timedelta(seconds=5) <= ts <= when + timedelta(seconds=150) and f" {trade['ticker']} " in line and "BOUGHT" in line:
            m = re.search(r"of \$([\d,]+) \(([^)]+)\)", line)
            if m:
                return m.group(2), float(m.group(1).replace(",", ""))
    return None, None


def _true_pnl(trade, buy, sells, repriced):
    """(mid, real, fallbacks) at the 1-minute-history prices, or (None, None, 0)."""
    if not repriced or not buy:
        return None, None, 0, [], []
    legs, fallbacks, booked_px = [], 0, []
    for leg, ev in zip([buy] + sells, reprice.fills(trade)):
        r = repriced.get(reprice.event_key(trade, ev))
        spread = (leg["ask"] - leg["bid"]) if leg.get("bid") is not None and leg.get("ask") is not None else None
        true = r["true"] if r and r["true"] is not None else None
        if true is None:                      # no trade that minute: recorded mid
            fallbacks += 1
            true = leg["price"] if leg["price"] is not None else None
        if true is None:
            return None, None, fallbacks, [], []
        # In the first ~15 minutes the delayed feed still shows the pre-market book (TSLA
        # 382.5C at 09:35 on 2026-10-20: 2.00/11.00), which is not a real spread. Only a
        # fill in that window with a spread over MAX_SPREAD_FRACTION of the price falls
        # back to the trade's other legs' spreads (else 5% of the price); wide spreads
        # later in the day are real (a $0.05 option trading 0.03/0.06) and are kept.
        lt = _ny(leg["t"])
        early = (9, 30) <= (lt.hour, lt.minute) < (9, 46)
        if spread is None or (early and spread > settings.MAX_SPREAD_FRACTION * true):
            sane = [l["ask"] - l["bid"] for l in [buy] + sells if l.get("bid") is not None and l.get("ask") is not None
                    and l.get("price") and (l["ask"] - l["bid"]) <= settings.MAX_SPREAD_FRACTION * l["price"]]
            spread = min(sane) if sane else 0.05 * true
        half = spread / 2
        sign = 1 if leg is buy else -1
        booked, how = true, (r or {}).get("how")
        if leg is not buy and close_exit(leg["t"]):
            if (r or {}).get("trade_minutes_30", 0) >= settings.CLOSE_LIQUID_MINUTES:
                how = f"close exit, liquid ({r['trade_minutes_30']}/30 min traded): mid ≈ {how}"
            else:
                booked = round(true - half, 2)
                how = (f"close exit, thin ({(r or {}).get('trade_minutes_30', 0)}/30 min traded): "
                       f"bid ≈ {true:.2f} - half the {spread:.2f} spread")
        legs.append((leg["qty"], booked, true + sign * half, sign, leg["t"]))
        booked_px.append((booked, how, true))
    mid = sum(-s * q * p * 100 for q, p, _, s, _ in legs)
    real = sum(-s * q * r * 100 for q, _, r, s, _ in legs)
    _, b_true, b_real, _, _ = legs[0]
    per_sale = [(ts, q, round(q * (r - b_real) * 100, 2), round(q * (p - b_true) * 100, 2)) for q, p, r, _, ts in legs[1:]]
    return round(mid, 2), round(real, 2), fallbacks, per_sale, booked_px


def close_exit(t_iso) -> bool:
    """A sale at the close (from CLOSE_EXIT_FROM ET, on days from CLOSE_RULE_FROM)."""
    t = _ny(t_iso)
    return t.date().isoformat() >= settings.CLOSE_RULE_FROM and (t.hour, t.minute) >= settings.CLOSE_EXIT_FROM


def trade_entry(trade, day, lines, repriced=None):
    buy, sells = _events(trade, lines)
    opened = _ny(trade["opened_at"])
    closed = _ny(trade["closed_at"]) if trade.get("closed_at") else None
    src, nlv = _sized_off(trade, lines, buy)
    real = None
    if buy and buy["ask"] is not None and all(s["bid"] is not None for s in sells):
        real = round((sum(s["qty"] * s["bid"] for s in sells) - buy["qty"] * buy["ask"]) * 100, 2)
    t_mid, t_real, fb, per_sale, booked_px = _true_pnl(trade, buy, sells, repriced) if sells else (None, None, 0, [], [])
    # Each sale's realized P&L, so a partial sale (the 2R trim) is booked on the day it
    # happens even when the rest is held overnight (2026-10-20, WMT).
    if per_sale:
        # pnl_real is at market prices (user, 2026-10-21): no spread deducted; the
        # spread-adjusted figure is kept as pnl_spread.
        legs_out = [dict(t=ts, qty=q, kind=s["kind"], pnl_real=pm, pnl_mid=pm, pnl_spread=pr) for (ts, q, pr, pm), s in zip(per_sale, sells)]
    else:
        legs_out = [dict(t=s["t"], qty=s["qty"], kind=s["kind"],
                         pnl_real=round(s["qty"] * (s["price"] - buy["price"]) * 100, 2) if buy and buy.get("price") and s.get("price") else None,
                         pnl_mid=round(s["qty"] * (s["price"] - buy["price"]) * 100, 2) if buy and buy.get("price") and s.get("price") else None,
                         pnl_spread=round(s["qty"] * (s["bid"] - buy["ask"]) * 100, 2) if buy and buy.get("ask") is not None and s.get("bid") is not None else None)
                    for s in sells]
    notes = []
    if t_mid is not None:
        # "true" is the price the P&L books: the last traded price, except a sale at the
        # close (mid if liquid, else the bid; CLOSE_RULE_FROM); "last_trade" keeps the 1-minute price.
        for leg, ev, (booked, how, last) in zip([buy] + sells, reprice.fills(trade), booked_px):
            r = (repriced or {}).get(reprice.event_key(trade, ev))
            leg["true"] = booked if r and r["true"] is not None else (r["true"] if r else None)
            leg["true_how"] = how if r else None
            if r and r["true"] is not None and booked != r["true"]:
                leg["last_trade"] = r["true"]
    if fb:
        notes.append(f"{fb} fill(s) had no 1-minute trade nearby; priced at the recorded (delayed) quote")
    if src == "dry-run default":
        notes.append("sized off the $100k placeholder (IBKR not connected), about a tenth of that day's normal size")
    if trade.get("resized"):
        rz = trade["resized"]
        notes.append(f"resized from {rz['from']} to {rz['to']} contracts: {rz['why']}")
    if buy and buy.get("source") == "IBKR" and buy["t"] < "2026-10-14T10:21":
        notes.append("entry priced off IBKR's 15-minute-delayed quote")
    tl = time_limit_test(trade, buy, repriced)
    return {
        "time_limit": tl,
        "ticker": trade["ticker"], "side": trade["side"], "right": trade["right"], "strike": trade["strike"],
        "expiry": trade["expiry"], "qty": trade["qty"], "level": trade.get("level"),
        "tv_entry": trade.get("tv_entry"), "tv_stop": trade.get("tv_stop"), "tv_target": trade.get("tv_target"),
        "opened": _hm(opened, day), "closed": _hm(closed, day) if closed else None,
        "result": " + ".join(s["kind"] for s in sells) or None,
        "buy": buy, "sells": sells,
        "pnl_mid": (t_mid if t_mid is not None else trade.get("pnl_usd")) if closed else None,
        # Market prices for entry and exit (user, 2026-10-21): the 1-minute traded price, no
        # spread. Days before REPRICE_FROM have no 1-minute prices and keep their recorded figure.
        "pnl_real": (t_mid if t_mid is not None else trade.get("pnl_usd")) if closed else None,
        "pnl_spread": t_real if closed else None,
        "pnl_recorded_mid": trade.get("pnl_usd") if closed else None, "pnl_recorded_real": real if closed else None,
        "repriced": t_mid is not None,
        "premium": round(buy["qty"] * buy["price"] * 100, 2) if buy and buy["price"] else None,
        "sized_off": src, "account": nlv, "notes": notes,
        "grade": trade.get("grade"), "size_fraction": trade.get("size_fraction"),
        "legs": legs_out,
    }


def time_limit_test(trade, buy, repriced):
    """The time-limit test for one trade (report only): None if it doesn't apply (opened
    before TIME_LIMIT_TEST_FROM, sold before the checkpoint, or the checkpoint falls at the
    close), else {"at", "price", "entry", "sell" (would be sold: option below the entry
    price), "pnl_real" (with the rule, set by build_day once the real P&L is known)}."""
    ev = reprice.time_limit_check(trade)
    if not ev or not buy:
        return None
    r = (repriced or {}).get(reprice.event_key(trade, ev))
    entry = buy.get("true") if buy.get("true") is not None else buy.get("price")
    out = {"at": _hm(_ny(ev["t"]), _ny(ev["t"]).date()), "price": r["true"] if r else None, "entry": entry,
           "sell": None, "pnl_if_sold": None}
    if r and r["true"] is not None and entry is not None:
        out["sell"] = r["true"] < entry
        out["pnl_if_sold"] = round(trade["qty"] * (r["true"] - entry) * 100, 2)
    return out


def ladder_pnl(steps, entry, qty, t0_ms, sells, bars):
    """P&L of a premium ladder on one trade. steps: [(gain %, share of the starting
    position sold by then)], the last step sells everything left; sells: the as-traded
    sales [(minute ms, qty, price)], each of which sells the same share of what is left;
    bars: [[minute ms, open, close]] of the option. A step fills as a limit at its price
    in the first minute after entry whose open or close reaches it (at the open if it
    opened above). Returns (pnl, [(gain %, minute ms, qty, price)])."""
    rem, pnl, done_q, k, si, act_left, hits = qty, 0.0, 0, 0, 0, qty, []
    end = sells[-1][0] if sells else t0_ms

    def sale(s):
        nonlocal rem, pnl, act_left
        share = s[1] / act_left
        act_left -= s[1]
        q = rem if act_left <= 0 else min(rem, round(rem * share))
        pnl += q * (s[2] - entry) * 100
        rem -= q

    for m, o, c in bars:
        if not (t0_ms < m <= end):
            continue
        while si < len(sells) and sells[si][0] <= m and rem > 0:
            sale(sells[si]); si += 1
        while rem > 0 and k < len(steps) and max(o, c) >= entry * (1 + steps[k][0] / 100):
            thr = round(entry * (1 + steps[k][0] / 100), 2)
            px = o if o >= thr else thr
            target = qty if k == len(steps) - 1 else math.floor(qty * steps[k][1] + 1e-9)
            q = min(rem, max(0, target - done_q))
            pnl += q * (px - entry) * 100
            rem -= q; done_q += q
            hits.append((steps[k][0], m, q, px)); k += 1
        if rem <= 0:
            break
    while si < len(sells) and rem > 0:
        sale(sells[si]); si += 1
    return round(pnl, 2), hits


def ladder_test(trade, repriced):
    """Premium ladder test for one closed trade (report only, from LADDER_TEST_FROM):
    None until its option bars are kept, else {"entry", "pnl_traded" (the sales the
    system made, at the journal's prices), "variants": {name: {"pnl", "steps"}}}."""
    ev = reprice.bars_check(trade)
    if not ev or not repriced:
        return None
    rb = repriced.get(reprice.event_key(trade, ev))
    rbuy = repriced.get(reprice.event_key(trade, reprice.fills(trade)[0]))
    if not rb or not rb.get("bars") or not rbuy or rbuy.get("true") is None:
        return None
    entry = rbuy["true"]
    sells = []
    for s in reprice.as_traded_exits(trade):
        r = repriced.get(reprice.event_key(trade, s)) or {}
        px = r.get("true") if r.get("true") is not None else s.get("price")
        if px is None:
            return None
        if close_exit(s["t"]) and r.get("trade_minutes_30", 0) < settings.CLOSE_LIQUID_MINUTES \
                and s.get("bid") is not None and s.get("ask") is not None:
            px = round(px - (s["ask"] - s["bid"]) / 2, 2)
        sells.append((int(_ny(s["t"]).timestamp() * 1000) // 60000 * 60000, s.get("qty") or int(s["action"].split()[1]), px))
    t0 = int(_ny(trade["opened_at"]).timestamp() * 1000) // 60000 * 60000
    day = _ny(trade["opened_at"]).date()
    hm = lambda m: _hm(datetime.fromtimestamp(m / 1000, NY), day)
    out = {"entry": entry, "pnl_traded": round(sum(q * (p - entry) * 100 for _, q, p in sells), 2), "variants": {}}
    for name, steps in settings.LADDER_TESTS.items():
        pnl, hits = ladder_pnl(steps, entry, trade["qty"], t0, sells, rb["bars"])
        out["variants"][name] = {"pnl": pnl, "steps": [{"gain": g, "at": hm(m), "qty": q, "price": p} for g, m, q, p in hits]}
    return out


def ladder_summary(trades):
    """Day totals of the premium ladder test, or None when no trade was tested."""
    rec = [t for t in trades if t.get("ladder")]
    if not rec:
        return None
    traded = sum(t["ladder"]["pnl_traded"] for t in rec)
    out = {"checked": len(rec), "pnl_traded": round(traded, 2), "variants": {}}
    for name in settings.LADDER_TESTS:
        v = sum(t["ladder"]["variants"][name]["pnl"] for t in rec)
        first = sum(1 for t in rec if t["ladder"]["variants"][name]["steps"])
        out["variants"][name] = {"pnl": round(v, 2), "change": round(v - traded, 2), "reached_first_step": first}
    return out


def _booked_sales(trade, repriced):
    """(entry, [(minute ms, qty, price)]) of the trade's sales as the journal books them, at
    the journal's prices (closing sales at the mid / bid by the close rule), or None."""
    fl = reprice.fills(trade)
    rbuy = (repriced or {}).get(reprice.event_key(trade, fl[0])) if fl else None
    if not rbuy or rbuy.get("true") is None or len(fl) < 2:
        return None
    sells = []
    for s in fl[1:]:
        r = repriced.get(reprice.event_key(trade, s)) or {}
        px = r.get("true") if r.get("true") is not None else s.get("price")
        if px is None:
            return None
        if close_exit(s["t"]) and r.get("trade_minutes_30", 0) < settings.CLOSE_LIQUID_MINUTES \
                and s.get("bid") is not None and s.get("ask") is not None:
            px = round(px - (s["ask"] - s["bid"]) / 2, 2)
        sells.append((int(_ny(s["t"]).timestamp() * 1000) // 60000 * 60000, s.get("qty") or int(s["action"].split()[1]), px))
    return rbuy["true"], sells


def be_test(trade, repriced):
    """Option break-even stop TEST (report only, from EXIT_TESTS_FROM): once the option has
    traded BE_TRIGGER_PCT above entry, everything left is sold the first minute it trades at
    or below the entry price (at that minute's price). None without option bars, else
    {"entry", "pnl_booked", "pnl", "armed", "fired": time or None, "price"}."""
    if trade.get("opened_at", "")[:10] < settings.EXIT_TESTS_FROM:
        return None
    ev = reprice.bars_check(trade)
    rb = (repriced or {}).get(reprice.event_key(trade, ev)) if ev else None
    sales = _booked_sales(trade, repriced)
    if not rb or not rb.get("bars") or not sales:
        return None
    entry, sells = sales
    t0 = int(_ny(trade["opened_at"]).timestamp() * 1000) // 60000 * 60000
    base = sum(q * (p - entry) * 100 for _, q, p in sells)
    left, pnl, si, armed, fired = trade["qty"], 0.0, 0, None, None
    day = _ny(trade["opened_at"]).date()
    hm = lambda m: _hm(datetime.fromtimestamp(m / 1000, NY), day)
    for m, o, c in rb["bars"]:
        while si < len(sells) and sells[si][0] <= m and left > 0:
            q = min(sells[si][1], left)
            pnl += q * (sells[si][2] - entry) * 100
            left -= q
            si += 1
        if left <= 0:
            break
        if m <= t0:
            continue
        for px in (o, c):
            if armed and px <= entry:
                pnl += left * (px - entry) * 100
                fired, left = (hm(m), px), 0
                break
            if armed is None and px >= entry * (1 + settings.BE_TRIGGER_PCT / 100):
                armed = hm(m)
        if left <= 0:
            break
    while si < len(sells) and left > 0:
        q = min(sells[si][1], left)
        pnl += q * (sells[si][2] - entry) * 100
        left -= q
        si += 1
    return {"entry": entry, "pnl_booked": round(base, 2), "pnl": round(pnl, 2), "armed": armed,
            "fired": fired[0] if fired else None, "price": fired[1] if fired else None}


def vf_test(trade, repriced):
    """Volume-fade exit TEST (report only, from EXIT_TESTS_FROM): before the first sale,
    sell everything at a 5-minute close when the last VF_BARS bars all had relative volume
    below VF_REL and none made a new extreme beyond the best price since entry. Relative
    volume = bar volume / average volume of the same bar time over the previous VF_SESSIONS
    sessions (at least 3). None without data, else {"entry", "pnl_booked", "pnl", "fired",
    "price"}."""
    if trade.get("opened_at", "")[:10] < settings.EXIT_TESTS_FROM:
        return None
    rv = (repriced or {}).get(reprice.event_key(trade, {"action": "VOL5"}))
    ev = reprice.bars_check(trade)
    rb = (repriced or {}).get(reprice.event_key(trade, ev)) if ev else None
    sales = _booked_sales(trade, repriced)
    if not rv or not rv.get("bars") or not rb or not rb.get("bars") or not sales:
        return None
    entry, sells = sales
    base = sum(q * (p - entry) * 100 for _, q, p in sells)
    out = {"entry": entry, "pnl_booked": round(base, 2), "pnl": round(base, 2), "fired": None, "price": None}
    opened = _ny(trade["opened_at"])
    day = opened.date()
    bars = sorted(rv["bars"])
    hist, rel = {}, {}
    for m, o, hi, lo, c, v in bars:
        d = datetime.fromtimestamp(m / 1000, NY)
        k = d.strftime("%H:%M")
        prev = hist.setdefault(k, [])[-settings.VF_SESSIONS:]
        rel[m] = v / (sum(prev) / len(prev)) if len(prev) >= 3 and sum(prev) > 0 else None
        if d.date() < day:
            hist[k].append(v)
    long_ = trade["side"] == "LONG"
    t_entry = int(opened.timestamp() * 1000)
    first_sale = sells[0][0]
    today = [b for b in bars if datetime.fromtimestamp(b[0] / 1000, NY).date() == day]
    ent_i = next((i for i, b in enumerate(today) if b[0] <= t_entry < b[0] + 300000), None)
    if ent_i is None:
        return out
    for i in range(ent_i + settings.VF_BARS, len(today)):
        close_ms = today[i][0] + 300000
        if close_ms > first_sale:
            break
        win = today[i - settings.VF_BARS + 1:i + 1]
        if any(rel.get(b[0]) is None or rel[b[0]] >= settings.VF_REL for b in win):
            continue
        before = today[ent_i:i - settings.VF_BARS + 1]
        ext = max(b[2] for b in before) if long_ else min(b[3] for b in before)
        if any((b[2] > ext) if long_ else (b[3] < ext) for b in win):
            continue
        opt = {m: (o, c) for m, o, c in rb["bars"]}
        px, _how = reprice.true_price(opt, datetime.fromtimestamp(close_ms / 1000, NY))
        if px is None:
            return out
        out.update(fired=_hm(datetime.fromtimestamp(close_ms / 1000, NY), day), price=px,
                   pnl=round(trade["qty"] * (px - entry) * 100, 2))
        break
    return out


def exit_test_summary(trades, key):
    """Day totals of a report-only exit test: trades checked, fired, P&L booked vs with the rule."""
    rec = [t for t in trades if t.get(key)]
    if not rec:
        return None
    booked = sum(t[key]["pnl_booked"] for t in rec)
    rule = sum(t[key]["pnl"] for t in rec)
    return {"checked": len(rec), "fired": [t["ticker"] for t in rec if t[key].get("fired")],
            "pnl_booked": round(booked, 2), "pnl_with_rule": round(rule, 2), "change": round(rule - booked, 2)}


def time_limit_summary(trades):
    """Day totals for the time-limit test, or None when no trade was checked."""
    checked = [t for t in trades if t.get("time_limit")]
    if not checked:
        return None
    real = sum(t["pnl_real"] or 0 for t in trades)
    with_rule = real
    cut = []
    for t in checked:
        tl = t["time_limit"]
        if tl["sell"]:
            # the whole position goes at the checkpoint; a trade sold across days is
            # booked in full here (time-limit sales happen on the entry day)
            whole = t.get("pnl_trade_real", t["pnl_real"]) or 0
            with_rule += tl["pnl_if_sold"] - whole
            cut.append(t)
        tl["pnl_real"] = tl["pnl_if_sold"] if tl["sell"] else t["pnl_real"]
    return {"minutes": settings.TIME_LIMIT_MIN, "checked": len(checked), "unpriced": sum(1 for t in checked if t["time_limit"]["sell"] is None),
            "would_sell": len(cut), "would_sell_tickers": [t["ticker"] for t in cut],
            "pnl_real": round(real, 2), "pnl_with_rule": round(with_rule, 2), "change": round(with_rule - real, 2)}


def build_day(day, mode="dry", ladder=False):
    """The day's journal document. ladder: add the premium ladder test (user, 2026-11-12:
    kept off the journal, for the end-of-week review only)."""
    path = settings.STATE_FILE if mode == "paper" else settings.DRY_STATE_FILE
    state = json.loads(path.read_text()) if path.exists() else {"positions": {}, "closed": [], "skipped": []}
    lines = _log_lines()
    rp = reprice.reprice_path("paper" if mode == "paper" else "dry")
    repriced = json.loads(rp.read_text()) if rp.exists() and day.isoformat() >= settings.REPRICE_FROM else {}
    real_entries = lambda t: t.get("opened_at", "") >= "2026-10-14T09:30"   # earlier rows are setup tests
    # A trade appears on every day it sold something; each day's P&L counts only that
    # day's sales (a 2R trim held overnight is booked on the trim day).
    trades = []
    for t in sorted([t for t in state["closed"] + list(state["positions"].values()) if real_entries(t)], key=lambda t: t["opened_at"]):
        closed_today = bool(t.get("closed_at")) and _ny(t["closed_at"]).date() == day
        e = trade_entry(t, day, lines, repriced)
        if closed_today:
            e["vf_test"] = vf_test(t, repriced)
            e["be_test"] = be_test(t, repriced)
        if ladder:
            e["ladder"] = ladder_test(t, repriced)
        today = [l for l in e["legs"] if _ny(l["t"]).date() == day]
        if not closed_today and not today:
            continue
        if not closed_today or len(today) < len(e["legs"]):
            e["pnl_trade_real"], e["pnl_trade_mid"] = e["pnl_real"], e["pnl_mid"]
            e["pnl_real"] = round(sum(l["pnl_real"] or 0 for l in today), 2)
            e["pnl_mid"] = round(sum(l["pnl_mid"] or 0 for l in today), 2)
            e["sells"] = [s for s in e["sells"] if _ny(s["t"]).date() == day]
            e["result"] = " + ".join(s["kind"] for s in e["sells"]) or e["result"]
            sold = sum(l["qty"] for l in today)
            if not closed_today:
                e["partial"] = True
                e["result"] = f"{e['result']} (partial: {t.get('open_qty', 0)} still open)"
                e["notes"].append(f"{sold} of {t['qty']} sold today and booked today; the rest is still open")
            else:
                e["notes"].append(f"only today's {sold} contracts are booked today; earlier sales were booked on their own day")
        trades.append(e)
    still_open = [trade_entry(t, day, lines, repriced) for t in state["positions"].values()
                  if real_entries(t) and _ny(t["opened_at"]).date() <= day]

    skipped = [s for s in state.get("skipped", []) if _ny(s["t"]).date() == day]
    if not skipped:   # days before skips were recorded in state: read them from the log
        for ts, line in lines:
            if ts.astimezone(NY).date() != day:
                continue
            m = re.search(r"skipped (LONG|SHORT) (\S+) - (.*)$", line)
            if m:
                skipped.append({"t": ts.astimezone(NY).isoformat(), "side": m.group(1), "ticker": m.group(2),
                                "reason": m.group(3).replace("already at the limit of 5 open positions", "position limit (5 open)")})
    errors = []
    for ts, line in lines:
        if ts.astimezone(NY).date() == day and "ERROR" in line.split("notify:", 1)[1][:60]:
            errors.append({"t": ts.astimezone(NY).isoformat(), "text": line.split("notify:", 1)[1].strip()[:200]})

    pr = [t["pnl_real"] for t in trades if t["pnl_real"] is not None]
    pm = [t["pnl_mid"] for t in trades if t["pnl_mid"] is not None]
    best = max(trades, key=lambda t: t["pnl_real"] if t["pnl_real"] is not None else -1e18, default=None)
    worst = min(trades, key=lambda t: t["pnl_real"] if t["pnl_real"] is not None else 1e18, default=None)
    accounts = [t["account"] for t in trades if t["account"] and t["sized_off"] in ("IBKR", "notional")]
    tls = time_limit_summary(trades)
    notes = []
    if tls:
        names = ", ".join(tls["would_sell_tickers"]) or "none"
        notes.append(f"Time limit test ({tls['minutes'] // 60} h, not traded): {tls['checked']} trade(s) still short of 2R at the "
                     f"{tls['minutes'] // 60}-hour mark, {tls['would_sell']} below the entry price would have been sold ({names}); "
                     f"P&L with the rule ${tls['pnl_with_rule']:,.0f} vs ${tls['pnl_real']:,.0f} ({tls['change']:+,.0f})"
                     + (f"; {tls['unpriced']} not priced yet" if tls["unpriced"] else "") + ".")
    for key, label, rule in (
            ("vf_test", "Volume-fade exit test", f"before 2R, sell after {settings.VF_BARS} quiet 5-min bars (volume under "
             f"{settings.VF_REL:g}x the usual for the time of day) with no new high/low"),
            ("be_test", "Break-even stop test", f"once the option is +{settings.BE_TRIGGER_PCT}% over its entry, sell the rest "
             f"if it falls back to the entry price")):
        sm = exit_test_summary(trades, key)
        if sm:
            notes.append(f"{label} (not traded; {rule}): {len(sm['fired'])} of {sm['checked']} trade(s) would have been sold"
                         + (f" ({', '.join(sm['fired'])})" if sm["fired"] else "")
                         + f"; P&L with the rule ${sm['pnl_with_rule']:,.0f} vs ${sm['pnl_booked']:,.0f} ({sm['change']:+,.0f}).")
    fb = [s for s in skipped if s.get("fallback_test", {}).get("qty")]
    if fb:
        notes.append("Friday fallback test (not traded; no liquid same-day strike, next week's expiry instead): "
                     + "; ".join(f"{s['ticker']} {s['side'].lower()} at {_ny(s['t']):%H:%M}: {s['fallback_test']['qty']} x "
                                 f"{s['fallback_test']['strike']:g}{s['fallback_test']['right']} {s['fallback_test']['expiry']} "
                                 f"@ about {s['fallback_test']['price'] or 0:.2f}" for s in fb) + ".")
    lad = ladder_summary(trades) if ladder else None
    if lad:
        parts = [f"{name}: {v['reached_first_step']} of {lad['checked']} reached the first step, P&L ${v['pnl']:,.0f} ({v['change']:+,.0f})"
                 for name, v in lad["variants"].items()]
        notes.append(f"Premium ladder test (not traded; sell in steps as the option's price rises over its entry, stock exits sell "
                     f"the same share of the rest): as traded ${lad['pnl_traded']:,.0f}; " + "; ".join(parts) + ".")
    return {
        "date": day.isoformat(),
        "mode": "PAPER" if mode == "paper" else "DRY RUN",
        "account": max(accounts) if accounts else None,
        "summary": {
            "trades": len(trades), "wins": sum(1 for x in pr if x > 0), "losses": sum(1 for x in pr if x <= 0),
            "pnl_mid": round(sum(pm), 2), "pnl_real": round(sum(pr), 2),
            "pnl_recorded_real": round(sum(t["pnl_recorded_real"] or 0 for t in trades), 2),
            "repriced": sum(1 for t in trades if t["repriced"]),
            "premium": round(sum(t["premium"] or 0 for t in trades), 2),
            "best": {"ticker": best["ticker"], "pnl_real": best["pnl_real"]} if best else None,
            "worst": {"ticker": worst["ticker"], "pnl_real": worst["pnl_real"]} if worst else None,
            "skipped": len(skipped), "errors": len(errors), "open": len(still_open),
            "time_limit_test": tls,
            "vf_test": exit_test_summary(trades, "vf_test"),
            "be_test": exit_test_summary(trades, "be_test"),
            **({"ladder_test": lad} if ladder else {}),
        },
        "trades": trades, "open": still_open, "skipped": skipped, "errors": errors,
        "notes": notes, "generated_at": datetime.now(NY).isoformat(timespec="seconds"),
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now(NY).date().isoformat())
    ap.add_argument("--mode", choices=("dry", "paper"), default="paper" if settings.EXECUTE else "dry")
    ap.add_argument("--no-reprice", action="store_true", help="skip fetching 1-minute prices for new fills")
    ap.add_argument("--ladder", action="store_true", help="add the premium ladder test (end-of-week review only, not the journal)")
    a = ap.parse_args(argv)
    if not a.no_reprice:
        try:
            reprice.run(a.mode)
        except Exception as e:              # journal still builds from what is already priced
            import sys
            print(f"reprice failed: {type(e).__name__}: {e}", file=sys.stderr)
    print(json.dumps(build_day(date.fromisoformat(a.date), a.mode, a.ladder), indent=1, default=str))


if __name__ == "__main__":
    main()
