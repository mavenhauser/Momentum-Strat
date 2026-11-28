#!/bin/bash
# DTS keep-awake: stop the Mac idle-sleeping during the US session so TradingView
# alerts, the executor, TWS and the 15:56 sweep keep running.
#
# launchd (com.jameswoo.dts-keepawake) runs this every 2 minutes. Inside the
# window (Mon-Fri 09:10-16:10 New York time, DST handled by the TZ) it execs
# `caffeinate` until the window ends; launchd never starts a second copy while
# one is running. Outside the window it exits at once.
#
# Limits: caffeinate cannot stop sleep when the lid is closed (unless an external
# display is attached), and -s (no system sleep) only holds on AC power. A Mac
# that is already asleep at 09:10 is not woken: use `sudo pmset repeat wake ...`.
# Installed copy: ~/.dts-executor/dts_keepawake.sh (outside Documents, for launchd).

START=0910
END=1610
dow=$(TZ=America/New_York date +%u)          # 1 = Monday
hm=$(TZ=America/New_York date +%H%M)
[ "$dow" -le 5 ] || exit 0
[ $((10#$hm)) -ge $((10#$START)) ] && [ $((10#$hm)) -lt $((10#$END)) ] || exit 0

now=$(date +%s)
end_epoch=$(TZ=America/New_York date -j -f "%Y-%m-%d %H%M" "$(TZ=America/New_York date +%F) $END" +%s)
secs=$((end_epoch - now))
[ "$secs" -gt 0 ] || exit 0
echo "$(date '+%F %T') keep-awake on for ${secs}s (until ${END} ET)"
exec /usr/bin/caffeinate -i -m -s -t "$secs"
