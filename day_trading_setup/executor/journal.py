"""Build one trading day's journal entry from the executor's state and log.

A trade is booked on the day it closes (realized P&L); positions opened that
day and still open are listed separately. Two P&L figures per trade:

  pnl_mid   what the executor recorded (fills at the quote's mid)
  pnl_real  conservative: bought at the ask, sold at the bid of the quote
            logged at each fill - the figure to judge results by

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

from day_trading_setup.executor import settings

NY = ZoneInfo("America/New_York")
_QUOTE = re.compile(r"(TastyTrade|IBKR)[^0-9]*([0-9][0-9.]*)/([0-9][0-9.]*)\)")
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


def trade_entry(trade, day, lines):
    buy, sells = _events(trade, lines)
    opened = _ny(trade["opened_at"])
    closed = _ny(trade["closed_at"]) if trade.get("closed_at") else None
    src, nlv = _sized_off(trade, lines, buy)
    real = None
    if buy and buy["ask"] is not None and all(s["bid"] is not None for s in sells):
        real = round((sum(s["qty"] * s["bid"] for s in sells) - buy["qty"] * buy["ask"]) * 100, 2)
    notes = []
    if src and "IBKR" not in src:
        notes.append(f"sized off the {src} account size (IBKR not connected)")
    if buy and buy.get("source") == "IBKR" and buy["t"] < "2026-10-14T10:21":
        notes.append("entry priced off IBKR's 15-minute-delayed quote")
    return {
        "ticker": trade["ticker"], "side": trade["side"], "right": trade["right"], "strike": trade["strike"],
        "expiry": trade["expiry"], "qty": trade["qty"], "level": trade.get("level"),
        "tv_entry": trade.get("tv_entry"), "tv_stop": trade.get("tv_stop"), "tv_target": trade.get("tv_target"),
        "opened": _hm(opened, day), "closed": _hm(closed, day) if closed else None,
        "result": " + ".join(s["kind"] for s in sells) or None,
        "buy": buy, "sells": sells,
        "pnl_mid": trade.get("pnl_usd") if closed else None, "pnl_real": real if closed else None,
        "premium": round(buy["qty"] * buy["price"] * 100, 2) if buy and buy["price"] else None,
        "sized_off": src, "account": nlv, "notes": notes,
    }


def build_day(day, mode="dry"):
    path = settings.STATE_FILE if mode == "paper" else settings.DRY_STATE_FILE
    state = json.loads(path.read_text()) if path.exists() else {"positions": {}, "closed": [], "skipped": []}
    lines = _log_lines()
    real_entries = lambda t: t.get("opened_at", "") >= "2026-10-14T09:30"   # earlier rows are setup tests
    closed = [t for t in state["closed"] if real_entries(t) and _ny(t["closed_at"]).date() == day]
    closed.sort(key=lambda t: t["opened_at"])
    trades = [trade_entry(t, day, lines) for t in closed]
    still_open = [trade_entry(t, day, lines) for t in state["positions"].values()
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
    accounts = [t["account"] for t in trades if t["account"] and t["sized_off"] == "IBKR"]
    return {
        "date": day.isoformat(),
        "mode": "PAPER" if mode == "paper" else "DRY RUN",
        "account": max(accounts) if accounts else None,
        "summary": {
            "trades": len(trades), "wins": sum(1 for x in pr if x > 0), "losses": sum(1 for x in pr if x <= 0),
            "pnl_mid": round(sum(pm), 2), "pnl_real": round(sum(pr), 2),
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
    a = ap.parse_args(argv)
    print(json.dumps(build_day(date.fromisoformat(a.date), a.mode), indent=1, default=str))


if __name__ == "__main__":
    main()
