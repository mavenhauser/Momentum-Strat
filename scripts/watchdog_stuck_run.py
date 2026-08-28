"""
Safety net for scripts/run_momentum_paper_trader.py: kills a run that's been
holding state/run_momentum_paper_trader.lock for too long.

Why this exists: a run can get stuck indefinitely if IBKR's API goes into a
degraded state (seen for real 2026-08-10 -> 2026-08-12 - a stale account
summary subscription left behind by an earlier `kill -9` caused ~44 hours of
Error 322 / Error 1100 flapping with the process never exiting on its own).
The main script has no self-timeout, and nothing else was watching, so the
cron lock silently blocked every subsequent tick for two full trading days
with no alert. This is meant to be invoked every few minutes by cron,
independently of the main */15 trading schedule.

Only ever touches the lock/process - never connects to IBKR/TastyTrade.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.momentum_state import LOCK_PATH, release_lock  # noqa: E402
from src.telegram_client import send_message  # noqa: E402

STUCK_THRESHOLD_SECONDS = 600  # 10 min - generous vs. the ~8 min slow-but-
                                # legitimate recovery seen 2026-08-06


def parse_etime_to_seconds(etime_str):
    """Parse macOS ps's `etime` format: [[dd-]hh:]mm:ss (no `etimes`/seconds
    keyword exists on BSD ps, unlike Linux)."""
    days = 0
    if "-" in etime_str:
        days_str, etime_str = etime_str.split("-", 1)
        days = int(days_str)
    parts = [int(p) for p in etime_str.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    hours, minutes, seconds = parts
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def main():
    if not LOCK_PATH.exists():
        return

    try:
        pid = int(LOCK_PATH.read_text().strip())
    except ValueError:
        return

    result = subprocess.run(
        ["ps", "-o", "etime=", "-p", str(pid)],
        capture_output=True, text=True,
    )
    output = result.stdout.strip()
    if not output:
        return  # PID isn't alive - stale lock, let acquire_lock() self-heal it

    elapsed_seconds = parse_etime_to_seconds(output)
    if elapsed_seconds <= STUCK_THRESHOLD_SECONDS:
        return

    print(f"Watchdog: PID {pid} has held the lock for {elapsed_seconds}s "
          f"(> {STUCK_THRESHOLD_SECONDS}s threshold) - killing it.")
    subprocess.run(["kill", "-9", str(pid)])
    release_lock()
    minutes = elapsed_seconds // 60
    send_message(
        f"*Momentum Trader* - Watchdog killed a stuck run (PID {pid}, "
        f"stuck {minutes}m) and cleared the lock so the next cycle can run."
    )


if __name__ == "__main__":
    main()
