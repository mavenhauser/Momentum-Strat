"""Re-price simulated fills from 1-minute option history.

The executor's live quotes come from TastyTrade's API feed, which for this account
is the 15-minute-delayed demo feed (found 2026-10-16), so every dry-run fill was
priced off a quote about 15 minutes old. The feed's 1-minute candles are accurate,
just published 15 minutes late, so once a fill is more than ~15 minutes old its
true price can be looked up:

  true price  open of the 1-minute candle containing the fill time (the first trade
              after the alert); if the option did not trade that minute, the last
              close in the 5 minutes before, else the next open within 5 minutes
  realistic   true price +/- half the bid/ask spread logged at the fill (the width
              of the delayed quote; spreads move far less than prices)

When the option did not trade in the fill's own minute, the nearby trade is moved
by the option's delta times the stock's move between that minute and the fill
(2026-11-09: DELL 372.5C's nearest trade was 5 minutes later, after DELL ran
+$3.3, so the unadjusted entry was ~$0.95 too high); with no trade within 5
minutes the nearest within 30 is used the same way. A fill is priced only once
the candles cover 5 minutes past it (DELAY + WINDOW): LRCX's 2R sale on Nov 9
was priced at 15:37, when the candles still ended near 15:22, and fell back to
the delayed quote; fills left without a price are retried on later runs.

Results go to state/dts_reprice_<mode>.json keyed by "<ticker>|<event time>", which
journal.py reads. Re-running only fetches events that are not priced yet.

  python -m day_trading_setup.executor.reprice [--mode dry|paper] [--since 2026-10-14] [--redo 2026-11-09]
"""
import argparse
import asyncio
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from day_trading_setup.executor import settings

NY = ZoneInfo("America/New_York")
DELAY = timedelta(minutes=16)          # candles become available ~15 min after the minute
WINDOW_MS = 5 * 60 * 1000
WIDE_MS = 30 * 60 * 1000               # delta-adjusted fallback for thinly traded strikes
READY = DELAY + timedelta(milliseconds=WINDOW_MS)   # candles cover the whole window after the fill
RETRY_FROM = "2026-11-09"              # older unpriced fills (no priced_at) are left as they are


def reprice_path(mode):
    return settings.STATE_DIR / f"dts_reprice_{mode}.json"


def streamer_symbol(trade):
    if trade.get("streamer"):
        return trade["streamer"]
    exp = datetime.fromisoformat(trade["expiry"]).strftime("%y%m%d")
    return f".{trade['ticker']}{exp}{trade['right']}{trade['strike']:g}"


def fills(trade):
    """The trade's BUY / SELL events (HOLD notes, e.g. a single contract held at 2R or a
    strong-close overnight hold, carry no fill)."""
    return [ev for ev in trade["events"] if ev["action"].startswith(("BUY", "SELL"))]


def event_key(trade, ev):
    return f"{trade['ticker']}|{ev['t']}"


async def _candles(symbols, start):
    import sys
    sys.path.insert(0, str(settings.STATE_DIR.parent))
    from src.tastytrade_client import config
    from tastytrade import DXLinkStreamer, Session
    from tastytrade.dxfeed import Candle
    sess = Session(provider_secret=config.TT_CLIENT_SECRET, refresh_token=config.TT_REFRESH_TOKEN)
    out = {s: {} for s in symbols}
    async with DXLinkStreamer(sess) as st:
        await st.subscribe_candle(symbols, "1m", start)
        # History arrives first, then live updates keep coming for actively traded
        # contracts, so stop at a hard deadline or after a quiet spell.
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
                if o == o and cl == cl and o > 0:      # skip NaN / empty bars
                    out[base][int(c.time)] = (o, cl)
    return out


def _stock_at(stock, minute, which):
    """Stock price at a minute: its open (which=0) or close (1), else the nearest close before."""
    if not stock:
        return None
    if minute in stock:
        return stock[minute][which]
    before = [m for m in stock if minute - WIDE_MS <= m < minute]
    return stock[max(before)][1] if before else None


def alert_price(ev):
    """The stock price in the fill's TradingView alert ("... @ 221.12 ..."), if any."""
    m = re.search(r"@ ([0-9]+(?:\.[0-9]+)?)", ev.get("alert") or "")
    return float(m.group(1)) if m else None


def true_price(bars, when, stock=None, delta=None, at=None):
    """(price, how) from {minute_start_ms: (open, close)} for a fill at `when`.

    stock: the underlying's 1-minute {minute: (open, close)}; delta: the option's
    signed delta. With both, a trade from another minute is moved by delta x the
    stock's move from that minute to the fill. at: the stock price at the fill (the
    alert's price); default the fill minute's open."""
    t = int(when.timestamp() * 1000)
    minute = t - t % 60000
    hm = lambda m: f"{datetime.fromtimestamp(m / 1000, NY):%H:%M}"
    if minute in bars:
        return bars[minute][0], f"1m open {hm(minute)}"
    s_fill = at if at is not None else _stock_at(stock, minute, 0)
    can_adjust = s_fill is not None and delta is not None and delta == delta

    def adjusted(px, m, which, how):
        if not can_adjust:
            return px, how
        s_ref = _stock_at(stock, m, which)
        if s_ref is None:
            return px, how
        move = delta * (s_fill - s_ref)
        return round(max(px + move, 0.01), 2), f"{how} {move:+.2f} (delta x stock {s_ref:.2f} -> {s_fill:.2f})"

    for width in (WINDOW_MS, WIDE_MS):
        if width == WIDE_MS and not can_adjust:
            break
        before = [m for m in bars if minute - width <= m < minute]
        after = [m for m in bars if minute < m <= minute + width]
        b = max(before) if before else None
        a = min(after) if after else None
        if b is None and a is None:
            continue
        if a is None or (b is not None and minute - b <= a - minute):
            return adjusted(bars[b][1], b, 1, f"1m close {hm(b)}")
        return adjusted(bars[a][0], a, 0, f"1m open {hm(a)}")
    return None, "no trades within 5 minutes" + (" (30 with the stock price)" if can_adjust else "")


def signed_delta(trade):
    d = trade.get("delta")
    if d is None:
        return None
    return abs(d) if trade.get("right") == "C" else -abs(d)


def run(mode="dry", since="2026-10-14T09:30", redo=None):
    """Price fills not priced yet (and retry ones left without a price). redo: an ISO
    date whose fills are all priced again."""
    state_file = settings.STATE_FILE if mode == "paper" else settings.DRY_STATE_FILE
    state = json.loads(state_file.read_text())
    path = reprice_path(mode)
    done = json.loads(path.read_text()) if path.exists() else {}
    now = datetime.now(NY)
    trades = [t for t in state["closed"] + list(state["positions"].values()) if t.get("opened_at", "") >= since]

    def due(ev, key):
        if datetime.fromisoformat(ev["t"]) > now - READY:
            return False
        if redo and ev["t"][:10] == redo:
            return True
        if key not in done:
            return True
        r = done[key]
        if r.get("true") is not None:
            return False
        # retry a fill left without a price only if it was priced before its candles were complete
        if r.get("priced_at"):
            return datetime.fromisoformat(r["priced_at"]) < datetime.fromisoformat(ev["t"]) + READY
        return ev["t"][:10] >= RETRY_FROM

    todo = [(t, ev) for t in trades for ev in fills(t) if due(ev, event_key(t, ev))]
    if not todo:
        return done, 0
    symbols = sorted({streamer_symbol(t) for t, _ in todo} | {t["ticker"] for t, _ in todo})
    start = min(datetime.fromisoformat(ev["t"]) for _, ev in todo).astimezone(NY).replace(hour=9, minute=30, second=0, microsecond=0)
    bars = asyncio.run(_candles(symbols, start))
    for t, ev in todo:
        price, how = true_price(bars[streamer_symbol(t)], datetime.fromisoformat(ev["t"]),
                                bars.get(t["ticker"]), signed_delta(t), alert_price(ev))
        spread = (ev["ask"] - ev["bid"]) if ev.get("bid") is not None and ev.get("ask") is not None else None
        minute = int(datetime.fromisoformat(ev["t"]).timestamp() * 1000) // 60000 * 60000
        traded = sum(1 for m in bars[streamer_symbol(t)] if minute - 29 * 60000 <= m <= minute)
        done[event_key(t, ev)] = {"symbol": streamer_symbol(t), "true": price, "how": how, "spread": spread,
                                  "trade_minutes_30": traded,
                                  "recorded": ev.get("price"), "side": "BUY" if ev["action"].startswith("BUY") else "SELL",
                                  "priced_at": now.isoformat(timespec="seconds")}
    path.write_text(json.dumps(done, indent=1))
    return done, len(todo)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("dry", "paper"), default="paper" if settings.EXECUTE else "dry")
    ap.add_argument("--since", default="2026-10-14T09:30")
    ap.add_argument("--redo", help="price every fill of this date (YYYY-MM-DD) again")
    a = ap.parse_args(argv)
    done, n = run(a.mode, a.since, a.redo)
    missing = [k for k, v in done.items() if v["true"] is None]
    print(f"priced {n} new fills; {len(done)} total; {len(missing)} without a 1-minute price: {missing}")


if __name__ == "__main__":
    main()
