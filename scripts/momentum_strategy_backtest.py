"""
Backtests the "Momentum Strategy" (the same Pine script running live in
TradingView) across every regular stock/ETF in the TradingView "Main" layout
watchlist, using historical bars pulled from IBKR instead of TradingView.

STRATEGY BEING REPLICATED (long + short, day-trade exit):
  - Daily trend filter: previous day's close vs. the 200-day SMA (as of the
    day before that).
  - Daily breakout/breakdown: latest close vs. previous day's high/low.
  - Intraday breakout/breakdown: latest close vs. today's premarket high/low
    AND vs. today's regular-session high/low-so-far (excluding the bar being
    evaluated, so the signal never uses its own still-forming extreme).
  - Entry: one position at a time, 100% of equity, long or short.
  - Exit: closed at the last regular-session bar of the same day (no
    overnight holds) -- mirrors the Pine script's end-of-session close_all().

This is the same condition logic as momentum_strategy_scanner.py, extended
from a single-point-in-time check into a full bar-by-bar historical walk.

WATCHLIST: fetched once from TradingView (via the MCP tool) and hardcoded
below as a snapshot -- there's no headless API for a live TradingView
watchlist, so re-run watchlist_get manually and update TICKERS if your
watchlist changes. 5 non-equity symbols from the original 49 were dropped
(continuous futures, an index, and a CFD) since they don't have the
premarket/regular-session structure this strategy depends on:
  CME_MINI:NQ1!, CME_MINI:ES1!, TVC:DJI, SPCFD:SPX, TVC:VIX

DATA SOURCE: IBKR via TWS/IB Gateway (ib_insync) -- Yahoo's chart API
returns HTTP 429 from this network, so Yahoo isn't an option here.
Requires TWS/IB Gateway running, logged in, with the API enabled
(see Algo Trading/.env for host/port).

RUNTIME: intraday 10-min bars are pulled from IBKR in ~1-month chunks per
ticker (IBKR's per-request duration cap for that bar size), so a ~10-month
backtest across ~44 tickers means roughly 44 x 10 = ~440 historical data
requests, paced with short sleeps to avoid IBKR pacing violations. Expect
this to take a while (tens of minutes) -- this script is meant to be run
directly (e.g. from VS Code), not from an interactive chat session.

Usage (from the 'Algo Trading' directory):
  source venv/bin/activate
  python scripts/momentum_strategy_backtest.py
"""

import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_insync import IB, Stock  # noqa: E402
from src import config  # noqa: E402
from src.momentum_signals import (  # noqa: E402
    PACING_SLEEP_SEC,
    bar_et,
    build_daily_context,
    fetch_daily_bars,
    fetch_intraday_bars,
    split_session_bars,
)

# ---- config ---------------------------------------------------------------

TICKERS = [
    "SPY", "QQQ", "IWM", "GLD", "SLV",
    "AAPL", "AMZN", "GOOGL", "META", "MSFT", "NVDA", "TSLA",
    "URTH", "IEFA", "MCHI", "EMXC", "EWJ", "AAXJ", "EEM", "EWY", "ACWI",
    "IBIT", "WGMI", "HUT", "FIGR", "BTDR", "COIN", "MARA",
    "DLR", "EQIX", "CLSK", "CORZ", "WULF", "IREN", "CIFR", "ICHR",
    "XLI", "DE", "CAT", "FDX", "MMM", "HON", "LMT", "GE",
]

START_DATE = date(2025, 9, 2)
END_DATE = date.today()
INITIAL_CAPITAL = 100_000.0

IBKR_CLIENT_ID = 44  # distinct from other Algo Trading / scanner clients


# ---- strategy replication ---------------------------------------------------

@dataclass
class Trade:
    symbol: str
    side: str
    entry_date: date
    entry_price: float
    exit_price: float
    pnl_pct: float


def backtest_ticker(symbol, daily_bars, intraday_bars):
    daily_context = build_daily_context(daily_bars)
    if not daily_context:
        return [], "insufficient daily history (<200 bars)"

    by_day = {}
    for b in intraday_bars:
        d = bar_et(b).date()
        by_day.setdefault(d, []).append(b)

    trades = []
    for day, bars in sorted(by_day.items()):
        ctx = daily_context.get(day)
        if ctx is None:
            continue
        prev_day_high, prev_day_low, prev_day_close, sma200 = ctx

        premarket_bars, regular_bars = split_session_bars(bars)
        if not regular_bars:
            continue

        pmh = max((b.high for b in premarket_bars), default=None)
        pml = min((b.low for b in premarket_bars), default=None)

        long_trend_ok = prev_day_close > sma200
        short_trend_ok = prev_day_close < sma200

        position = None  # None | "long" | "short"
        entry_price = None

        today_high_before = None
        today_low_before = None

        for idx, bar in enumerate(regular_bars):
            close = bar.close

            if position is None:
                daily_breakout = close > prev_day_high
                daily_breakdown = close < prev_day_low
                intraday_breakout = (
                    pmh is not None and today_high_before is not None
                    and close > pmh and close > today_high_before
                )
                intraday_breakdown = (
                    pml is not None and today_low_before is not None
                    and close < pml and close < today_low_before
                )

                if long_trend_ok and daily_breakout and intraday_breakout:
                    position = "long"
                    entry_price = close
                elif short_trend_ok and daily_breakdown and intraday_breakdown:
                    position = "short"
                    entry_price = close

            # update today's running high/low AFTER using the "before" values above
            today_high_before = bar.high if today_high_before is None else max(today_high_before, bar.high)
            today_low_before = bar.low if today_low_before is None else min(today_low_before, bar.low)

            is_last_bar_of_day = idx == len(regular_bars) - 1
            if position is not None and is_last_bar_of_day:
                exit_price = close
                pnl_pct = (
                    (exit_price - entry_price) / entry_price * 100 if position == "long"
                    else (entry_price - exit_price) / entry_price * 100
                )
                trades.append(Trade(symbol, position, day, round(entry_price, 4), round(exit_price, 4), round(pnl_pct, 3)))
                position = None
                entry_price = None

    return trades, None


def compute_stats(trades, initial_capital):
    n = len(trades)
    if n == 0:
        return {"trades": 0}

    wins = [t for t in trades if t.pnl_pct > 0]
    losses = [t for t in trades if t.pnl_pct <= 0]
    win_rate = len(wins) / n * 100

    gross_profit = sum(t.pnl_pct for t in wins)
    gross_loss = -sum(t.pnl_pct for t in losses)
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

    equity = initial_capital
    peak = initial_capital
    max_dd_pct = 0.0
    for t in trades:
        equity *= (1 + t.pnl_pct / 100)
        peak = max(peak, equity)
        dd_pct = (peak - equity) / peak * 100
        max_dd_pct = max(max_dd_pct, dd_pct)

    total_return_pct = (equity / initial_capital - 1) * 100

    return {
        "trades": n,
        "win_rate_pct": round(win_rate, 2),
        "profit_factor": round(profit_factor, 3) if profit_factor != float("inf") else None,
        "total_return_pct": round(total_return_pct, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "final_equity": round(equity, 2),
    }


# ---- main -------------------------------------------------------------------

def main():
    ib = IB()
    print(f"Connecting to IBKR at {config.IBKR_HOST}:{config.IBKR_PORT} (clientId={IBKR_CLIENT_ID})...")
    ib.connect(config.IBKR_HOST, config.IBKR_PORT, clientId=IBKR_CLIENT_ID, timeout=15)
    print("Connected.\n")

    all_results = {}
    all_trades = []

    try:
        for symbol in TICKERS:
            print(f"--- {symbol} ---")
            contract = Stock(symbol, "SMART", "USD")
            ib.qualifyContracts(contract)

            daily_bars = fetch_daily_bars(ib, contract)
            time.sleep(PACING_SLEEP_SEC)
            intraday_bars = fetch_intraday_bars(ib, contract, START_DATE, END_DATE)

            trades, err = backtest_ticker(symbol, daily_bars, intraday_bars)
            if err:
                print(f"  skipped: {err}")
                all_results[symbol] = {"trades": 0, "error": err}
                continue

            stats = compute_stats(trades, INITIAL_CAPITAL)
            all_results[symbol] = stats
            all_trades.extend(trades)
            print(f"  {stats}")

    finally:
        ib.disconnect()
        print("\nDisconnected.")

    print("\n=== Summary ===")
    print(f"{'Symbol':<8}{'Trades':>8}{'Win%':>8}{'PF':>8}{'Return%':>10}{'MaxDD%':>10}")
    for symbol, r in sorted(all_results.items(), key=lambda kv: kv[1].get("total_return_pct", -999), reverse=True):
        if r.get("trades", 0) == 0:
            print(f"{symbol:<8}{'--':>8}{'--':>8}{'--':>8}{'--':>10}{'--':>10}  ({r.get('error', 'no trades')})")
        else:
            print(f"{symbol:<8}{r['trades']:>8}{r['win_rate_pct']:>8}{r['profit_factor']:>8}{r['total_return_pct']:>10}{r['max_drawdown_pct']:>10}")

    out_path = Path(__file__).resolve().parent / f"momentum_backtest_{date.today().isoformat()}.csv"
    with open(out_path, "w") as f:
        f.write("symbol,side,entry_date,entry_price,exit_price,pnl_pct\n")
        for t in all_trades:
            f.write(f"{t.symbol},{t.side},{t.entry_date},{t.entry_price},{t.exit_price},{t.pnl_pct}\n")
    print(f"\nSaved {len(all_trades)} trades to {out_path}")


if __name__ == "__main__":
    main()
