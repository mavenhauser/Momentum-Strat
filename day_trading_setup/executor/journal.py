"""Build one trading day's journal entry from the executor's state and log.

A trade is booked on the day it closes (realized P&L); positions opened that
day and still open are listed separately.

The executor's quotes are 15 minutes delayed (TastyTrade demo feed, found
2026-10-16), so its recorded fills are stale. reprice.py looks up each fill's
true price in 1-minute option history; when a fill has one:

  pnl_real  at market prices: the true 1-minute price at entry and exit, no spread
            (the figure used everywhere since 2026-10-21; also in pnl_mid)
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
    if why.startswith("2R"):
        return "2R"
    m = re.match(r"TradingView (\w+)", why)
    if m:
        return m.group(1)
    return "SWEEP" if "sweep" in why else why[:20]


def _events(trade, lines):
    """(buy, sells) with qty, price, bid, ask filled in."""
    buy, sells = None, []
    for ev in trade["events"]:
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
        return None, None, 0, []
    legs, fallbacks = [], 0
    for leg, ev in zip([buy] + sells, [e for e in trade["events"]]):
        r = repriced.get(reprice.event_key(trade, ev))
        spread = (leg["ask"] - leg["bid"]) if leg.get("bid") is not None and leg.get("ask") is not None else None
        true = r["true"] if r and r["true"] is not None else None
        if true is None:                      # no trade that minute: recorded mid
            fallbacks += 1
            true = leg["price"] if leg["price"] is not None else None
        if true is None:
            return None, None, fallbacks, []
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
        legs.append((leg["qty"], true, true + sign * half, sign, leg["t"]))
    mid = sum(-s * q * p * 100 for q, p, _, s, _ in legs)
    real = sum(-s * q * r * 100 for q, _, r, s, _ in legs)
    _, b_true, b_real, _, _ = legs[0]
    per_sale = [(ts, q, round(q * (r - b_real) * 100, 2), round(q * (p - b_true) * 100, 2)) for q, p, r, _, ts in legs[1:]]
    return round(mid, 2), round(real, 2), fallbacks, per_sale


def trade_entry(trade, day, lines, repriced=None):
    buy, sells = _events(trade, lines)
    opened = _ny(trade["opened_at"])
    closed = _ny(trade["closed_at"]) if trade.get("closed_at") else None
    src, nlv = _sized_off(trade, lines, buy)
    real = None
    if buy and buy["ask"] is not None and all(s["bid"] is not None for s in sells):
        real = round((sum(s["qty"] * s["bid"] for s in sells) - buy["qty"] * buy["ask"]) * 100, 2)
    t_mid, t_real, fb, per_sale = _true_pnl(trade, buy, sells, repriced) if sells else (None, None, 0, [])
    # Each sale's realized P&L, so a partial sale (the 2R trim) is booked on the day it
    # happens even when the rest is held overnight (2026-10-20, WMT).
    if per_sale:
        # pnl_real is at market prices (user, 2026-10-21): no spread deducted; the
        # spread-adjusted figure is kept as pnl_spread.
        legs_out = [dict(t=ts, qty=q, kind=s["kind"], pnl_real=pm, pnl_mid=pm, pnl_spread=pr) for (ts, q, pr, pm), s in zip(per_sale, sells)]
    else:
        legs_out = [dict(t=s["t"], qty=s["qty"], kind=s["kind"],
                         pnl_real=round(s["qty"] * (s["bid"] - buy["ask"]) * 100, 2) if buy and buy.get("ask") is not None and s.get("bid") is not None else None,
                         pnl_mid=round(s["qty"] * (s["price"] - buy["price"]) * 100, 2) if buy and buy.get("price") and s.get("price") else None)
                    for s in sells]
    notes = []
    if t_mid is not None:
        for leg, ev in zip([buy] + sells, trade["events"]):
            r = (repriced or {}).get(reprice.event_key(trade, ev))
            leg["true"] = r["true"] if r else None
            leg["true_how"] = r["how"] if r else None
    if fb:
        notes.append(f"{fb} fill(s) had no 1-minute trade nearby; priced at the recorded (delayed) quote")
    if src == "dry-run default":
        notes.append("sized off the $100k placeholder (IBKR not connected), about a tenth of that day's normal size")
    if buy and buy.get("source") == "IBKR" and buy["t"] < "2026-10-14T10:21":
        notes.append("entry priced off IBKR's 15-minute-delayed quote")
    return {
        "ticker": trade["ticker"], "side": trade["side"], "right": trade["right"], "strike": trade["strike"],
        "expiry": trade["expiry"], "qty": trade["qty"], "level": trade.get("level"),
        "tv_entry": trade.get("tv_entry"), "tv_stop": trade.get("tv_stop"), "tv_target": trade.get("tv_target"),
        "opened": _hm(opened, day), "closed": _hm(closed, day) if closed else None,
        "result": " + ".join(s["kind"] for s in sells) or None,
        "buy": buy, "sells": sells,
        "pnl_mid": (t_mid if t_mid is not None else trade.get("pnl_usd")) if closed else None,
        # Market prices for entry and exit (user, 2026-10-21): the 1-minute traded price, no
        # spread. Days before REPRICE_FROM have no 1-minute prices and keep their recorded figure.
        "pnl_real": (t_mid if t_mid is not None else real) if closed else None,
        "pnl_spread": t_real if closed else None,
        "pnl_recorded_mid": trade.get("pnl_usd") if closed else None, "pnl_recorded_real": real if closed else None,
        "repriced": t_mid is not None,
        "premium": round(buy["qty"] * buy["price"] * 100, 2) if buy and buy["price"] else None,
        "sized_off": src, "account": nlv, "notes": notes,
        "grade": trade.get("grade"), "size_fraction": trade.get("size_fraction"),
        "legs": legs_out,
    }


def build_day(day, mode="dry"):
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
        },
        "trades": trades, "open": still_open, "skipped": skipped, "errors": errors,
        "notes": [], "generated_at": datetime.now(NY).isoformat(timespec="seconds"),
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now(NY).date().isoformat())
    ap.add_argument("--mode", choices=("dry", "paper"), default="paper" if settings.EXECUTE else "dry")
    ap.add_argument("--no-reprice", action="store_true", help="skip fetching 1-minute prices for new fills")
    a = ap.parse_args(argv)
    if not a.no_reprice:
        try:
            reprice.run(a.mode)
        except Exception as e:              # journal still builds from what is already priced
            import sys
            print(f"reprice failed: {type(e).__name__}: {e}", file=sys.stderr)
    print(json.dumps(build_day(date.fromisoformat(a.date), a.mode), indent=1, default=str))


if __name__ == "__main__":
    main()
