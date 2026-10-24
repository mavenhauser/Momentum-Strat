#!/usr/bin/env bash
#
# Refreshes DTS Screener's ticker list, and the TradingView alerts that follow
# it (one DTS Entry alert per ticker plus the screener alert), twice each
# trading morning, 08:30 and
# 09:15 America/New_York, Monday-Friday: 08:30 so the list is there while
# picking names, 09:15 so the final list reflects the late premarket movers.
# Launchd's StartInterval polls this wrapper every 5 minutes; the marker file
# is what prevents double-firing and lets a missed slot (Mac asleep) catch up
# once on wake. Catch-up is allowed until 12:00 because the feed keeps the
# day's premarket figures after the open.
#
# If TradingView isn't running at all, the wrapper starts it in the background
# with the debugging port the updater needs. It never closes or restarts a
# TradingView that is already open: one opened without the port (from the Dock)
# can't be reached, so the slot fails, a notification says so, and the next
# poll retries. A slot is only marked done when the update succeeds.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MARKER_FILE="$SCRIPT_DIR/.last_run_slot"
FAIL_FILE="$SCRIPT_DIR/.last_fail_slot"
UPDATER="$SCRIPT_DIR/update_screener_list.mjs"
NODE="$(command -v node || echo /opt/homebrew/bin/node)"

et_date="$(TZ='America/New_York' date +%Y-%m-%d)"
et_hour="$(TZ='America/New_York' date +%H)"
et_minute="$(TZ='America/New_York' date +%M)"
et_dow="$(TZ='America/New_York' date +%u)"  # 1=Mon .. 7=Sun
now=$((10#$et_hour * 60 + 10#$et_minute))

SLOT_1=$((8 * 60 + 30))    # 08:30 ET
SLOT_2=$((9 * 60 + 15))    # 09:15 ET
WINDOW_END=$((12 * 60))    # 12:00 ET

# Skip weekends and anything outside 08:30-12:00.
if [ "$et_dow" -ge 6 ] || [ "$now" -lt "$SLOT_1" ] || [ "$now" -gt "$WINDOW_END" ]; then
  exit 0
fi

if [ "$now" -ge "$SLOT_2" ]; then slot="$SLOT_2"; else slot="$SLOT_1"; fi
slot_id="${et_date}_${slot}"

if [ -f "$MARKER_FILE" ] && [ "$(cat "$MARKER_FILE")" = "$slot_id" ]; then
  exit 0
fi

CDP_PORT=9222
TV_BIN="/Applications/TradingView.app/Contents/MacOS/TradingView"
chart_tabs() {
  curl -s -m 3 "http://localhost:$CDP_PORT/json/list" 2>/dev/null | grep -c 'tradingview.com/chart' || true
}

echo "=== $(TZ='America/New_York' date '+%Y-%m-%d %H:%M:%S') ET slot $slot_id"

attempts=1
reason="TradingView is open without its connection. Quit it and reopen with TradingView Connected."
if [ "$(chart_tabs)" -eq 0 ] && ! pgrep -f "$TV_BIN( |\$)" >/dev/null; then
  echo "TradingView not running - starting it with --remote-debugging-port=$CDP_PORT"
  open -g -a TradingView --args "--remote-debugging-port=$CDP_PORT" || true
  for _ in $(seq 1 30); do
    [ "$(chart_tabs)" -gt 0 ] && break
    sleep 3
  done
  sleep 20          # let the layouts and indicators finish loading
  attempts=3
  reason="TradingView was started but did not finish loading in time."
fi

ok=0
for i in $(seq 1 "$attempts"); do
  if "$NODE" "$UPDATER" --apply --alerts; then ok=1; break; fi
  [ "$i" -lt "$attempts" ] && sleep 15
done

if [ "$ok" -eq 1 ]; then
  echo "$slot_id" > "$MARKER_FILE"
else
  if [ ! -f "$FAIL_FILE" ] || [ "$(cat "$FAIL_FILE")" != "$slot_id" ]; then
    echo "$slot_id" > "$FAIL_FILE"
    osascript -e "display notification \"$reason Retrying every 5 minutes until 12:00 ET.\" with title \"DTS Screener list / alerts not updated\"" >/dev/null 2>&1 || true
  fi
fi
