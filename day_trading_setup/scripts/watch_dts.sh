#!/bin/bash
# Report DTS executor notifications, resuming from where the last watch stopped,
# so nothing is lost while the watch is being re-armed.
LOG="/Users/James Woo/Documents/Trading/Momentum Strat/logs/dts_executor.log"
OFF="${DTS_WATCH_OFFSET:-$HOME/.dts-executor/watch.offset}"
[ -f "$OFF" ] || stat -f %z "$LOG" > "$OFF"
while true; do
  size=$(stat -f %z "$LOG"); off=$(cat "$OFF")
  [ "$size" -lt "$off" ] && off=0
  if [ "$size" -gt "$off" ]; then
    tail -c +$((off + 1)) "$LOG" | head -c $((size - off)) \
      | grep -E "notify:|APLUS alert|ERROR|Traceback|failed handling" \
      | grep -v -E "Make sure API port|API connection failed|Connect call failed|Error 10091" \
      | sed -E 's/^[0-9-]+ ([0-9:]+),[0-9]+ INFO (notify: )?/\1 /' | cut -c1-300
    echo "$size" > "$OFF"
  fi
  sleep 3
done
