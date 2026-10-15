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
RISK_FRACTION = 0.01
MIN_CONTRACTS = 1            # if 1% buys less than one contract, buy one anyway

# Contract selection. Expiry: this week's last listed expiry (normally Friday),
# even on Friday itself (0DTE), per the agreed rule. Strike: nearest to the
# TradingView entry price; if that strike fails the liquidity check, the next
# nearest is tried.
STRIKES_TO_TRY = 4
MIN_OPEN_INTEREST = 100
MAX_SPREAD_FRACTION = 0.15   # (ask - bid) / mid must be at or below this

# At the 2R alert: sell half (rounded down). A single contract is sold whole.
TRIM_FRACTION = 0.5

# Hard limits.
MAX_OPEN_POSITIONS = 5
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
