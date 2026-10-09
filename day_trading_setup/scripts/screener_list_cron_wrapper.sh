#!/usr/bin/env bash
#
# Refreshes DTS Screener's ticker list twice each trading morning, 08:30 and
# 09:15 America/New_York, Monday-Friday: 08:30 so the list is there while
# picking names, 09:15 so the final list reflects the late premarket movers.
# Launchd's StartInterval polls this wrapper every 5 minutes; the marker file
# is what prevents double-firing and lets a missed slot (Mac asleep) catch up
# once on wake. Catch-up is allowed until 12:00 because the feed keeps the
# day's premarket figures after the open.
#
# A slot is only marked done when the update succeeds, so if TradingView isn't
# reachable the next poll retries. One macOS notification per failed slot.

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

echo "=== $(TZ='America/New_York' date '+%Y-%m-%d %H:%M:%S') ET slot $slot_id"
if "$NODE" "$UPDATER" --apply; then
  echo "$slot_id" > "$MARKER_FILE"
else
  if [ ! -f "$FAIL_FILE" ] || [ "$(cat "$FAIL_FILE")" != "$slot_id" ]; then
    echo "$slot_id" > "$FAIL_FILE"
    osascript -e 'display notification "TradingView was not reachable. Retrying every 5 minutes until 12:00 ET." with title "DTS Screener list not updated"' >/dev/null 2>&1 || true
  fi
fi
