"""Small-account shadow (user decision 2026-11-26): the $10k account that is the eventual goal,
followed next to normal operations from Mon Nov 30, with no orders of its own.

It takes the same trades as the executor, at the same real option prices the journal uses, but
sizes them as a $10k account would with sizing "H": premium 10% of the sizing base for an A+ OUTSIDE
setup, 7% for A OUTSIDE, 5% otherwise (scaled down the same way the main sizing is, e.g. half when
the market is choppy), whole contracts, at most SMALL_MAX_OPEN open at once and never more premium
out than the balance. A trade too expensive for even one contract is skipped. Two or more
contracts follow the main trade's sales in proportion. The sizing base is the day's starting
balance but never more than $10k: losses shrink the trades, gains don't grow them (sizing H was
chosen on a fixed $10k; sizing off a growing balance doubled the drawdown in the Oct 14-Nov 25
replay). Two or more (half at 2R, ...); a single contract can't
be split and goes out with the main trade's last sale. Sales are booked on the day they happen,
like the journal.

  venv/bin/python3 -m day_trading_setup.executor.small_account --date 2026-11-30 --journal-file /tmp/dts-journal-2026-11-30.json

Each day's journal trades are kept in state/small_account/<date>.json and the whole ledger is
rebuilt from them on every run (re-running a day is safe). Prints the day, the balance and a
journal note ("Small account ($10k shadow): ...").
"""
import argparse
import json
from datetime import date, datetime

from day_trading_setup.executor import settings
from day_trading_setup.executor.journal import NY

SMALL_START = 10000.0
SMALL_FROM = "2026-11-30"
SMALL_SIZE = {"A+ OUTSIDE": 0.10, "A OUTSIDE": 0.07}
SMALL_SIZE_DEFAULT = 0.05
SMALL_MAX_OPEN = 6
MAIN_SIZE = {"A+ OUTSIDE": 0.05, "A OUTSIDE": 0.035}
MAIN_SIZE_DEFAULT = 0.025


def _price(ev):
    return ev.get("true") or ev.get("price")


def _key(t):
    return f"{t['ticker']}|{(t.get('buy') or {}).get('t', '')}"


def size_fraction(t):
    """Sizing H, scaled by how the main sizing scaled this trade (choppy half size, Friday swing)."""
    g = t.get("grade")
    base, main = SMALL_SIZE.get(g, SMALL_SIZE_DEFAULT), MAIN_SIZE.get(g, MAIN_SIZE_DEFAULT)
    f = t.get("size_fraction")
    return base * min(1.0, f / main) if f else base


def _clock(ts):
    return datetime.fromisoformat(ts).astimezone(NY).strftime("%Y-%m-%dT%H:%M") if ts else None


def replay(days):
    """days: [(date, journal trades)] in date order -> (ledger rows, positions)."""
    bal = SMALL_START
    pos = {}                     # key -> {n, p, qty_main, sold_main, opened, done}
    rows = []
    for d, trades in days:
        start_bal = bal
        day_pnl, taken, skipped = 0.0, [], []
        events = []
        for t in trades:
            b = t.get("buy") or {}
            if b.get("t") and b["t"][:10] == d and _price(b):
                events.append((_clock(b["t"]), "buy", t))
            for s in t.get("sells", []):
                if s.get("t") and s["t"][:10] == d and _price(s) and s.get("qty"):
                    events.append((_clock(s["t"]), "sell", t, s))
        for ev in sorted(events, key=lambda e: (e[0], 0 if e[1] == "sell" else 1)):
            t = ev[2]; k = _key(t)
            if ev[1] == "buy":
                if k in pos:
                    continue
                p = _price(t["buy"])
                n = int(size_fraction(t) * min(start_bal, SMALL_START) // (p * 100))
                live = [x for x in pos.values() if not x["done"]]
                out = sum(x["n"] * x["p"] * 100 for x in live)
                why = ("too expensive for one contract" if n < 1 else
                       f"{SMALL_MAX_OPEN} already open" if len(live) >= SMALL_MAX_OPEN else
                       "not enough cash" if out + n * p * 100 > bal else None)
                if why:
                    skipped.append(dict(ticker=t["ticker"], why=why, price=p)); pos[k] = dict(n=0, done=True, skip=True)
                    continue
                pos[k] = dict(n=n, p=p, qty_main=t.get("qty") or t["buy"].get("qty") or 1, sold_main=0, opened=d,
                              ticker=t["ticker"], done=False, pnl=0.0)
                taken.append(dict(ticker=t["ticker"], contracts=n, price=p, grade=t.get("grade")))
            else:
                x = pos.get(k)
                if not x or x.get("skip") or x["done"]:
                    continue
                s = ev[3]; sp = _price(s)
                x["sold_main"] += s["qty"]
                if x["n"] >= 2:
                    q = s["qty"] / x["qty_main"] * x["n"]
                    pnl = q * (sp - x["p"]) * 100
                    if x["sold_main"] >= x["qty_main"]:
                        x["done"] = True
                else:
                    if x["sold_main"] < x["qty_main"]:
                        continue                 # one contract: wait for the main trade's last sale
                    pnl = (sp - x["p"]) * 100; x["done"] = True
                x["pnl"] += pnl; day_pnl += pnl; bal += pnl
        rows.append(dict(date=d, start=round(start_bal, 2), pnl=round(day_pnl, 2), balance=round(bal, 2),
                         taken=taken, skipped=skipped,
                         open=[dict(ticker=x["ticker"], contracts=x["n"]) for x in pos.values() if not x.get("skip") and not x["done"]]))
    return rows, pos


def note(row, rows):
    total = row["balance"] - SMALL_START
    sk = {}
    for s in row["skipped"]:
        sk[s["why"]] = sk.get(s["why"], 0) + 1
    skip_txt = ("; skipped " + ", ".join(f"{v} ({k})" for k, v in sk.items())) if sk else ""
    return (f"Small account ($10k shadow, sizing 10/7/5%, max {SMALL_MAX_OPEN} open, not traded): {len(row['taken'])} trade(s) taken"
            f"{skip_txt}; day ${row['pnl']:+,.0f}, balance ${row['balance']:,.0f} ({total / SMALL_START:+.1%} since {SMALL_FROM}, "
            f"{sum(1 for r in rows if r['pnl'] > 0)} green / {sum(1 for r in rows if r['pnl'] < 0)} red days).")


def run(day, journal_doc):
    d = settings.STATE_DIR / "small_account"; d.mkdir(parents=True, exist_ok=True)
    if day.isoformat() >= SMALL_FROM:
        (d / f"{day.isoformat()}.json").write_text(json.dumps(journal_doc.get("trades", []), default=str))
    days = sorted((p.stem, json.loads(p.read_text())) for p in d.glob("20*.json") if p.stem >= SMALL_FROM)
    rows, _ = replay(days)
    (settings.STATE_DIR / "small_account.json").write_text(json.dumps(dict(start=SMALL_START, start_date=SMALL_FROM, rows=rows), indent=1))
    row = next((r for r in rows if r["date"] == day.isoformat()), None)
    return row, rows


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now(NY).date().isoformat())
    ap.add_argument("--journal-file", help="the day's built journal JSON (default: build it now)")
    ap.add_argument("--mode", default="dry")
    a = ap.parse_args(argv)
    day = date.fromisoformat(a.date)
    if a.journal_file:
        doc = json.loads(open(a.journal_file).read())
    else:
        from day_trading_setup.executor import journal
        doc = journal.build_day(day, a.mode)
    row, rows = run(day, doc)
    if row is None:
        print(json.dumps(dict(date=a.date, note=None, why=f"the shadow starts {SMALL_FROM}")))
        return
    print(json.dumps(dict(date=a.date, day=row, note=note(row, rows)), indent=1, default=str))


if __name__ == "__main__":
    main()
