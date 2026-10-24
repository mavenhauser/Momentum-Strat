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

Results go to state/dts_reprice_<mode>.json keyed by "<ticker>|<event time>", which
journal.py reads. Re-running only fetches events that are not priced yet.

  python -m day_trading_setup.executor.reprice [--mode dry|paper] [--since 2026-10-14]
"""
import argparse
import asyncio
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from day_trading_setup.executor import settings

NY = ZoneInfo("America/New_York")
DELAY = timedelta(minutes=16)          # candles become available ~15 min after the minute
WINDOW_MS = 5 * 60 * 1000


def reprice_path(mode):
    return settings.STATE_DIR / f"dts_reprice_{mode}.json"


def streamer_symbol(trade):
    if trade.get("streamer"):
        return trade["streamer"]
    exp = datetime.fromisoformat(trade["expiry"]).strftime("%y%m%d")
    return f".{trade['ticker']}{exp}{trade['right']}{trade['strike']:g}"


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


def true_price(bars, when):
    """(price, how) from {minute_start_ms: (open, close)} for a fill at `when`."""
    t = int(when.timestamp() * 1000)
    minute = t - t % 60000
    if minute in bars:
        return bars[minute][0], f"1m open {datetime.fromtimestamp(minute / 1000, NY):%H:%M}"
    before = [m for m in bars if minute - WINDOW_MS <= m < minute]
    if before:
        m = max(before)
        return bars[m][1], f"1m close {datetime.fromtimestamp(m / 1000, NY):%H:%M}"
    after = [m for m in bars if minute < m <= minute + WINDOW_MS]
    if after:
        m = min(after)
        return bars[m][0], f"1m open {datetime.fromtimestamp(m / 1000, NY):%H:%M}"
    return None, "no trades within 5 minutes"


def run(mode="dry", since="2026-10-14T09:30"):
    state_file = settings.STATE_FILE if mode == "paper" else settings.DRY_STATE_FILE
    state = json.loads(state_file.read_text())
    path = reprice_path(mode)
    done = json.loads(path.read_text()) if path.exists() else {}
    now = datetime.now(NY)
    trades = [t for t in state["closed"] + list(state["positions"].values()) if t.get("opened_at", "") >= since]
    todo = [(t, ev) for t in trades for ev in t["events"]
            if event_key(t, ev) not in done and datetime.fromisoformat(ev["t"]) <= now - DELAY]
    if not todo:
        return done, 0
    symbols = sorted({streamer_symbol(t) for t, _ in todo})
    start = min(datetime.fromisoformat(ev["t"]) for _, ev in todo).astimezone(NY).replace(hour=9, minute=30, second=0, microsecond=0)
    bars = asyncio.run(_candles(symbols, start))
    for t, ev in todo:
        price, how = true_price(bars[streamer_symbol(t)], datetime.fromisoformat(ev["t"]))
        spread = (ev["ask"] - ev["bid"]) if ev.get("bid") is not None and ev.get("ask") is not None else None
        done[event_key(t, ev)] = {"symbol": streamer_symbol(t), "true": price, "how": how, "spread": spread,
                                  "recorded": ev.get("price"), "side": "BUY" if ev["action"].startswith("BUY") else "SELL"}
    path.write_text(json.dumps(done, indent=1))
    return done, len(todo)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("dry", "paper"), default="paper" if settings.EXECUTE else "dry")
    ap.add_argument("--since", default="2026-10-14T09:30")
    a = ap.parse_args(argv)
    done, n = run(a.mode, a.since)
    missing = [k for k, v in done.items() if v["true"] is None]
    print(f"priced {n} new fills; {len(done)} total; {len(missing)} without a 1-minute price: {missing}")


if __name__ == "__main__":
    main()
