-- "TradingView Connected": opens TradingView Desktop with the debugging port that the
-- DTS Screener daily list (and the other TradingView automations) need. Use it instead
-- of the normal TradingView icon. Build:
--   osacompile -o ~/Applications/"TradingView Connected.app" tradingview_connected.applescript
set tvBin to "/Applications/TradingView.app/Contents/MacOS/TradingView"
set tvProc to quoted form of (tvBin & "( |$)")

set portUp to (do shell script "curl -s -m 2 http://localhost:9222/json/version >/dev/null 2>&1 && echo yes || echo no")
if portUp is "yes" then
	-- Already connected: just bring it to the front.
	do shell script "open -a TradingView"
	return
end if

set isRunning to (do shell script "pgrep -f " & tvProc & " >/dev/null && echo yes || echo no")
if isRunning is "yes" then
	set answer to button returned of (display dialog "TradingView is open without its connection, so the daily screener list and other automations can't reach it." & return & return & "Restart it now? It takes about 20 seconds and your tabs come back." buttons {"Not now", "Restart"} default button "Restart" with title "TradingView Connected")
	if answer is "Not now" then return
	do shell script "pkill -f " & tvProc & "; for i in $(seq 1 20); do pgrep -f " & tvProc & " >/dev/null || break; sleep 1; done"
	delay 2
end if

do shell script "open -a TradingView --args --remote-debugging-port=9222"
