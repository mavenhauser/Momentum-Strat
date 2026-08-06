#!/bin/bash
# One-shot gate for scripts/check_momentum_option_order.py: does nothing
# unless (a) it hasn't already run today, and (b) IBKR TWS is actually
# reachable right now (i.e. you've opened it and logged into the paper
# account). Meant to be invoked every few minutes across a wide window by
# cron - that stands in for "run once whenever TWS becomes available
# today" without needing a login-triggered launchd watcher. See the
# crontab line noted when this was installed (`crontab -l`).
set -uo pipefail
cd "$(dirname "$0")/.."

MARKER="state/check_momentum_option_order.lastrun"
TODAY=$(date +%Y-%m-%d)

mkdir -p state
if [ -f "$MARKER" ] && [ "$(cat "$MARKER")" = "$TODAY" ]; then
    exit 0  # already ran today - no-op
fi

source venv/bin/activate
PORT=$(python3 -c "from src import config; print(config.IBKR_PORT)")

if ! nc -z -w2 127.0.0.1 "$PORT" 2>/dev/null; then
    exit 0  # TWS not up/logged in yet - try again next tick
fi

echo "$TODAY" > "$MARKER"
python3 scripts/check_momentum_option_order.py >> logs/check_momentum_option_order.log 2>&1
