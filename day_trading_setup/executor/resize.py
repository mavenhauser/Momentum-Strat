"""End-of-day resize to the real entry price (user decision, 2026-11-17).

During the session nothing is resized: each trade keeps the size the executor picked off
~15-minute-old quotes, and its messages show that size. At the end of the trading day, when the
journal is built, each trade opened that day is re-booked at its budget at the real entry price
(the 1-minute option price): floor(nlv x size_fraction / (real x 100)) contracts, at least
MIN_CONTRACTS_TO_TRIM. Its sales are re-booked by the executor's own rules: 2R trim = half rounded
down, runner trim = half or the last one, an exit = the rest. Dry run only (in paper mode the
broker fill is the real price). Each trade is checked once (resize_checked); one whose entry has
no 1-minute price yet is left for the next run.

    venv/bin/python3 -m day_trading_setup.executor.resize --date 2026-11-18
Prints JSON: {date, resized: [...], unchanged: n, no_price: [...]}.
"""
import argparse
import fcntl
import json
import math
import os
import re
from datetime import date, datetime

from day_trading_setup.executor import contracts, settings
from day_trading_setup.executor.journal import NY

RESIZE_FROM = "2026-11-18"        # earlier days were resized by hand (Nov 9-13, 16, 17)
_TRIM = re.compile(r"\((\d+(?:\.\d+)?R) trim\)")


def rebook_quantities(p, new):
    """Re-book a trade at `new` contracts: the buy, then each sale. Returns the contracts open."""
    buy = p["events"][0]
    buy["qty"] = new
    buy["action"] = re.sub(r"^BUY \d+", f"BUY {new}", buy["action"])
    left, trims, proceeds = new, 0, 0.0
    for e in p["events"][1:]:
        if not e.get("action", "").startswith("SELL"):
            continue
        if _TRIM.search(e["action"]):
            n = contracts.trim_quantity(left) if trims == 0 else (contracts.trim_quantity(left) or left)
            trims += 1
        else:
            n = left
        e["qty"] = n
        e["action"] = re.sub(r"^SELL \d+", f"SELL {n}", e["action"])
        left -= n
        proceeds += n * (e.get("price") or 0.0)
    p["qty"] = new
    p["proceeds"] = proceeds
    if "closed_at" in p:
        p["pnl_usd"] = round((proceeds - new * p["avg_cost"]) * 100, 2)
    else:
        p["open_qty"] = left
    return left


def _opened_on(p, day):
    return datetime.fromisoformat(p["opened_at"]).astimezone(NY).date() == day


def resize_day(state, day, repriced):
    """Resize the trades opened on `day`. Returns the report; changes `state` in place."""
    from day_trading_setup.executor import reprice
    out = {"date": day.isoformat(), "resized": [], "unchanged": 0, "no_price": []}
    if day.isoformat() < RESIZE_FROM:
        return out
    trades = list(state["closed"]) + list(state["positions"].values())
    for p in trades:
        if not p.get("opened_at") or not _opened_on(p, day) or p.get("resize_checked") or p.get("resized"):
            continue
        r = repriced.get(reprice.event_key(p, p["events"][0]))
        if r is None:
            out["no_price"].append(p["ticker"])          # not priced yet: next run
            continue
        p["resize_checked"] = True
        real, frac, nlv = r.get("true"), p.get("size_fraction"), p.get("nlv")
        if not real or not frac or not nlv:
            out["no_price"].append(p["ticker"])
            continue
        new = max(math.floor(nlv * frac / (real * 100)), settings.MIN_CONTRACTS_TO_TRIM)
        old = p["qty"]
        if new == old:
            out["unchanged"] += 1
            continue
        rebook_quantities(p, new)
        p["resized"] = {"from": old, "to": new, "why": f"budget at the real entry price {real:.2f} "
                        f"(end-of-day resize; user decision, 2026-11-17)"}
        _redo_confirmed(p, repriced)
        out["resized"].append({"ticker": p["ticker"], "from": old, "to": new, "real_entry": real,
                               "sized_at": p["events"][0].get("price"), "budget": round(nlv * frac)})
    return out


def _redo_confirmed(p, repriced):
    """Bring the confirmed P&L figures in line with the new size."""
    from day_trading_setup.executor import reprice
    buy = repriced.get(reprice.event_key(p, p["events"][0]), {}).get("true")
    if buy is None:
        return
    total = 0.0
    for e in p["events"][1:]:
        if not e.get("action", "").startswith("SELL"):
            continue
        sale = repriced.get(reprice.event_key(p, e), {}).get("true")
        if sale is None:
            sale = e.get("price") or 0.0
        pnl = round(e["qty"] * (sale - buy) * 100)
        if "pnl_confirmed" in e:
            e["pnl_confirmed"] = pnl
        total += pnl
    if p.get("pnl_confirmed") is not None and "closed_at" in p:
        p["pnl_confirmed"] = round(total)


def main(argv=None):
    from day_trading_setup.executor import reprice
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="New York trading date, YYYY-MM-DD")
    a = ap.parse_args(argv)
    day = date.fromisoformat(a.date)
    if settings.EXECUTE:
        print(json.dumps({"date": a.date, "skipped": "paper mode: broker fills are real prices"}))
        return 0
    try:
        reprice.run("dry")                  # network: before the lock
    except Exception as e:
        print(json.dumps({"date": a.date, "warning": f"reprice failed: {type(e).__name__}: {e}"}))
    rp = reprice.reprice_path("dry")
    repriced = json.loads(rp.read_text()) if rp.exists() else {}
    with open(settings.LOCK_FILE, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)    # the executor's lock: no alert lands mid-write
        state = json.loads(settings.DRY_STATE_FILE.read_text())
        out = resize_day(state, day, repriced)
        tmp = settings.DRY_STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, default=str))
        os.replace(tmp, settings.DRY_STATE_FILE)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
