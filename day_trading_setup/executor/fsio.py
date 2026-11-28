"""File access that survives iCloud Drive (2026-11-28 system check).

The repo sits in ~/Documents, which iCloud Drive syncs. While iCloud is syncing a file, macOS
can refuse to open it with EDEADLK ("Resource deadlock avoided"). It happened on Oct 27-28,
Nov 12, Nov 14 and twice on Nov 27: at 12:25:05 ET eight alerts failed together opening the
executor's lock (nothing was held then, so nothing was missed). An alert whose executor fails
that way is lost: the receiver has already logged it, so the missed-alert backup won't re-send it.
So the lock lives outside iCloud (settings.LOCK_FILE in ~/.dts-executor) and is opened without
truncating it, and file access the alerts depend on is retried briefly on EDEADLK.
"""
import errno
import time
from pathlib import Path

from day_trading_setup.executor import settings

RETRY_ERRNOS = {errno.EDEADLK}
ATTEMPTS = 40              # 40 x 0.25 s = 10 s at most
PAUSE = 0.25


def retry(fn, *args, **kwargs):
    """fn(*args, **kwargs), retried on a transient iCloud refusal (EDEADLK)."""
    for attempt in range(ATTEMPTS):
        try:
            return fn(*args, **kwargs)
        except OSError as e:
            if e.errno not in RETRY_ERRNOS or attempt == ATTEMPTS - 1:
                raise
            time.sleep(PAUSE)


def open_lock():
    """The executor's lock file, opened for flock without rewriting it (mode "a")."""
    path = Path(settings.LOCK_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    return retry(open, path, "a")
