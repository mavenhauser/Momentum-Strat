"""Small-account shadow (user decisions 2026-11-26): the $10k account that is the eventual goal,
followed next to normal operations from Mon Nov 30. No orders, no Telegram, not in the journal:
it keeps its own record (state/small_account.json, shown on its own page).

It takes the same trades as the executor, with the same exits (TradingView's stop, 2R half, trail,
time limit, the 15:56 close and the day rules all come from the main trade), but sizes them as a
$10k account:
  - premium budget "sizing H": 10% of the sizing base for an A+ OUTSIDE setup, 7% for A OUTSIDE,
    5% otherwise, scaled down the way the main sizing was (e.g. half when the market is choppy);
    the sizing base is the day's starting balance but never more than $10k;
  - contract: the main system's strike rules, applied to this budget: start at the strike the
    main trade bought and step further out of the money until a strike buys TARGET_CONTRACTS (4),
    else MIN_CONTRACTS_TO_TRIM (2), with delta >= the main delta floor, price >= $0.50 and at least
    100 contracts traded that day; none -> skipped (1 contract can't be split at 2R). Weekly
    contracts usually have the open interest; delta (the "beta" to the stock) falls further out,
    hence the floor. Delta comes from each contract's own price (Black-Scholes implied volatility),
    since past deltas aren't stored;
  - at most SMALL_MAX_OPEN open, never more premium out than the balance.
Prices: the real 1-minute traded prices of the chosen contract (no spread, like the journal),
fetched the same evening while TastyTrade still has them. Sales follow the main trade's sales in
proportion, at the chosen contract's price in that minute.

  venv/bin/python3 -m day_trading_setup.executor.small_account --date 2026-11-30 --journal-file /tmp/dts-journal-2026-11-30.json

Writes state/small_account/<date>.json (the day's shadow trades) and state/small_account.json
(the ledger: one row per day, rebuilt from the day files on every run; re-running a day is safe).
"""
import argparse
import asyncio
import json
import math
from datetime import date, datetime, timedelta

from day_trading_setup.executor import estimate, settings
from day_trading_setup.executor.journal import NY

SMALL_START = 10000.0
SMALL_FROM = "2026-11-30"
SMALL_SIZE = {"A+ OUTSIDE": 0.10, "A OUTSIDE": 0.07}
SMALL_SIZE_DEFAULT = 0.05
SMALL_MAX_OPEN = 6
MAIN_SIZE = {"A+ OUTSIDE": 0.05, "A OUTSIDE": 0.035}
MAIN_SIZE_DEFAULT = 0.025
MIN_DAY_VOLUME = 100
MAX_RUNGS = 20


def _price(ev):
    return ev.get("true") or ev.get("price")


def _key(t):
    return f"{t['ticker']}|{(t.get('buy') or {}).get('t', '')}"


def _hm(ts):
    return datetime.fromisoformat(ts).astimezone(NY).strftime("%H:%M")


def size_fraction(t):
    """Sizing H, scaled by how the main sizing scaled this trade (choppy half size, Friday swing)."""
    g = t.get("grade")
    base, main = SMALL_SIZE.get(g, SMALL_SIZE_DEFAULT), MAIN_SIZE.get(g, MAIN_SIZE_DEFAULT)
    f = t.get("size_fraction")
    return base * min(1.0, f / main) if f else base


def option_symbol(ticker, expiry, right, strike):
    ks = str(int(strike)) if float(strike) == int(strike) else f"{strike:g}"
    return f".{ticker}{date.fromisoformat(expiry).strftime('%y%m%d')}{right}{ks}"


def strike_step(price):
    return 0.5 if price < 50 else 1.0 if price < 200 else 2.5 if price < 500 else 5.0


def candidate_strikes(t, budget):
    """Strikes from the main trade's strike outward (calls up, puts down), guided by Black-Scholes so
    the cheap enough ones are reached: the main strike, its next rungs, and the rungs around the
    strikes that price at budget/400 and budget/200."""
    k0, right, s = float(t["strike"]), t["right"], t["tv_entry"]
    p0 = _price(t["buy"]); exp = date.fromisoformat(t["expiry"])
    when = datetime.fromisoformat(t["buy"]["t"]).astimezone(NY)
    yrs = estimate.years_to(exp, when)
    vol = estimate.implied_vol(p0, s, k0, yrs, right) or 0.5
    step = strike_step(s); sg = 1 if right == "C" else -1
    # every rung from the main strike outward, as far as Black-Scholes says the 4-contract price lies
    # (+2 rungs for the volatility smile), at least 4 rungs, at most MAX_RUNGS
    target = budget / (settings.TARGET_CONTRACTS * 100)
    j = 0
    while j < MAX_RUNGS and estimate.bs_price(s, k0 + sg * step * j, yrs, vol, right) > target:
        j += 1
    n = min(MAX_RUNGS, max(4, j + 3))
    ks = [round(k0 + sg * step * i, 2) for i in range(n) if k0 + sg * step * i > 0]
    return ks, yrs, vol


async def _fetch(symbols, start):
    """{symbol: {"HH:MM": (open, close, volume)}} 1-minute candles of `start`'s day."""
    import sys
    sys.path.insert(0, str(settings.STATE_DIR.parent))
    from src.tastytrade_client import config
    from tastytrade import DXLinkStreamer, Session
    from tastytrade.dxfeed import Candle
    sess = Session(provider_secret=config.TT_CLIENT_SECRET, refresh_token=config.TT_REFRESH_TOKEN)
    out = {s: {} for s in symbols}
    async with DXLinkStreamer(sess) as st:
        await st.subscribe_candle(symbols, "1m", start)
        loop = asyncio.get_event_loop()
        deadline = loop.time() + max(45, 3 * len(symbols))
        while loop.time() < deadline:
            try:
                c = await asyncio.wait_for(st.get_event(Candle), 5)
            except asyncio.TimeoutError:
                break
            base = c.event_symbol.split("{")[0]
            if base in out and c.time and c.open is not None and c.close is not None:
                o, cl = float(c.open), float(c.close)
                t = datetime.fromtimestamp(int(c.time) / 1000, NY)
                if o == o and cl == cl and o > 0 and t.date() == start.date():
                    out[base][t.strftime("%H:%M")] = (o, cl, float(c.volume or 0))
    return out


FETCH_BATCH = 40   # TastyTrade refuses a candle subscription that is too big ("subscription size ... too big")


def fetch_candles(symbols, day):
    syms = sorted(set(symbols)); out = {}
    start = datetime.combine(day, datetime.min.time()).replace(hour=9, minute=30, tzinfo=NY)
    for i in range(0, len(syms), FETCH_BATCH):
        out.update(asyncio.run(_fetch(syms[i:i + FETCH_BATCH], start)))
    return out


def _at(series, hm, which=1):
    if hm in series:
        return series[hm][which]
    before = [k for k in series if k < hm]
    return series[max(before)][1] if before else None


def choose(t, budget, m1, yrs_vol):
    """(symbol, strike, qty, price, delta, rule) by the main strike rules, or (None, ..., reason)."""
    ks, yrs, _ = yrs_vol
    hm = _hm(t["buy"]["t"]); right = t["right"]; s = t["tv_entry"]
    rows = []
    for k in ks:
        sym = option_symbol(t["ticker"], t["expiry"], right, k)
        ser = m1.get(sym, {})
        p = _at(ser, hm, 0) if ser else None
        if not p:
            continue
        vol = sum(v[2] for v in ser.values())
        iv = estimate.implied_vol(p, s, k, yrs, right)
        d = abs(estimate.bs_delta(s, k, yrs, iv, right)) if iv else None
        rows.append((k, sym, p, d, vol))
    floor = settings.MIN_DELTA
    for need, rule in ((settings.TARGET_CONTRACTS, f"{settings.TARGET_CONTRACTS}+ contracts"),
                       (settings.MIN_CONTRACTS_TO_TRIM, f"{settings.MIN_CONTRACTS_TO_TRIM}+ contracts")):
        for k, sym, p, d, vol in rows:
            q = int(budget // (p * 100))
            if p >= settings.MIN_OPTION_PRICE and d is not None and d >= floor and vol >= MIN_DAY_VOLUME and q >= need:
                return sym, k, q, p, d, rule
    return None, None, 0, None, None, f"no strike at delta {floor}+ buys {settings.MIN_CONTRACTS_TO_TRIM} within ${budget:,.0f}"


def build_day(day, trades, start_bal, carried, fetch=fetch_candles):
    """The day's shadow trades. carried: positions open from earlier days {key: {...}}.
    Returns (records, carried after the day)."""
    d = day.isoformat()
    base = min(start_bal, SMALL_START)
    buys = [t for t in trades if (t.get("buy") or {}).get("t", "")[:10] == d and _price(t["buy"]) and t.get("strike") and t.get("expiry")]
    cand = {}
    for t in buys:
        b = size_fraction(t) * base
        cand[_key(t)] = (b, candidate_strikes(t, b))
    syms = [option_symbol(t["ticker"], t["expiry"], t["right"], k) for t in buys for k in cand[_key(t)][1][0]]
    syms += [x["symbol"] for x in carried.values()]
    m1 = fetch(syms, day)
    records, live = [], dict(carried)
    events = []
    for t in buys:
        events.append((_hm(t["buy"]["t"]), 1, "buy", t))
    for t in trades:
        for s in t.get("sells", []):
            if (s.get("t") or "")[:10] == d and s.get("qty"):
                events.append((_hm(s["t"]), 0, "sell", t, s))
    bal = start_bal
    for ev in sorted(events, key=lambda e: (e[0], e[1])):
        t = ev[3]; k = _key(t)
        if ev[2] == "buy":
            budget, kv = cand[k]
            open_ = [x for x in live.values() if x["open"] > 0]
            rec = dict(key=k, date=d, ticker=t["ticker"], side=t.get("side"), grade=t.get("grade"), budget=round(budget, 2),
                       main_contract=option_symbol(t["ticker"], t["expiry"], t["right"], float(t["strike"])), t_buy=ev[0])
            if len(open_) >= SMALL_MAX_OPEN:
                rec.update(taken=False, why=f"{SMALL_MAX_OPEN} already open"); records.append(rec); continue
            sym, strike, q, p, dl, rule = choose(t, budget, m1, kv)
            if not sym:
                rec.update(taken=False, why=rule); records.append(rec); continue
            if sum(x["open"] * x["price"] * 100 for x in open_) + q * p * 100 > bal:
                rec.update(taken=False, why="not enough cash"); records.append(rec); continue
            rec.update(taken=True, symbol=sym, strike=strike, qty=q, price=p, delta=round(dl, 2), rule=rule, sells=[], pnl=0.0)
            records.append(rec)
            live[k] = dict(symbol=sym, qty=q, open=q, price=p, main_qty=t.get("qty") or 1, main_sold=0, rec=len(records) - 1, day=d)
        else:
            x = live.get(k)
            if not x or x["open"] <= 0:
                continue
            s = ev[4]
            x["main_sold"] += s["qty"]
            # the same share of contracts as the main trade has sold so far (all of it at its last sale)
            target = x["qty"] if x["main_sold"] >= x["main_qty"] else int(x["qty"] * x["main_sold"] / x["main_qty"])
            want = min(x["open"], target - (x["qty"] - x["open"]))
            if want <= 0:
                continue
            sp = _at(m1.get(x["symbol"], {}), ev[0])
            if sp is None:
                sp = _price(s) * (x["price"] / max(_price(t["buy"]), 0.01))   # no trade that minute: the main contract's move
            pnl = want * (sp - x["price"]) * 100
            x["open"] -= want; bal += pnl
            sale = dict(t=ev[0], date=d, qty=want, price=sp, pnl=round(pnl, 2), why=s.get("kind"))
            if x["day"] == d:
                records[x["rec"]]["sells"].append(sale); records[x["rec"]]["pnl"] = round(records[x["rec"]]["pnl"] + pnl, 2)
            else:
                records.append(dict(key=k, date=d, ticker=t["ticker"], carried_from=x["day"], taken=True, symbol=x["symbol"],
                                    qty=want, price=x["price"], sells=[sale], pnl=round(pnl, 2), sale_only=True))
    carried = {k: dict(x, rec=None) for k, x in live.items() if x["open"] > 0}
    return records, carried


def ledger(day_files):
    """Rows per day from the saved day files (date order)."""
    bal = SMALL_START; rows = []
    for d, doc in day_files:
        recs = doc["records"]
        pnl = sum(r.get("pnl", 0) for r in recs if r.get("taken"))
        start = bal; bal += pnl
        rows.append(dict(date=d, start=round(start, 2), pnl=round(pnl, 2), balance=round(bal, 2),
                         taken=sum(1 for r in recs if r.get("taken") and not r.get("sale_only")),
                         skipped=sum(1 for r in recs if not r.get("taken")),
                         main_trades=doc.get("main_trades"), open_after=len(doc.get("carried", {}))))
    return rows


def run(day, journal_doc, fetch=fetch_candles):
    root = settings.STATE_DIR / "small_account"; root.mkdir(parents=True, exist_ok=True)
    d = day.isoformat()
    prev = sorted((p.stem, json.loads(p.read_text())) for p in root.glob("20*.json") if SMALL_FROM <= p.stem < d)
    rows = ledger(prev)
    start_bal = rows[-1]["balance"] if rows else SMALL_START
    carried = prev[-1][1].get("carried", {}) if prev else {}
    recs, carried = build_day(day, journal_doc.get("trades", []), start_bal, carried, fetch)
    (root / f"{d}.json").write_text(json.dumps(dict(date=d, records=recs, carried=carried,
                                                    main_trades=len(journal_doc.get("trades", []))), indent=1, default=str))
    allf = sorted((p.stem, json.loads(p.read_text())) for p in root.glob("20*.json") if p.stem >= SMALL_FROM)
    rows = ledger(allf)
    (settings.STATE_DIR / "small_account.json").write_text(json.dumps(dict(start=SMALL_START, start_date=SMALL_FROM, rows=rows), indent=1))
    return next(r for r in rows if r["date"] == d), rows, recs


SHADOW_PAGE = "https://claude.ai/artifact/DJ155AeQTWpTJWGBormpgq"   # the "DTS $10k Shadow" page (its own record)


def page_docs(row, recs, journal_doc):
    """The two documents the evening job writes to the shadow page's store for one day:
    data/shadow/days/<date> and data/shadow/trades/<date> (that day's trades as a list)."""
    main = (journal_doc.get("summary") or {}).get("pnl_real")
    day = dict(date=row["date"], start=row["start"], pnl=row["pnl"], balance=row["balance"], taken=row["taken"],
               skipped=row["skipped"], main_trades=row.get("main_trades"),
               main_pnl_tenth=None if main is None else round(main / 10, 2))
    trades = []
    for i, r in enumerate(recs):
        sells = r.get("sells") or []
        note = (r.get("why") if not r.get("taken") else
                ("sale of a position from " + r["carried_from"]) if r.get("sale_only") else
                (r.get("rule") or "") + ("; sold: " + ", ".join(f"{s['qty']} {s.get('why') or ''}".strip() for s in sells) if sells else "; still open"))
        trades.append(dict(id=f"{row['date']}_{i:02d}", date=row["date"], time=r.get("t_buy") or (sells[0]["t"] if sells else None),
                           ticker=r.get("ticker"), side=r.get("side"), grade=r.get("grade"), taken=bool(r.get("taken")),
                           contract=r.get("symbol"), main_contract=r.get("main_contract"), delta=r.get("delta"), qty=r.get("qty"),
                           price=r.get("price"), pnl=r.get("pnl") if r.get("taken") else None, note=note))
    return day, dict(trades=trades)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now(NY).date().isoformat())
    ap.add_argument("--journal-file", help="the day's built journal JSON (default: build it now)")
    ap.add_argument("--mode", default="dry")
    a = ap.parse_args(argv)
    day = date.fromisoformat(a.date)
    if a.date < SMALL_FROM:
        print(json.dumps(dict(date=a.date, why=f"the shadow starts {SMALL_FROM}")))
        return
    doc = json.loads(open(a.journal_file).read()) if a.journal_file else __import__(
        "day_trading_setup.executor.journal", fromlist=["build_day"]).build_day(day, a.mode)
    row, rows, recs = run(day, doc)
    day_doc, trades_doc = page_docs(row, recs, doc)
    out = settings.STATE_DIR / "small_account" / "page"; out.mkdir(parents=True, exist_ok=True)
    (out / f"{a.date}_day.json").write_text(json.dumps(day_doc, default=str))
    (out / f"{a.date}_trades.json").write_text(json.dumps(trades_doc, default=str))
    print(json.dumps(dict(day=row, page=SHADOW_PAGE, day_doc=str(out / f"{a.date}_day.json"),
                          trades_doc=str(out / f"{a.date}_trades.json")), indent=1, default=str))


if __name__ == "__main__":
    main()
