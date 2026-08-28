import os
from dotenv import load_dotenv

load_dotenv()

# IBKR connection
IBKR_HOST = os.getenv("IBKR_HOST", "127.0.0.1")
IBKR_PORT = int(os.getenv("IBKR_PORT", "7497"))  # 7497 = paper TWS, 4002 = paper Gateway
IBKR_CLIENT_ID = int(os.getenv("IBKR_CLIENT_ID", "1"))  # scripts/run_paper_trader.py (SPX Scalp) - long-running, holds this all day

# Distinct client IDs for the momentum options system (2026-08-03: found
# colliding with the SPX Scalp trader's clientId=1, which stays connected
# continuously all session - "Error 326: client id already in use" every
# time run_momentum_paper_trader.py tried to connect while it was up).
MOMENTUM_IBKR_CLIENT_ID = 45  # scripts/run_momentum_paper_trader.py
MOMENTUM_CHECK_IBKR_CLIENT_ID = 46  # scripts/check_momentum_option_order.py

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Polygon (optional fallback for historical data)
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")

# Tastytrade (live option quotes only - order execution stays on IBKR).
# Personal accounts commonly enforce 2FA, which breaks an unattended
# username/password Session; use the OAuth refresh-token flow instead.
# TT_CLIENT_ID is only needed once, to register the OAuth app in Tastytrade's
# developer portal and mint TT_REFRESH_TOKEN - the tastytrade SDK's Session
# only takes the client secret + refresh token at runtime.
TT_CLIENT_ID = os.getenv("TT_CLIENT_ID", "")
TT_CLIENT_SECRET = os.getenv("TT_CLIENT_SECRET", "")
TT_REFRESH_TOKEN = os.getenv("TT_REFRESH_TOKEN", "")
TT_QUOTE_MAX_STALENESS_SECONDS = 15
# get_quote()/session_low() pull single-contract quotes via the DXLink
# stream (see tastytrade_client.py) rather than the REST market-data
# snapshot, which was confirmed (2026-08-05) to return zero results for
# every SPX/SPXW/SPY option contract tested. This bounds how long each
# call waits for a live tick before giving up.
TT_QUOTE_STREAM_TIMEOUT_SECONDS = 8

# Strategy / risk parameters
INITIAL_CASH = 1000
PREMIUM_RANGE = (2.0, 3.0)
CONTRACTS_PER_TRADE = 4
STOP_LOSS = -0.20  # -20%, active until the breakeven trigger fires
TRIM_LEVELS = [0.25, 0.50]  # +25%, +50% - 1 contract trimmed at each
BREAKEVEN_AFTER_LEVEL = 0.50  # once this trim fires, stop moves to breakeven (0%)

# Strategy 1: EMA stack on SPX index bars (8 > 21 > 50 -> calls, reverse -> puts)
BAR_SIZE = "5 mins"
EMA_PERIODS = (8, 21, 50)

# Entry trigger tuning: a candle "touches" EMA8 if its wick gets within this
# fraction of price of the EMA (doesn't need to cross fully through it)
EMA_TOUCH_TOLERANCE_PCT = 0.0005

# Minimum EMA8-EMA50 separation (as a fraction of price) required at signal
# time - a proxy for trend conviction, filtering out weak/choppy stacks.
# NOTE (2026-08-01): no longer used by the live signal (see
# EMA_CALLS_MIN_SPREAD_PCT below) - detect_entry_signals/bounce_signal,
# which this gates, was retired from the live path after an extensive
# investigation (docs/paper_trading_dev_tracker.html) found no config of
# it ever profitable under real capital constraints. Left here only
# because src/backtest.py's original run_backtest()/variants still use it
# for historical comparison.
MIN_STACK_SPREAD_PCT = 0.0

# Live production signal (2026-08-01): calls-only, loosened EMA condition
# (src/ema_calls_loose_signal.py::detect_ema_calls_loose_signal) - within a
# bullish EMA8/21/50 stack, any bar closing above ema8 counts, no
# touch/bounce/cross requirement. Replaced detect_entry_signals as the live
# signal after a months-long investigation found calls have a real,
# sample-robust directional follow-through edge (54-65% win rate at 5-30min,
# confirmed 5 independent ways) while puts never showed one, at any filter
# setting or regime tried (docs/paper_trading_dev_tracker.html has the full
# record). 0% held up as well as tighter widths across the whole sweep, so
# there's no separate width filter live - kept as its own constant (not
# reusing MIN_STACK_SPREAD_PCT) since that one gates a different, retired
# signal.
EMA_CALLS_MIN_SPREAD_PCT = 0.0

# Only fire signals within these ET windows. 9:45-12:00 ET (2026-08-01) -
# the window the calls-only signal above was developed and tested against.
SIGNAL_TIME_WINDOWS = [(9, 45, 12, 0)]

# Live signal polling
POLL_INTERVAL_SECONDS = 60

# Status update messages
MARKET_OPEN = (9, 30)  # ET
OPENING_RANGE_MINUTES = 15
HEARTBEAT_INTERVAL_MINUTES = 30

# Emoji used to color-code CALL/PUT and up/down in Telegram messages
CALL_EMOJI = "\U0001F7E2"  # green circle
PUT_EMOJI = "\U0001F534"  # red circle

# Telegram strike suggestions (display only, independent of the backtest's
# premium-based pick_strike): round strikes, at least this far OTM
SUGGESTION_MIN_DISTANCE = 50
SUGGESTION_STRIKE_INCREMENT = 10
SUGGESTION_COUNT = 3

# 0DTE option pricing approximation (Black-Scholes, since IBKR has no historical
# premium data for expired same-day contracts). Backtests price every bar
# with a single constant ASSUMED_IV - an earlier version simulated an
# elevated "IV pop" at entry that decayed back to ASSUMED_IV over time, but
# that was removed: pick_strike's PREMIUM_RANGE walk lands on FAR-OTM
# strikes (confirmed: 270+ points OTM for a $2-3 premium), and far-OTM 0DTE
# premium is almost entirely vega/tail-probability, not delta - so ANY
# nonzero sigma decay (even slowed 10x, to 5 hours) still dominated the P&L
# and stopped out ~100% of trades regardless of what the underlying
# actually did. Confirmed fix: a constant sigma makes premium changes track
# the underlying's real price action again.
ASSUMED_IV = 0.15
RISK_FREE_RATE = 0.05
TRADING_MINUTES_PER_DAY = 390  # 9:30-16:00 ET regular session
TRADING_DAYS_PER_YEAR = 252
STRIKE_INCREMENT = 5

# Which strategies scripts/run_paper_trader.py evaluates - "ema", "range_scalp",
# "vwap_rejection". Range Scalp/VWAP Rejection are disabled for now: focus is
# on getting the EMA strategy live-validated first (it's had far more
# backtesting/tuning); their code is untouched and fully wired, just not
# called - add them back here to re-enable.
ENABLED_STRATEGIES = ["ema"]

# Live order execution (paper trading)
MARKETABLE_LIMIT_SLIPPAGE_PCT = 0.10  # buffer over ask (buy) / under bid (sell)
ORDER_FILL_TIMEOUT_SECONDS = 30
POSITION_PNL_UPDATE_INTERVAL_MINUTES = 5
EOD_FLATTEN_TIME = (15, 55)  # ET, buffer before the 16:00 hard cutoff

# Premium-velocity entry confirmation (2026-08-01, see
# docs/paper_trading_dev_tracker.html "why can you execute this manually
# but the script can't" diagnosis). The EMA touch/cross still picks
# direction + a candidate strike, but the system now WATCHES that
# contract's own live premium for a real "IV flush" before entering,
# instead of entering the instant the price-only EMA signal fires - this
# is the actual edge as described from live manual execution, which the
# system had no way to see before (constant-IV pricing everywhere else in
# this project, by design - see ASSUMED_IV above). Not backtestable -
# Tastytrade has no historical option-premium tick data for expired 0DTE
# contracts, same wall hit investigating a dealer-hedging signal. Live/
# prospective validation only; tune these from what's actually observed.
PREMIUM_VELOCITY_THRESHOLD_PCT = 0.15  # premium must rise >=15% from first-watched quote to confirm
PREMIUM_VELOCITY_WATCH_POLL_SECONDS = 5  # how often to re-check while watching a candidate
PREMIUM_VELOCITY_WATCH_MAX_SECONDS = 120  # abandon the signal if no flush confirms within this long

# Strategy 2: Range Scalp - buy at the 2nd (or riskier 3rd) retest of an SPX
# support/resistance range, entered at the option's own day-low premium.
# Strike selection uses the shared PREMIUM_RANGE walk, same as every other
# strategy - no separate premium band of its own.
RANGE_SCALP_BAR_SIZE = "5 mins"
RANGE_SCALP_RETEST_TOLERANCE_PCT = 0.001  # ~7-8 SPX points at 7500 - starting guess, tune against real retests
RANGE_SCALP_ENTRY_LIMIT_BUFFER = 0.075  # midpoint of the $0.05-$0.10 buffer below day-low premium
RANGE_SCALP_MAX_RETEST = 3  # allow 2nd (preferred) and 3rd (riskier) retests

# Strategy 3: VWAP Rejection - after 2 rejections off VWAP in one direction,
# buy the opposite side on the 3rd test, anticipating the reversal
VWAP_VOLUME_PROXY_SYMBOL = "SPY"  # SPX itself has no real volume (confirmed via both IBKR and Tastytrade)
VWAP_BAR_SIZE = "5 mins"
VWAP_CONFIRMATION_BAR_SIZES = ["15 mins"]  # bias filter only, not entry trigger
VWAP_TEST_TOLERANCE_PCT = 0.001  # ~7-8 SPX points at 7500 - starting guess, tune against real rejections

# ---------------------------------------------------------------------------
# Momentum Strategy - Variant H, options-based, IBKR paper account
# (see Algo Trading/docs/momentum_strategy_backtest_record.html for the full
# variant history/backtest record this is built from)
# ---------------------------------------------------------------------------

# 10-ticker universe: original 5-ticker pilot (explosive-momentum semis/EV)
# plus the 5-ticker expansion (steadier mega-cap tech - the doc's own test of
# these showed materially weaker results, -0.29% avg vs. +10.79% on the
# original 5, since H's edge depends on genuinely explosive moves these
# names mostly didn't have in the tested window; kept in the universe by
# explicit choice, not because the backtest recommends them equally).
MOMENTUM_TICKERS = [
    "DELL", "AMD", "NVDA", "MU", "TSLA",
    "AVGO", "PLTR", "MSFT", "AAPL", "GOOGL",
]

# Semis/AI-hardware cluster - the doc's concentration-risk grouping, so
# several of these signaling the same week can't stack into one oversized
# correlated bet.
MOMENTUM_CLUSTER_SEMIS = ["NVDA", "AMD", "MU", "AVGO", "DELL"]
MOMENTUM_CLUSTER_MAX = 2  # max concurrent open positions within the cluster above
MOMENTUM_MAX_CONCURRENT = 4  # max concurrent open positions across the whole universe

# Entry: hourly check, trailing 6-candle (10-min bar = past hour) lookback,
# 9:45-14:00 ET only - Variant H's whole point vs. continuous checking.
# 9:45 (rather than 10:00) matches this project's existing SPX Scalp
# convention (config.SIGNAL_TIME_WINDOWS) for when signals are allowed to
# start firing. Cron should fire at :45 past every hour to catch this -
# see scripts/run_momentum_paper_trader.sh.
MOMENTUM_ENTRY_LOOKBACK_CANDLES = 6
MOMENTUM_ENTRY_WINDOW_ET = (9, 45, 14, 0)  # (start_hour, start_min, end_hour, end_min)
MOMENTUM_SMA_WINDOW = 200

# Exit (underlying-price terms, translated into option sell orders):
MOMENTUM_STOP_PCT = -0.10  # -10%
MOMENTUM_R_PCT = 0.10  # 1R = 10% of entry price
MOMENTUM_TRIM_R = 2.0  # trim 25% at +2R (option premium terms - see r_multiple() usage in manage_position())
MOMENTUM_TRIM_FRACTION = 0.25

# Second, independent trim trigger (2026-08-15): the underlying hitting a
# "target price" set at entry, whichever of this or MOMENTUM_TRIM_R fires
# first. Target = entry underlying + (swing_high - swing_low), a
# "measured-move" projection - picked over ATR-multiple/Fibonacci-extension
# per scripts/momentum_target_price_backtest.py's results (2026-08-15): it
# beat the +2R-only baseline on return, win rate, AND max drawdown, where
# the other two candidates were a real trade-off or basically inert.
#
# swing_high/swing_low come from find_swing_leg() (real local pivots), NOT
# a plain N-day high/low window - the first live version used a plain
# 20-day window and it backfired immediately: NVDA's target came out above
# its own all-time high, because the window's low and high were just the
# two ends of the same rally that had already carried it to its highs, not
# a separate leg with room to run. Re-backtested with pivot-based detection
# as a direct replacement (same day): target-hit rate rose from 15.3% to
# 37.5% and win rate improved, for a total-return cost that landed at
# rough parity with baseline rather than beating it - a small, honest
# trade-off versus a flaw. See docs/momentum_strategy_backtest_record.html
# for the full comparison.
MOMENTUM_SWING_PIVOT_WIDTH = 3  # bars needed on each side to confirm a local high/low
MOMENTUM_SWING_PIVOT_MAX_LOOKBACK_DAYS = 60
MOMENTUM_TIME_EXIT_TRADING_DAYS = 20
MOMENTUM_ITM_EXTENSION_TRADING_DAYS = 10  # extra runway if still ITM at the time exit

# "Catalyst" exit - user-specified concrete definition (2026-08-02),
# replacing the doc's vaguer "no catalyst ahead" language: exit
# unconditionally, ahead of every other rule, if EITHER holds:
#   (a) theta exceeds delta (both taken directly from the TastyTrade
#       greeks snapshot - the ...*100*contracts scaling cancels on both
#       sides, so this is just abs(theta) > delta, no threshold to tune)
#   (b) the position is still open the same calendar week as its own
#       monthly expiry (src.momentum_signals.is_same_week) - this also
#       fully subsumes the old "N days before expiry" idea, including
#       during an ITM time-exit extension.

# Contract selection (src/momentum_option_picker.py)
MOMENTUM_MIN_DELTA = 0.25
MOMENTUM_MIN_VOLUME = 500
MOMENTUM_MIN_OPEN_INTEREST = 1000
MOMENTUM_TARGET_DTE_MIN = 45  # calendar days out, nearest monthly OpEx

# Sizing: 1% of current IBKR paper account NLV spent as premium budget per
# position (2026-08-02: corrected down from an initial 20%, which was a
# mistranslation of the share-backtest's sizing sweep onto options - 1%
# matches the doc's own live-trading-plan proposal). With
# MOMENTUM_MAX_CONCURRENT=4, this means up to ~4% of NLV in premium at once.
MOMENTUM_SIZING_PCT_NLV = 0.01

# Live order execution (paper trading). Reuses ORDER_FILL_TIMEOUT_SECONDS
# above, but NOT MARKETABLE_LIMIT_SLIPPAGE_PCT - that 10% buffer, live-tested
# 2026-08-03 on an equity option (DELL), tripped IBKR's price-reasonability
# collar and got the order auto-cancelled (Error 202: "cannot accept an
# order at a limit price at or more aggressive than 29.2 ... current market
# price of 28.2" - ~3.5% tolerance, not 10%). Scoped to a separate, smaller
# constant here rather than changing the shared one, since
# MARKETABLE_LIMIT_SLIPPAGE_PCT is also used by the already-running SPX
# Scalp trader (SPX index options, untested against this same collar and
# not to be touched without separately verifying it there).
MOMENTUM_LIMIT_SLIPPAGE_PCT = 0.03
MOMENTUM_STATE_PATH = "state/momentum_positions.json"
