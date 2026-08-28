"""
Backtests candidate "target price" trim triggers against the existing
+2R-only trim, for Variant H's swing exit (see
docs/momentum_strategy_backtest_record.html for the full variant history
and this script's results once run).

WHY THIS EXISTS: the live trader trims 25% of a position once either (a)
the underlying moves +2R (2026-08-15: this specific ratio, and only this
ratio, is measured on option premium in the live script - see
r_multiple()/manage_position() - everything in THIS backtest stays in
underlying-price terms throughout, matching every prior variant's
methodology in the doc above, so results are directly comparable to them),
or (b) NEW: the underlying hits a "target price" set at entry representing
the trade's thesis. There's no established formula for computing that
target automatically, so this compares 4 candidates pulled from what's
commonly used for breakout/momentum trades:
  - ATR-multiple:      target = entry + 2 x ATR(14) as of entry
  - Measured-move:     target = entry + (swing_high - swing_low), plain
                        20-day window high/low
  - Measured-move,     target = entry + (pivot_high - pivot_low), using
    pivot-based           real local swing pivots (find_swing_leg()) instead
                        of a plain window - added 2026-08-15 after the plain
                        version projected a target ABOVE NVDA's own
                        all-time high in real live use (the window's low
                        and high were just the two ends of the same rally
                        that already carried the stock to its highs, not a
                        separate leg with room left to run)
  - Fib extension:     target = entry + 1.618 x (swing_high - swing_low),
                        same plain window as measured-move
(A 5th common method, volume-profile/POC, was dropped - it needs real
volume-at-price data this system doesn't have, only 10-min OHLCV bars; a
bar-derived proxy wouldn't be a faithful implementation.)

METHODOLOGY: identical entries across all 5 conditions (reuses
check_variant_h_entry() directly from src/momentum_signals.py, so this
isolates ONLY the exit-target choice, not entry variance), one position at
a time per ticker, -10% stop (checks intrabar low, matching
pine/momentum_strategy_variant_h.pine), 20-trading-day time exit (no ITM
extension modeled - that's an option-level refinement this underlying-only
backtest doesn't represent, same simplification the doc's other variants
already make). Entries are found by walking available intraday bars (10-min,
narrower history); once a trade opens, the forward swing-exit walk uses only
DAILY bars, since the live Pine script's stop/trim/time-exit checks are
themselves daily-cadence - this is both truer to the original methodology
and far cheaper than an intraday forward walk.

DATA SOURCE: IBKR via TWS/IB Gateway (ib_insync) - requires TWS/IB Gateway
running, logged in, API enabled. Not run automatically; meant to be
invoked directly, expect it to take a while (see RUNTIME note in
scripts/momentum_strategy_backtest.py for why - same IBKR pacing
constraints apply here).

Usage (from the 'Momentum Strat' directory):
  source venv/bin/activate
  python scripts/momentum_target_price_backtest.py
"""

import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_insync import IB, Stock  # noqa: E402

from src import config  # noqa: E402
from src.momentum_signals import (  # noqa: E402
    PACING_SLEEP_SEC,
    build_atr_context,
    build_daily_context,
    check_variant_h_entry,
    fetch_daily_bars,
    fetch_intraday_bars,
    find_swing_leg,
    pct_move,
    split_session_bars,
    swing_range,
    trading_days_since,
    trim_triggered,
)

# ---- config ------------------------------------------------------------------

TICKERS = config.MOMENTUM_TICKERS
INTRADAY_LOOKBACK_DAYS = 365  # how far back to search for entries
END_DATE = date.today()
START_DATE = END_DATE - timedelta(days=INTRADAY_LOOKBACK_DAYS)
DAILY_DURATION = "5 Y"  # extra depth vs. live's 2Y default, for ATR/swing/SMA headroom

STOP_PCT = config.MOMENTUM_STOP_PCT
TRIM_R = config.MOMENTUM_TRIM_R
R_PCT = config.MOMENTUM_R_PCT
TRIM_FRACTION = config.MOMENTUM_TRIM_FRACTION
TIME_EXIT_DAYS = config.MOMENTUM_TIME_EXIT_TRADING_DAYS
ENTRY_LOOKBACK_CANDLES = config.MOMENTUM_ENTRY_LOOKBACK_CANDLES
SMA_WINDOW = config.MOMENTUM_SMA_WINDOW

ATR_PERIOD = 14
ATR_MULTIPLE = 2.0
SWING_LOOKBACK_DAYS = 20  # matches TIME_EXIT_DAYS, for a consistent "recent swing" window
FIB_EXTENSION = 1.618
PIVOT_WIDTH = 3  # bars needed on each side to confirm a local high/low - see find_swing_leg()
PIVOT_MAX_LOOKBACK_DAYS = 60

# measured_move_pivot added 2026-08-15: the naive measured_move (swing_range,
# a plain 20-day high/low) projected a target ABOVE NVDA's own all-time high
# in real live use, because the window's low and high were just the two ends
# of the same rally that already carried the stock to its highs - it wasn't
# measuring a separate leg with room left to run. measured_move_pivot uses
# find_swing_leg() (real local pivots) instead, to isolate one genuine prior
# leg. Both are kept here for a direct before/after comparison.
METHODS = ["baseline_2R", "atr_multiple", "measured_move", "measured_move_pivot", "fib_extension"]

IBKR_CLIENT_ID = 80  # distinct from every other client id used this session

INITIAL_CAPITAL = 100_000.0


@dataclass
class Trade:
    symbol: str
    method: str
    entry_date: date
    entry_price: float
    exit_reason: str  # stop | time | data_end
    exit_idx: int  # daily_bars index of the exit - used to gate re-scanning
    trimmed: bool
    trim_reason: str  # "2R" | "target" | None
    days_held: int
    pnl_pct: float  # blended, weighted by trim fraction realized


def target_price_for(method, entry_price, atr_at_entry, swing_high, swing_low, pivot_high, pivot_low):
    if method == "baseline_2R":
        return None  # no separate target - +2R is the only trigger
    if method == "atr_multiple":
        return entry_price + ATR_MULTIPLE * atr_at_entry if atr_at_entry is not None else None
    if method == "measured_move_pivot":
        if pivot_high is None or pivot_low is None:
            return None
        return entry_price + (pivot_high - pivot_low)
    if swing_high is None or swing_low is None:
        return None
    height = swing_high - swing_low
    if method == "measured_move":
        return entry_price + height
    if method == "fib_extension":
        return entry_price + FIB_EXTENSION * height
    raise ValueError(method)


def simulate_position(daily_bars, entry_idx, target_price):
    """Forward-walks daily_bars from entry_idx+1. Trim fires on the first of
    +2R (underlying, via trim_triggered()) or day.high >= target_price
    (target_price=None -> 2R is the only trigger, i.e. today's live rule).
    If both fire the same day, credits whichever price level is lower (the
    one price would reach first on the way up). Returns a dict of trade
    fields (all but symbol/method, which the caller fills in)."""
    entry_price = daily_bars[entry_idx].close
    entry_date = daily_bars[entry_idx].date
    stop_price = entry_price * (1 + STOP_PCT)
    r2_price = entry_price * (1 + TRIM_R * R_PCT)

    trimmed, trim_reason = False, None
    realized_pct, remaining_frac = 0.0, 1.0

    for j in range(entry_idx + 1, len(daily_bars)):
        day = daily_bars[j]
        days_held = trading_days_since(entry_date, day.date)

        if day.low <= stop_price:
            realized_pct += remaining_frac * pct_move(entry_price, stop_price)
            return dict(entry_date=entry_date, entry_price=entry_price, exit_reason="stop", exit_idx=j,
                        trimmed=trimmed, trim_reason=trim_reason, days_held=days_held, pnl_pct=realized_pct * 100)

        if not trimmed:
            hit_2r = trim_triggered(entry_price, day.close, TRIM_R, R_PCT)
            hit_target = target_price is not None and day.high >= target_price
            if hit_2r or hit_target:
                if hit_2r and hit_target:
                    fill_price = min(r2_price, target_price)
                    trim_reason = "2R" if r2_price <= target_price else "target"
                elif hit_2r:
                    fill_price, trim_reason = r2_price, "2R"
                else:
                    fill_price, trim_reason = target_price, "target"
                realized_pct += TRIM_FRACTION * pct_move(entry_price, fill_price)
                remaining_frac -= TRIM_FRACTION
                trimmed = True
                stop_price = entry_price  # breakeven, matches live/Pine

        if days_held >= TIME_EXIT_DAYS:
            realized_pct += remaining_frac * pct_move(entry_price, day.close)
            return dict(entry_date=entry_date, entry_price=entry_price, exit_reason="time", exit_idx=j,
                        trimmed=trimmed, trim_reason=trim_reason, days_held=days_held, pnl_pct=realized_pct * 100)

    # ran out of daily history before any exit fired - close at the last available bar
    last_idx = len(daily_bars) - 1
    last = daily_bars[last_idx]
    days_held = trading_days_since(entry_date, last.date)
    realized_pct += remaining_frac * pct_move(entry_price, last.close)
    return dict(entry_date=entry_date, entry_price=entry_price, exit_reason="data_end", exit_idx=last_idx,
                trimmed=trimmed, trim_reason=trim_reason, days_held=days_held, pnl_pct=realized_pct * 100)


def backtest_ticker(symbol, daily_bars, intraday_bars):
    daily_context = build_daily_context(daily_bars, sma_window=SMA_WINDOW)
    atr_context = build_atr_context(daily_bars, period=ATR_PERIOD)
    if not daily_context:
        return [], f"insufficient daily history (<{SMA_WINDOW} bars)"

    by_day = {}
    for b in intraday_bars:
        by_day.setdefault(b.date.date(), []).append(b)

    day_index = {b.date: i for i, b in enumerate(daily_bars)}
    trades = []
    # One position at a time per ticker, same as scripts/momentum_strategy_backtest.py.
    # The 4 methods can exit on different dates - the baseline (+2R-only, always
    # defined) sets the re-scan cadence, matching what the live single-method
    # system would actually do.
    resume_after_idx = -1

    for day, bars in sorted(by_day.items()):
        if day not in day_index or day_index[day] <= resume_after_idx:
            continue  # not a known daily bar, or still holding from an earlier entry
        ctx = daily_context.get(day)
        if ctx is None:
            continue
        prev_day_high, _prev_day_low, prev_day_close, sma = ctx

        premarket_bars, regular_bars = split_session_bars(bars)
        if not regular_bars:
            continue
        premarket_high = max((b.high for b in premarket_bars), default=None)

        triggered = check_variant_h_entry(
            prev_day_close, sma, prev_day_high, premarket_high, regular_bars,
            lookback=ENTRY_LOOKBACK_CANDLES,
        )
        if not triggered:
            continue

        entry_idx = day_index[day]
        entry_price = daily_bars[entry_idx].close
        atr_at_entry = atr_context.get(day)
        swing_high, swing_low = swing_range(daily_bars, day, lookback_days=SWING_LOOKBACK_DAYS)
        pivot_high, pivot_low = find_swing_leg(daily_bars, day, PIVOT_WIDTH, PIVOT_MAX_LOOKBACK_DAYS)

        baseline_exit_idx = None
        for method in METHODS:
            target = target_price_for(method, entry_price, atr_at_entry, swing_high, swing_low, pivot_high, pivot_low)
            if method != "baseline_2R" and target is None:
                continue  # not enough history for this method's target this trade - skip just this method
            result = simulate_position(daily_bars, entry_idx, target)
            trades.append(Trade(symbol=symbol, method=method, **result))
            if method == "baseline_2R":
                baseline_exit_idx = result["exit_idx"]

        resume_after_idx = baseline_exit_idx  # re-scan starting the day after baseline's exit

    return trades, None


def compute_stats(trades):
    n = len(trades)
    if n == 0:
        return {"trades": 0}
    wins = [t for t in trades if t.pnl_pct > 0]
    losses = [t for t in trades if t.pnl_pct <= 0]
    gross_profit = sum(t.pnl_pct for t in wins)
    gross_loss = -sum(t.pnl_pct for t in losses)
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

    equity = INITIAL_CAPITAL
    peak = INITIAL_CAPITAL
    max_dd_pct = 0.0
    for t in trades:
        equity *= (1 + t.pnl_pct / 100)
        peak = max(peak, equity)
        max_dd_pct = max(max_dd_pct, (peak - equity) / peak * 100)

    target_hits = [t for t in trades if t.trim_reason == "target"]
    return {
        "trades": n,
        "win_rate_pct": round(len(wins) / n * 100, 2),
        "profit_factor": round(profit_factor, 3) if profit_factor != float("inf") else None,
        "total_return_pct": round((equity / INITIAL_CAPITAL - 1) * 100, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "trimmed_pct": round(sum(1 for t in trades if t.trimmed) / n * 100, 1),
        "target_hit_pct": round(len(target_hits) / n * 100, 1),
        "avg_days_to_target": (
            round(sum(t.days_held for t in target_hits) / len(target_hits), 1) if target_hits else None
        ),
    }


def main():
    ib = IB()
    print(f"Connecting to IBKR at {config.IBKR_HOST}:{config.IBKR_PORT} (clientId={IBKR_CLIENT_ID})...")
    ib.connect(config.IBKR_HOST, config.IBKR_PORT, clientId=IBKR_CLIENT_ID, timeout=15)
    print("Connected.\n")

    all_trades = []
    per_ticker_errors = {}

    try:
        for symbol in TICKERS:
            print(f"--- {symbol} ---")
            contract = Stock(symbol, "SMART", "USD")
            ib.qualifyContracts(contract)

            daily_bars = fetch_daily_bars(ib, contract, duration=DAILY_DURATION)
            time.sleep(PACING_SLEEP_SEC)
            intraday_bars = fetch_intraday_bars(ib, contract, START_DATE, END_DATE)

            trades, err = backtest_ticker(symbol, daily_bars, intraday_bars)
            if err:
                print(f"  skipped: {err}")
                per_ticker_errors[symbol] = err
                continue
            all_trades.extend(trades)
            print(f"  {len(trades) // len(METHODS)} entries found, {len(trades)} simulated trade-legs")

    finally:
        ib.disconnect()
        print("\nDisconnected.")

    print("\n=== Summary by method ===")
    print(f"{'Method':<20}{'Trades':>8}{'Win%':>8}{'PF':>8}{'Return%':>10}{'MaxDD%':>10}{'Trimmed%':>10}{'TargetHit%':>12}{'AvgDaysToTgt':>14}")
    for method in METHODS:
        method_trades = [t for t in all_trades if t.method == method]
        stats = compute_stats(method_trades)
        if stats["trades"] == 0:
            print(f"{method:<20}{'--':>8}")
            continue
        print(f"{method:<20}{stats['trades']:>8}{stats['win_rate_pct']:>8}{stats['profit_factor']:>8}"
              f"{stats['total_return_pct']:>10}{stats['max_drawdown_pct']:>10}{stats['trimmed_pct']:>10}"
              f"{stats['target_hit_pct']:>12}{str(stats['avg_days_to_target']):>14}")

    out_path = Path(__file__).resolve().parent / f"momentum_target_price_backtest_{date.today().isoformat()}.csv"
    with open(out_path, "w") as f:
        f.write("symbol,method,entry_date,entry_price,exit_reason,trimmed,trim_reason,days_held,pnl_pct\n")
        for t in all_trades:
            f.write(f"{t.symbol},{t.method},{t.entry_date},{t.entry_price},{t.exit_reason},"
                     f"{t.trimmed},{t.trim_reason or ''},{t.days_held},{t.pnl_pct}\n")
    print(f"\nSaved {len(all_trades)} trade-legs to {out_path}")
    if per_ticker_errors:
        print(f"\nSkipped tickers: {per_ticker_errors}")


if __name__ == "__main__":
    main()
