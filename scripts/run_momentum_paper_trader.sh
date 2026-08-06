#!/bin/bash
# Standalone wrapper for scripts/run_momentum_paper_trader.py - activates
# the venv and logs to logs/momentum_trader.log. Handy for a manual/ad-hoc
# run (e.g. `./scripts/run_momentum_paper_trader.sh --dry-run` doesn't work
# since this doesn't forward args - just run the .py directly for that).
#
# NOT what's actually installed in cron: this user's crontab already sets
# `TZ=America/New_York` globally, so the installed entry uses real ET hours
# directly (matching scripts/run_paper_trader.py's existing crontab style)
# rather than this wrapper:
#   */15 9-14 * * 1-5 cd "<repo>/Momentum Strat" && venv/bin/python3 -u scripts/run_momentum_paper_trader.py >> logs/momentum_trader.log 2>&1
# (path updated 2026-08-06 when this project split out of the old combined
# "Algo Trading" repo into its own repo - see `crontab -l`.) The Python
# script is still a no-op outside its own 9:45-14:00 ET window regardless,
# as a second layer of defense if the schedule ever drifts.
set -euo pipefail
cd "$(dirname "$0")/.."
source venv/bin/activate
python scripts/run_momentum_paper_trader.py >> logs/momentum_trader.log 2>&1
