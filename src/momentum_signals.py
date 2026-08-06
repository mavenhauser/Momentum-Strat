"""
Shared data-fetch and entry-condition logic for the "Momentum Strategy"
(see docs/momentum_strategy_backtest_record.html for the full variant
history). Two consumers:

  - scripts/momentum_strategy_backtest.py - a day-trade long/short backtest
    over a wide ticker watchlist (a different variant than H; only the
    IBKR data-fetch plumbing here is shared with it).
  - scripts/run_momentum_paper_trader.py - the live options trader for
    Variant H specifically (SMA200 + triple-confirmed breakout, long-only,
    hourly check with a trailing 6-candle lookback, swing exit).

Keeping the daily/intraday bar fetchers in one place (rather than
duplicated per script) avoids exactly the backtest/live drift that this
project's own dev docs flag repeatedly as a recurring failure mode.
"""
import time
from datetime import datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
from pandas.tseries.holiday import USFederalHolidayCalendar
from pandas.tseries.offsets import CustomBusinessDay

ET = ZoneInfo("America/New_York")
REGULAR_START = dtime(9, 30)
REGULAR_END = dtime(16, 0)
INTRADAY_CHUNK = "1 M"  # IBKR's per-request duration cap at 10-min bar size
PACING_SLEEP_SEC = 2

US_BUSINESS_DAY = CustomBusinessDay(calendar=USFederalHolidayCalendar())


def bar_et(b):
    d = b.date
    return d.astimezone(ET) if d.tzinfo else d.replace(tzinfo=ET)


def split_session_bars(bars):
    """Split a day's bars into (premarket, sorted regular-session) lists."""
    premarket = [b for b in bars if bar_et(b).time() < REGULAR_START]
    regular = sorted(
        (b for b in bars if REGULAR_START <= bar_et(b).time() < REGULAR_END),
        key=lambda b: b.date,
    )
    return premarket, regular


# ---- IBKR data fetch --------------------------------------------------------

def fetch_daily_bars(ib, contract, duration="2 Y"):
    return ib.reqHistoricalData(
        contract,
        endDateTime="",
        durationStr=duration,
        barSizeSetting="1 day",
        whatToShow="TRADES",
        useRTH=True,
        formatDate=1,
    )


def fetch_intraday_bars(ib, contract, start_date, end_date,
                         chunk=INTRADAY_CHUNK, pacing_sleep=PACING_SLEEP_SEC):
    """Walk backward in ~1-month chunks from end_date to start_date, since a
    single reqHistoricalData call can't span many months at 10-min bars."""
    end_dt_et = datetime.combine(end_date + timedelta(days=1), dtime(0, 0), tzinfo=ET)
    start_dt_et = datetime.combine(start_date, dtime(0, 0), tzinfo=ET)

    all_bars = []
    cursor_end = end_dt_et

    while cursor_end > start_dt_et:
        chunk_bars = ib.reqHistoricalData(
            contract,
            endDateTime=cursor_end,
            durationStr=chunk,
            barSizeSetting="10 mins",
            whatToShow="TRADES",
            useRTH=False,
            formatDate=1,
        )
        time.sleep(pacing_sleep)

        if not chunk_bars:
            break

        all_bars = chunk_bars + all_bars

        earliest = chunk_bars[0].date
        if earliest <= start_dt_et:
            break
        cursor_end = earliest

    # de-dupe by timestamp, sort ascending, clip to range
    by_ts = {b.date: b for b in all_bars}
    bars = sorted(by_ts.values(), key=lambda b: b.date)
    return [b for b in bars if start_dt_et <= b.date <= end_dt_et]


def build_daily_context(daily_bars, sma_window=200):
    """Per completed trading day: {date: (prev_day_high, prev_day_low, prev_day_close, sma)}."""
    context = {}
    for i in range(sma_window, len(daily_bars)):
        window = daily_bars[i - sma_window:i]  # sma_window bars ending the day before daily_bars[i]
        sma = sum(b.close for b in window) / sma_window
        prev = daily_bars[i - 1]
        # this context applies to "today" = daily_bars[i].date
        context[daily_bars[i].date] = (prev.high, prev.low, prev.close, sma)
    return context


# ---- Variant H entry condition (long-only) ----------------------------------

def _daily_breakout_flags(prev_day_high, premarket_high, regular_bars_so_far):
    """Per-bar breakout flag, aligned with regular_bars_so_far. Each bar is
    checked against the running high strictly BEFORE it (never uses a bar's
    own still-forming extreme) - same rule as the backtest engine."""
    flags = []
    today_high_before = None
    for bar in regular_bars_so_far:
        close = bar.close
        if today_high_before is not None and premarket_high is not None:
            flags.append(close > prev_day_high and close > premarket_high and close > today_high_before)
        else:
            flags.append(False)
        today_high_before = bar.high if today_high_before is None else max(today_high_before, bar.high)
    return flags


def check_variant_h_entry(prev_day_close, sma200, prev_day_high, premarket_high,
                           regular_bars_so_far, lookback=6):
    """Variant H's entry: SMA200 trend filter + triple-confirmed breakout
    (prior-day high, premarket high, fresh high-of-day), true if the
    condition fired on ANY of the trailing `lookback` regular-session bars
    (6 x 10-min bars = the past hour) rather than only the current bar -
    this is what lets an hourly check hold up against G's continuous check
    (see docs/momentum_strategy_backtest_record.html, Variant H). Long-only:
    no short-side condition exists.
    """
    if prev_day_close is None or sma200 is None or prev_day_close <= sma200:
        return False
    if premarket_high is None or not regular_bars_so_far:
        return False
    flags = _daily_breakout_flags(prev_day_high, premarket_high, regular_bars_so_far)
    return any(flags[-lookback:])


# ---- Exit thresholds (pure functions, underlying-price terms) ---------------

def underlying_pct_move(entry_price, current_price):
    return (current_price - entry_price) / entry_price


def underlying_r_multiple(entry_price, current_price, r_pct=0.10):
    return underlying_pct_move(entry_price, current_price) / r_pct


def stop_triggered(entry_price, current_price, stop_pct=-0.10):
    return underlying_pct_move(entry_price, current_price) <= stop_pct


def trim_triggered(entry_price, current_price, trim_r=2.0, r_pct=0.10):
    return underlying_r_multiple(entry_price, current_price, r_pct) >= trim_r


def is_same_week(date_a, date_b):
    """True if date_a and date_b fall in the same Mon-Sun calendar week.
    Used to detect "still holding into the contract's own expiry week"
    (monthly options expire on a Friday, so this covers Monday through
    expiry day itself)."""
    monday_a = date_a - timedelta(days=date_a.weekday())
    monday_b = date_b - timedelta(days=date_b.weekday())
    return monday_a == monday_b


def add_trading_days(start_date, n):
    """`start_date` plus `n` US market business days (same approximation as
    trading_days_since - see its docstring). Used for the ITM time-exit
    extension deadline."""
    if n <= 0:
        return start_date
    rng = pd.bdate_range(start=start_date, periods=n + 1, freq=US_BUSINESS_DAY)
    return rng[-1].date()


def trading_days_since(entry_date, as_of_date):
    """Count of US market business days from entry_date to as_of_date
    (inclusive of as_of_date, exclusive of entry_date) - used for the
    20-trading-day time exit. Uses pandas' federal holiday calendar as an
    approximation of the market calendar (doesn't special-case the handful
    of days that differ, e.g. Good Friday isn't a federal holiday)."""
    if as_of_date <= entry_date:
        return 0
    return len(pd.bdate_range(entry_date, as_of_date, freq=US_BUSINESS_DAY)) - 1
