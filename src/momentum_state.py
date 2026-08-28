"""
Position-state persistence for scripts/run_momentum_paper_trader.py.

Unlike scripts/run_paper_trader.py (one long-lived process, in-memory
state), the momentum trader is invoked fresh once an hour by cron/launchd -
nothing survives between invocations unless it's written to disk. This is
the only state-persistence code in the repo; there's no existing pattern to
follow, so it's kept deliberately simple: one JSON file, atomic writes.

Position schema (a ticker key is only present while it has at least one
open lot; the value is a LIST of lots, not a single dict, because a
delayed/late fill on an order the script already gave up waiting on can
land after a later cycle has opened a fresh lot on the same ticker -
seen for real 2026-08-07 on PLTR - so a single-slot-per-ticker schema
would silently lose track of one of them):
    {
      "<TICKER>": [
        {
          "strike": float, "expiry": "YYYYMMDD", "streamer_symbol": str,
          "contracts_remaining": int,
          "entry_premium": float, "entry_underlying_price": float,
          "target_price": float | None,     # measured-move trim target set at
                                             # entry (2026-08-15) - None on
                                             # positions opened before this
                                             # existed, or if there wasn't
                                             # enough daily history at entry
          "entry_date": "YYYY-MM-DD",
          "trims_done": [float, ...],       # R-multiples already trimmed at
          "breakeven_active": bool,
          "itm_extension_deadline": "YYYY-MM-DD" | null,  # set once the time
                                                           # exit is reached ITM
        },
        ...
      ]
    }
"""
import json
import os
from pathlib import Path

from src import config

STATE_PATH = Path(__file__).resolve().parent.parent / config.MOMENTUM_STATE_PATH
LOCK_PATH = Path(__file__).resolve().parent.parent / "state" / "run_momentum_paper_trader.lock"


def load_state(path=None):
    path = Path(path) if path else STATE_PATH
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def save_state(state, path=None):
    path = Path(path) if path else STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp_path, path)  # atomic on POSIX - a crash mid-write can't corrupt the real file


def acquire_lock(path=None):
    """True if the lock was acquired (safe to proceed), False if another
    instance is genuinely still running (checked via os.kill(pid, 0), not
    just the lock file's age - a run can legitimately take several minutes
    during an IBKR connectivity stall, as seen 2026-08-05, so a fixed
    timeout would either block a healthy run or fail to block a real
    overlap). A lock left behind by a crashed/killed process whose PID no
    longer exists is treated as stale and silently taken over."""
    path = Path(path) if path else LOCK_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            existing_pid = int(path.read_text().strip())
        except ValueError:
            existing_pid = None
        if existing_pid is not None:
            try:
                os.kill(existing_pid, 0)
                return False  # process exists - still running
            except ProcessLookupError:
                pass  # stale - that process is gone, safe to take over
            except PermissionError:
                return False  # exists (just not signalable) - treat as still running
    path.write_text(str(os.getpid()))
    return True


def release_lock(path=None):
    path = Path(path) if path else LOCK_PATH
    try:
        path.unlink()
    except FileNotFoundError:
        pass
