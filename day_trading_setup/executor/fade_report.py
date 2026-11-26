"""Fade test, report only (user decision 2026-11-26): no orders, nothing sent to the executor.

After the close, for one trading day, finds every VWAP-band fade the research found
(Nov 26, scratch study): between 10:00 and 10:30 New York time, a stock on the day's list whose
previous 5-minute bar stretched beyond its VWAP +/- 2 standard deviations, and whose bar then
closes back inside the band and against the stretch. Entry at that close, toward VWAP; stop
0.25 ATR beyond the extreme; target VWAP; sold after 90 minutes, else at 15:55; one per stock;
skipped while the executor held that stock. Each fade is priced with the real 1-minute prices of
the option the executor would buy (this week's expiry, nearest strike at or out of the money),
fetched the same evening while TastyTrade still has them (it keeps no history once a contract
expires). Size: FADE_BUDGET of premium. P&L at traded prices and after FADE_SPREAD (the median
round-trip spread at executor fills 10:00-10:30, Oct 14-Nov 25).

  venv/bin/python3 -m day_trading_setup.executor.fade_report --date 2026-11-30

Writes state/fade_report/<date>.json, appends a line to state/fade_report.jsonl and prints the
day's summary with a one-line journal note ("Fade test (not traded): ...").
"""
import argparse
import asyncio
import json
import math
from datetime import date, datetime, timedelta

from day_trading_setup.executor import settings
from day_trading_setup.executor.journal import NY

K_SD = 2.0
WINDOW = ("10:00", "10:30")
STOP_ATR = 0.25
TIME_STOP_MIN = 90
FADE_BUDGET = 2500.0
FADE_SPREAD = 0.043          # round trip, share of the option price
MIN_OPTION = 0.30


def _hm(ms):
    return datetime.fromtimestamp(ms / 1000, NY).strftime("%H:%M")


def _add(hm, m):
    return (datetime.strptime(hm, "%H:%M") + timedelta(minutes=m)).strftime("%H:%M")


def prev_weekday(d):
    d -= timedelta(days=1)
    while d.weekday() >= 5 or d.isoformat() in settings.MARKET_HOLIDAYS:
        d -= timedelta(days=1)
    return d


def find_fades(bars, day, held=None, qqq=None):
    """bars: {ticker: [(date, "HH:MM", o, h, l, c, v), ...]} 5-minute regular-hours bars including the
    previous session (ATR warm-up). held: {ticker: (from "HH:MM", to "HH:MM")} executor positions.
    qqq: [(date, hm, o, h, l, c, v)] for the QQQ tag. Returns the day's fades (stock R)."""
    held = held or {}
    d = day.isoformat()
    qday = {}
    if qqq:
        qb = [r for r in qqq if r[0] == d]
        if qb:
            qo = qb[0][2]
            qday = {_add(r[1], 5): (r[5] / qo - 1) * 100 for r in qb}
    out = []
    for tk, rows in bars.items():
        rows = sorted(rows)
        tr, atr = [], []
        for i, r in enumerate(rows):
            t = r[3] - r[4] if i == 0 else max(r[3] - r[4], abs(r[3] - rows[i-1][5]), abs(r[4] - rows[i-1][5]))
            tr.append(t)
            atr.append(sum(tr) / len(tr) if i < 14 else (atr[-1] * 13 + t) / 14)
        idx = [i for i, r in enumerate(rows) if r[0] == d]
        if len(idx) < 3:
            continue
        pv = vv = pv2 = 0.0
        vw, sd = {}, {}
        for i in idx:
            r = rows[i]; tp = (r[3] + r[4] + r[5]) / 3
            pv += tp * r[6]; vv += r[6]; pv2 += tp * tp * r[6]
            m = pv / vv if vv else tp
            vw[i] = m; sd[i] = math.sqrt(max(pv2 / vv - m * m, 0)) if vv else 0.0
        pos = None
        for i in idx[2:]:
            _, hm, o, h, l, c, v = rows[i]
            end = _add(hm, 5)
            if pos:
                sg = pos["sg"]
                if (l <= pos["stop"]) if sg > 0 else (h >= pos["stop"]):
                    pos.update(R=-1.0, t_out=end, why="stop"); out.append(pos); pos = None; break
                if (h >= vw[i]) if sg > 0 else (l <= vw[i]):
                    pos.update(R=(vw[i] - pos["e"]) * sg / pos["r"], t_out=end, why="target"); out.append(pos); pos = None; break
                held_min = (datetime.strptime(end, "%H:%M") - datetime.strptime(pos["t_in"], "%H:%M")).seconds / 60
                if held_min >= TIME_STOP_MIN or end >= "15:55":
                    pos.update(R=(c - pos["e"]) * sg / pos["r"], t_out=end, why="time"); out.append(pos); pos = None; break
                continue
            if not (WINDOW[0] < end <= WINDOW[1]) or sd[i] <= 0:
                continue
            if tk in held and held[tk][0] <= end < held[tk][1]:
                continue
            p = rows[i-1]
            for sg, stretched, back in ((-1, p[3] > vw[i-1] + K_SD * sd[i-1], c < vw[i] + K_SD * sd[i] and c < o),
                                        (1, p[4] < vw[i-1] - K_SD * sd[i-1], c > vw[i] - K_SD * sd[i] and c > o)):
                if stretched and back:
                    ext = max(p[3], h) if sg < 0 else min(p[4], l)
                    stop = ext - sg * STOP_ATR * atr[i]
                    r = abs(c - stop)
                    if r <= 0 or r / c < 0.002 or (vw[i] - c) * sg <= 0:
                        continue
                    pos = dict(ticker=tk, side="LONG" if sg > 0 else "SHORT", sg=sg, e=c, stop=stop, r=r, t_in=end,
                               target_at_entry=vw[i], qqq_day=qday.get(end))
                    break
        if pos:
            last = rows[idx[-1]]
            pos.update(R=(last[5] - pos["e"]) * pos["sg"] / pos["r"], t_out="16:00", why="close"); out.append(pos)
    return out


def option_candidates(f, day):
    fri = day + timedelta(days=(4 - day.weekday()) % 7)
    exp = fri.strftime("%y%m%d"); right = "C" if f["sg"] > 0 else "P"; px = f["e"]
    grids = [1.0, 2.5, 5.0] if px < 120 else [2.5, 5.0] if px < 500 else [5.0, 10.0]
    out = []
    for g in grids:
        k = math.ceil(px / g) * g if right == "C" else math.floor(px / g) * g
        ks = str(int(k)) if k == int(k) else str(round(k, 2))
        out.append(f".{f['ticker']}{exp}{right}{ks}")
    return list(dict.fromkeys(out))


def price_fades(fades, m1, spread=FADE_SPREAD, budget=FADE_BUDGET):
    """m1: {symbol: {"HH:MM": (open, close)}} 1-minute prices for the day. Adds option fields."""
    def at(s, hm, which):
        if hm in s:
            return s[hm][0 if which == "open" else 1]
        before = [k for k in s if k < hm]
        return s[max(before)][1] if before else None
    for f in fades:
        f["priced"] = False
        opt = next((c for c in f["cands"] if any(_add(f["t_in"], -10) <= k <= _add(f["t_in"], 5) for k in m1.get(c, {}))), None)
        if not opt:
            f["why_unpriced"] = "no option trades near the entry"; continue
        s = m1[opt]; o_in = at(s, f["t_in"], "open"); o_out = at(s, min(f["t_out"], "15:59"), "close")
        if not o_in or o_in < MIN_OPTION or o_out is None:
            f["why_unpriced"] = "option price missing or under $0.30"; continue
        qty = max(1, int(budget // (o_in * 100)))
        pnl = (o_out - o_in) * 100 * qty
        f.update(priced=True, option=opt, o_in=o_in, o_out=o_out, qty=qty, pnl=round(pnl, 2),
                 pnl_after_spread=round(pnl - spread * o_in * 100 * qty, 2))
    return fades


def summary(fades):
    pr = [f for f in fades if f.get("priced")]
    sub = [f for f in pr if f["sg"] < 0 and (f.get("qqq_day") or 0) > 0]
    s = lambda v, k: round(sum(f[k] for f in v), 2)
    return dict(signals=len(fades), priced=len(pr), stock_R=round(sum(f["R"] for f in fades), 2),
                pnl=s(pr, "pnl"), pnl_after_spread=s(pr, "pnl_after_spread"),
                wins=sum(1 for f in pr if f["pnl"] > 0),
                puts_qqq_up=dict(n=len(sub), pnl=s(sub, "pnl"), pnl_after_spread=s(sub, "pnl_after_spread")))


def note(day, sm):
    if not sm["signals"]:
        return "Fade test (not traded; VWAP-band fades 10:00-10:30): no signals today."
    return (f"Fade test (not traded; VWAP-band fades 10:00-10:30): {sm['signals']} signal(s), {sm['priced']} priced with real "
            f"option prices: ${sm['pnl']:+,.0f} at market prices, ${sm['pnl_after_spread']:+,.0f} after a {FADE_SPREAD:.1%} spread "
            f"({sm['wins']} of {sm['priced']} won); puts with QQQ up {sm['puts_qqq_up']['n']}: "
            f"${sm['puts_qqq_up']['pnl_after_spread']:+,.0f} after spread.")


def day_list(day):
    p = settings.STATE_DIR / "dts_today_list.json"
    try:
        j = json.loads(p.read_text())
        if j.get("date") == day.isoformat():
            return list(dict.fromkeys(j["tickers"] + j.get("held_only", [])))
    except Exception:
        pass
    st = json.loads(settings.DRY_STATE_FILE.read_text())
    return sorted({t["ticker"] for t in st["closed"] + st.get("skipped", []) if (t.get("opened_at") or t.get("t") or "")[:10] == day.isoformat()})


def held_today(day):
    st = json.loads(settings.DRY_STATE_FILE.read_text())
    out = {}
    for p in list(st["closed"]) + list(st["positions"].values()):
        if (p.get("opened_at") or "")[:10] == day.isoformat():
            a = datetime.fromisoformat(p["opened_at"]).astimezone(NY).strftime("%H:%M")
            b = datetime.fromisoformat(p["closed_at"]).astimezone(NY).strftime("%H:%M") if p.get("closed_at") else "16:00"
            out[p["ticker"]] = (a, b)
    return out


def run(day):
    from day_trading_setup.executor import reprice
    tickers = day_list(day)
    start = datetime.combine(prev_weekday(day), datetime.min.time()).replace(hour=9, minute=30, tzinfo=NY)
    raw = asyncio.run(reprice._candles5(tickers + ["QQQ"], start))
    bars = {}
    for s, b in raw.items():
        bars[s] = [(datetime.fromtimestamp(k / 1000, NY).date().isoformat(), _hm(k), *v) for k, v in sorted(b.items())]
    qqq = bars.pop("QQQ", [])
    fades = find_fades({k: v for k, v in bars.items() if v}, day, held_today(day), qqq)
    for f in fades:
        f["cands"] = option_candidates(f, day)
    syms = sorted({c for f in fades for c in f["cands"]})
    m1 = {}
    if syms:
        got = asyncio.run(reprice._candles(syms, datetime.combine(day, datetime.min.time()).replace(hour=9, minute=30, tzinfo=NY)))
        m1 = {s: {_hm(k): v for k, v in sorted(b.items()) if datetime.fromtimestamp(k / 1000, NY).date() == day} for s, b in got.items()}
    price_fades(fades, m1)
    sm = summary(fades)
    doc = dict(date=day.isoformat(), tickers=tickers, fades=fades, summary=sm, note=note(day, sm))
    d = settings.STATE_DIR / "fade_report"; d.mkdir(parents=True, exist_ok=True)
    (d / f"{day.isoformat()}.json").write_text(json.dumps(doc, indent=1, default=str))
    with open(settings.STATE_DIR / "fade_report.jsonl", "a") as fh:
        fh.write(json.dumps(dict(date=day.isoformat(), **sm)) + "\n")
    return doc


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now(NY).date().isoformat())
    a = ap.parse_args(argv)
    doc = run(date.fromisoformat(a.date))
    print(json.dumps(dict(date=doc["date"], summary=doc["summary"], note=doc["note"]), indent=1))


if __name__ == "__main__":
    main()
