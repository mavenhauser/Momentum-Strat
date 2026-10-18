"""Day Trading Setup executor settings.

Broker credentials, ports and Telegram come from the repo's .env through
src.config (shared with the Momentum Strategy). Everything DTS-specific lives
here so the two systems never step on each other's state or IBKR client id.
"""
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STATE_DIR = REPO / "state"
STATE_FILE = STATE_DIR / "dts_positions.json"          # paper-order mode
DRY_STATE_FILE = STATE_DIR / "dts_positions_dry.json"  # dry-run mode (simulated fills)
LOCK_FILE = STATE_DIR / "dts_executor.lock"
INBOX_FILE = STATE_DIR / "dts_alert_inbox.jsonl"       # every alert received, as received
LOG_DIR = REPO / "logs"

# Orders are only ever placed when this is "1" (set by the user, not by default).
# Anything else = dry run: full contract selection, sizing and quotes, no orders.
EXECUTE = os.getenv("DTS_EXECUTE", "0") == "1"
NOTIFY = os.getenv("DTS_NOTIFY", "1") == "1"      # Telegram messages; set 0 for silent tests

# Receiver (TradingView webhook -> this Mac). The secret path segment is kept
# outside the repo in ~/.dts-executor/secret and is created on first start.
RECEIVER_HOST = "127.0.0.1"
RECEIVER_PORT = 8090
SECRET_FILE = Path.home() / ".dts-executor" / "secret"

IBKR_CLIENT_ID = 47          # SPX scalp = 1, Momentum Strategy = 45
PAPER_PORTS = (7497, 4002)   # same hard guard as the Momentum trader

# Position sizing: option premium paid per trade, as a fraction of a NOTIONAL
# strategy account (not the IBKR paper account's value, which also holds other
# strategies). 1% of $100k = about $1,000 of premium per trade. Matches the
# starting balance of the DTS Trading Journal (2026-10-15).
SIZING_ACCOUNT = 100_000
# Size by confluence (2026-10-16, was a flat 1%): the entry alert carries DTS
# Entry's setup grade (points for clean trend, VWAP side, beyond the opening
# range, beyond the prior-week level; OUTSIDE = beyond both PDH/PMH or PDL/PML).
# Only the strongest confluence gets the full 5%. With MAX_OPEN_POSITIONS = 10,
# at most 25-50% of the account is in premium at once (2.5-5% each).
SIZE_BY_GRADE = {
    "A+ OUTSIDE": 0.05,    # all 4 confirmations and outside the day + premarket range
    "A OUTSIDE": 0.035,    # 3 of 4, outside
}
SIZE_DEFAULT = 0.025       # everything else, and alerts without a grade
MIN_CONTRACTS = 1            # if 1% buys less than one contract, buy one anyway

# Contract selection. Expiry: this week's last listed expiry (normally Friday),
# even on Friday itself (0DTE), per the agreed rule. Strike: nearest to the
# TradingView entry price; if that strike fails the liquidity check, the next
# nearest is tried.
# Never in the money (2026-10-16): the ladder starts at the at-the-money strike.
# A strike in the money by no more than this % of the price still counts as at
# the money (e.g. the 217.5 call with the stock at 217.70).
ATM_TOLERANCE_PCT = 0.10
# Strike ladder (2026-10-15): from the strike nearest the money, step out of the
# money until a strike buys TARGET_CONTRACTS within the budget; failing that,
# MIN_CONTRACTS_TO_TRIM (so half can be sold at 2R and half trailed); never
# below MIN_DELTA. If none qualifies, buy 1 contract of the nearest liquid strike.
TARGET_CONTRACTS = 4
MIN_CONTRACTS_TO_TRIM = 2
MIN_DELTA = 0.15             # weeklies held 1-2 days: further OTM is fine, rides the volatility spike (was 0.25 until 2026-10-16)
# On expiry day (normally Friday) the same-day option is bought only inside this New
# York window; earlier entries (first-hour decay, 2026-10-16) and afternoon entries
# (little left to run for, 2026-10-18) buy next week's expiry instead.
ZERO_DTE_WINDOW = ((10, 30), (12, 0))
# Journal days from this date use fills re-priced from 1-minute history (reprice.py);
# earlier days keep the executor's recorded figures (user decision, 2026-10-16).
REPRICE_FROM = "2026-10-16"
MIN_DELTA_0DTE = 0.15        # expiry day (normally Friday): explicit so a later weekday change doesn't touch it; 0.25 is hard to find out of the money on 0DTE
STRIKES_TO_SCAN = 12          # out-of-the-money strikes examined beyond the nearest one
MIN_OPEN_INTEREST = 100
MIN_OPTION_VOLUME = 100     # contracts traded today in that strike (2026-10-16); unknown volume fails
MAX_SPREAD_FRACTION = 0.15   # (ask - bid) / mid must be at or below this
MIN_OPTION_PRICE = 0.50      # mid of at least $0.50 (2026-10-18): KO 94C at $0.05 x277 lost 90% on Oct 16

# Trade filters (2026-10-16, from the Oct 14-15 dry runs).
# Skip entries whose TradingView stop is closer than this to the entry, as a %
# of price: on those days every such stop was a one-bar stop-out or a target
# smaller than the option spread (8 trades, -$2,557 realistic).
MIN_STOP_PCT = 0.20
# The option's expected gain at the 2R target (|delta| x the stock move to the
# target) must be at least this multiple of the strike's bid/ask spread, or the
# spread eats the win (JNJ on Oct 14: TradingView 2R winner, options -$1,755).
MIN_TARGET_TO_SPREAD = 2.0

# At the 2R alert: sell half (rounded down). A single contract is held through 2R
# and exits on the trail, break-even, stop or time exit.
TRIM_FRACTION = 0.5

# Hard limits.
MAX_OPEN_POSITIONS = 10      # raised from 5 (2026-10-18): on Oct 16 all 5 signals the cap blocked reached 2R; 10 would have taken them all
MAX_HOLD_SESSIONS = 2        # mirrors "Max sessions held" on DTS Entry (backup only)

# Time rules (New York). Entries only inside the regular session; the safety
# sweep runs after TradingView's own 15:55 alerts have had time to arrive.
ENTRY_START = (9, 30)
ENTRY_END = (16, 0)
SWEEP_AT = (15, 56)

ORDER_FILL_TIMEOUT_SECONDS = 30

# IBKR connection watch. Set while TWS isn't accepting API connections, so the
# Telegram warning goes out once per outage, plus once when it comes back.
IBKR_DOWN_FILE = STATE_DIR / "dts_ibkr_down"
HEALTH_START = (9, 25)        # the sweep job checks the TWS port every 5 min from here
HEALTH_END = (16, 0)
