#!/usr/bin/env python3
"""
Momentum strategy scanner — matches the live "Momentum Strategy" Pine script
(Variant H), purely to ALERT via Telegram when a play becomes valid. It never
places orders; execution stays manual in TWS. Sends to the same Telegram bot
and channel as the premarket gappers/losers scanners (~/.trading-scanners/telegram.env).

Entry criteria (long only — Variant H has no short side):
  - Trend filter: previous day's close > the 200-day SMA (as of the day
    before that).
  - Daily breakout: close > previous day's high.
  - Premarket breakout: close > today's premarket high.
  - Fresh high-of-day: close > today's regular-session high-so-far (excluding
    the bar itself, so the signal never uses a bar's own extreme against
    itself).
  - Hourly check with a 6-candle (trailing-hour) lookback: rather than firing
    the instant all four conditions align on the latest bar, this checks
    whether they aligned on *any* of the last 6 regular-session bars (the
    past hour, since bars are 10 minutes) - matches how the Pine script's
    `ta.highest(breakoutNow, 6)` latches a breakout from within the last hour
    rather than requiring it on the exact current bar. The alert price is
    always the *current* bar's close, not the bar where the breakout first
    triggered - that's the only price actually tradable from a scan that
    isn't checking every single bar in real time.
  - Time gate: only considered at/after 10:00 ET.

Full backtest record (why H was chosen over the other variants tested) lives
at ~/Documents/momentum-strategy/docs/momentum_strategy_backtest_record.html
and the live Pine source at ~/Documents/momentum-strategy/pine/momentum_strategy.pine.

Data source: TradingView Desktop, driven headlessly via `claude -p` and the
tradingview MCP server (chart_set_symbol / chart_set_timeframe /
data_get_ohlcv), reading raw price bars off the live chart and computing
SMA200/PMH/prev-day-high ourselves in Python - deliberately NOT parsing
indicator label text, which proved incomplete/unreliable (custom indicators
on the chart don't reliably expose distinctly-labeled values via
data_get_study_values). Previously used IBKR/TWS via ib_insync; switched away
after TWS's historical/live data requests got stuck behind an unresolved
account-level "different IP address" error (see project memory).

Requires:
  - TradingView Desktop running with CDP enabled (remote-debugging-port=9222)
    and a chart open with the "Momentum Strategy" indicator loaded.
  - CLAUDE_CODE_OAUTH_TOKEN set (see `claude setup-token`).
  - The tradingview MCP server registered in ~/.claude.json.

Each `claude -p` call MUST explicitly call ToolSearch to load the tradingview
MCP tool schemas before using them - Claude Code defers loading schemas for
MCP servers with many tools, and a plain "just call the tool" prompt silently
sees no matching tool and fails, inconsistently, without that step.

State + Telegram gating: only sends a Telegram message on the first run of
the trading day, when a ticker shows a *new* hit not already reported today,
or on error. Otherwise stays silent. State is kept in
momentum_state_YYYY-MM-DD.json in this directory.

Each new hit's alert also includes a suggested long call contract from
momentum_option_picker.py (IBKR paper account, delta/volume/expiry filters,
1%-of-NLV sizing) - a suggestion only, never an order. If IBKR/market data
is unavailable that line degrades to a short note instead of blocking the
underlying stock-signal alert, which is the part that actually matters.

Usage: ./momentum_strategy_scanner.py
"""

import json
import subprocess
import sys
import traceback
from datetime import datetime, time as dtime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import momentum_option_picker  # noqa: E402 - needs sys.path set first
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
TELEGRAM_ENV_FILE = Path.home() / ".trading-scanners" / "telegram.env"

TICKERS = ["AMD", "NVDA", "MU", "TSLA", "DELL", "AVGO", "PLTR", "MSFT", "AAPL", "GOOGL"]

MCP_CONFIG = json.dumps({
    "mcpServers": {
        "tradingview": {
            "type": "stdio",
            "command": "/opt/homebrew/bin/node",
            "args": ["/Users/James Woo/tradingview-mcp/src/server.js"],
            "env": {},
        }
    }
})
CLAUDE_MAX_BUDGET_USD = "1.50"

# The scan changes the live chart's symbol/timeframe, so it only runs while
# TradingView's active layout has exactly this name - any other layout is left alone.
SCAN_LAYOUT_NAME = "Main"


class WrongLayout(Exception):
    """TradingView's active layout isn't SCAN_LAYOUT_NAME - chart left untouched."""

# Deterministic tab finder (no LLM): a read-only CDP call that locates the tab whose layout is
# exactly SCAN_LAYOUT_NAME, even when it's a background tab, and the MCP is then pinned to it
# (TV_TARGET_ID). Runs before every `claude -p`; no such tab = no scan. The in-prompt layout
# check below stays as a backstop in case that tab is switched to another layout mid-scan.
LAYOUT_CHECK_SCRIPT = Path(__file__).resolve().parent / "tv_layout_check.mjs"
NODE_BIN = "/opt/homebrew/bin/node"


def find_scan_tab() -> str:
    """CDP target id of the TradingView tab whose layout is exactly SCAN_LAYOUT_NAME, found whether
    or not it's the tab in front (the scan is pinned to it, so other tabs/windows are never touched).
    Raises WrongLayout if no tab has that layout; RuntimeError if TradingView can't be read at all
    (fail closed - never fall back to driving whichever tab happens to be visible)."""
    try:
        r = subprocess.run([NODE_BIN, str(LAYOUT_CHECK_SCRIPT), SCAN_LAYOUT_NAME],
                           capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise RuntimeError("layout pre-check timed out")
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        raise RuntimeError(f"layout pre-check gave no JSON (exit {r.returncode}): {(r.stdout + r.stderr)[-300:]}")
    if r.returncode != 0 or "error" in out:
        raise RuntimeError(f"layout pre-check failed: {out.get('error', out)}")
    if not out.get("target_id"):
        open_layouts = ", ".join(repr(t.get("layout")) for t in out.get("tabs", [])) or "none"
        raise WrongLayout(f"open layouts: {open_layouts}")
    return out["target_id"]


def mcp_config(target_id: str) -> str:
    """MCP_CONFIG with the tradingview server pinned to one tab (TV_TARGET_ID, see its connection.js)."""
    cfg = json.loads(MCP_CONFIG)
    cfg["mcpServers"]["tradingview"]["env"] = {"TV_TARGET_ID": target_id}
    return json.dumps(cfg)

DAILY_BAR_COUNT = 210   # need 200 for SMA200 + prev day, small buffer
INTRADAY_BAR_COUNT = 150  # comfortably covers today + premarket
INTRADAY_TIMEFRAME = "10"  # 10-minute bars, matches the Pine script's chart resolution

ET = ZoneInfo("America/New_York")
REGULAR_START = dtime(9, 30)
REGULAR_END = dtime(16, 0)
ENTRY_GATE_START = dtime(10, 0)  # Variant G only considers entries at/after 10:00 ET

BARS_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "bars": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "time": {"type": "number"},
                    "open": {"type": "number"},
                    "high": {"type": "number"},
                    "low": {"type": "number"},
                    "close": {"type": "number"},
                    "volume": {"type": "number"},
                },
                "required": ["time", "open", "high", "low", "close", "volume"],
            },
        },
        "skipped": {"type": "string"},
    },
    "required": ["bars"],
})


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def load_telegram_credentials():
    if not TELEGRAM_ENV_FILE.exists():
        return None, None, None
    bot_token = chat_id = channel_id = ""
    for line in TELEGRAM_ENV_FILE.read_text().splitlines():
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            bot_token = line.split("=", 1)[1].strip()
        elif line.startswith("TELEGRAM_CHAT_ID="):
            chat_id = line.split("=", 1)[1].strip()
        elif line.startswith("TELEGRAM_CHANNEL_ID="):
            channel_id = line.split("=", 1)[1].strip()
    return bot_token or None, chat_id or None, channel_id or None


def send_telegram(text):
    """Returns True if the message reached at least one destination, False
    otherwise - callers use this to decide whether it's safe to mark hits as
    reported in state (a failed send must not be treated as delivered, or a
    real signal can silently go unreported forever)."""
    # Shells out to curl (system trust store) rather than Python's urllib/ssl:
    # a fresh venv's bundled CA set doesn't include this sandbox's intercepting
    # proxy cert, which makes urlopen fail with CERTIFICATE_VERIFY_FAILED even
    # though the same request over curl succeeds fine (as the other scanners do).
    bot_token, chat_id, channel_id = load_telegram_credentials()
    if not bot_token:
        log(f"Telegram: bot token missing in {TELEGRAM_ENV_FILE}, skipping send.")
        return False

    sent_any = False
    for dest in (chat_id, channel_id):
        if not dest:
            continue
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        try:
            result = subprocess.run(
                [
                    "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                    url,
                    "-d", f"chat_id={dest}",
                    "--data-urlencode", f"text={text}",
                    "-d", "parse_mode=Markdown",
                ],
                capture_output=True, text=True, timeout=15,
            )
            http_code = result.stdout.strip()
            if http_code != "200":
                log(f"Telegram: send failed for chat_id={dest} (HTTP {http_code}): {result.stderr}")
            else:
                log(f"Telegram: message sent to chat_id={dest}.")
                sent_any = True
        except Exception as e:
            log(f"Telegram: send failed for chat_id={dest} (exception, scan continues): {e}")

    if not sent_any:
        log("Telegram: no destination succeeded.")

    return sent_any


def r2(x):
    return round(x, 2)


def fetch_bars(symbol, timeframe, count, extended_hours=False):
    """Shells out to `claude -p` to drive the live TradingView chart and pull
    raw bars for one timeframe. Returns a list of dicts sorted oldest-first,
    or raises on failure. Kept as one (symbol, timeframe) per call - an
    earlier version that set the symbol once and fetched both daily and
    intraday bars in a single combined call (6 sequential tool calls) timed
    out at 180s; splitting into two independent lighter calls is more
    reliable even though it re-sets the symbol each time."""
    target_id = find_scan_tab()  # deterministic gate: no tab on the right layout = nothing below runs

    # The chart's session defaults to "regular" (09:30-16:00 ET only), which
    # silently excludes premarket bars from data_get_ohlcv - confirmed via
    # ui_evaluate that mainSeries().properties().childs().sessionId must be
    # set to "extended" (0400-2000 ET) to include them. No dedicated MCP tool
    # for this, so it's set directly via ui_evaluate before fetching.
    session_step = (
        "2) use ui_evaluate to run this exact JS and confirm it returns 'extended': "
        "window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().mainSeries().properties().childs().sessionId.setValue('extended'); "
        "window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().mainSeries().properties().childs().sessionId.value() "
        if extended_hours else ""
    )
    tools = (
        "mcp__tradingview__chart_set_symbol,mcp__tradingview__chart_set_timeframe,"
        "mcp__tradingview__data_get_ohlcv,mcp__tradingview__ui_evaluate"
    )

    layout_guard = (
        "0) FIRST, before touching the chart, use ui_evaluate to run exactly: "
        "window.TradingViewApi.layoutName() . "
        f"If the result is not exactly '{SCAN_LAYOUT_NAME}', STOP IMMEDIATELY: make no further tool calls "
        "(do NOT call chart_set_symbol or chart_set_timeframe) and return ONLY "
        '{"bars": [], "skipped": "<the layout name you read>"}. '
        f"Only if it is exactly '{SCAN_LAYOUT_NAME}', continue with the steps below. "
    )

    prompt = (
        f"First call ToolSearch with query 'select:{tools}' to load schemas. Then: "
        f"{layout_guard}"
        f"1) call chart_set_symbol with symbol='{symbol}'. "
        f"{session_step}"
        f"3) call chart_set_timeframe with timeframe='{timeframe}'. "
        f"4) call data_get_ohlcv with count={count}, summary=false to get raw bars. "
        f'Return ONLY a JSON object of the form {{"bars": [...]}} '
        f"containing the raw bar array from step 4 (each bar as "
        f"{{time, open, high, low, close, volume}}), no extra commentary."
    )

    raw = subprocess.run(
        [
            "claude", "-p", prompt,
            "--print", "--output-format", "json",
            "--json-schema", BARS_SCHEMA,
            "--mcp-config", mcp_config(target_id),
            "--strict-mcp-config",
            "--permission-mode", "bypassPermissions",
            "--no-session-persistence",
            "--max-budget-usd", CLAUDE_MAX_BUDGET_USD,
        ],
        capture_output=True, text=True, timeout=180,
    )

    if raw.returncode != 0:
        raise RuntimeError(f"claude -p exited {raw.returncode}: {raw.stderr[-500:]}")

    try:
        outer = json.loads(raw.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"claude -p produced non-JSON output: {e}; stdout tail: {raw.stdout[-500:]}")

    if outer.get("is_error"):
        raise RuntimeError(f"claude -p reported an error: {outer.get('result')}")

    payload = outer.get("structured_output")
    if not payload:
        result_text = outer.get("result", "")
        try:
            payload = json.loads(result_text)
        except (json.JSONDecodeError, TypeError):
            raise RuntimeError(f"No structured_output and .result isn't JSON: {result_text[:500]}")

    if payload.get("skipped") is not None:
        raise WrongLayout(payload["skipped"])

    bars = sorted(payload.get("bars", []), key=lambda b: b["time"])
    if not bars:
        raise RuntimeError("Empty bars array returned")

    return bars


def fetch_bars_via_tradingview(symbol):
    """Fetches (daily_bars, intraday_bars) for symbol as two separate calls."""
    daily_bars = fetch_bars(symbol, "1D", DAILY_BAR_COUNT)
    intraday_bars = fetch_bars(symbol, INTRADAY_TIMEFRAME, INTRADAY_BAR_COUNT, extended_hours=True)
    return daily_bars, intraday_bars


def fetch_ticker_signal(symbol, today_et):
    try:
        daily_bars, intraday_bars = fetch_bars_via_tradingview(symbol)
    except WrongLayout:
        raise
    except Exception as e:
        return {"symbol": symbol, "error": f"TradingView fetch failed: {e}"}

    def bar_et(b):
        return datetime.fromtimestamp(b["time"], tz=ET)

    daily_hist = [b for b in daily_bars if bar_et(b).date() < today_et]
    if len(daily_hist) < 200:
        return {"symbol": symbol, "error": f"only {len(daily_hist)} daily bars before today, need 200"}

    prev_day = daily_hist[-1]
    sma200 = sum(b["close"] for b in daily_hist[-200:]) / 200
    prev_day_high, prev_day_close = prev_day["high"], prev_day["close"]

    todays_bars = [b for b in intraday_bars if bar_et(b).date() == today_et]
    premarket_bars = [b for b in todays_bars if bar_et(b).time() < REGULAR_START]
    regular_bars = [b for b in todays_bars if REGULAR_START <= bar_et(b).time() < REGULAR_END]

    if not regular_bars:
        return {"symbol": symbol, "error": "no regular-session bars yet today"}

    pmh = max((b["high"] for b in premarket_bars), default=None)

    trend_ok = prev_day_close > sma200

    # Walk the regular-session bars in order, tracking "high so far" (which
    # excludes the current bar, matching Pine's prevSessionHigh), and record
    # whether the full breakout condition held on each bar - mirrors the
    # Pine script's `breakoutNow` computed every bar. The 6-candle lookback
    # below then checks whether it held on *any* of the trailing hour's bars,
    # not just the latest one.
    session_high_before = None
    breakout_flags = []
    for b in regular_bars:
        after_ten_am = bar_et(b).time() >= ENTRY_GATE_START
        close = b["close"]
        daily_breakout = close > prev_day_high
        premarket_breakout = pmh is not None and close > pmh
        fresh_hod = session_high_before is not None and close > session_high_before
        breakout_flags.append(after_ten_am and trend_ok and daily_breakout and premarket_breakout and fresh_hod)
        session_high_before = b["high"] if session_high_before is None else max(session_high_before, b["high"])

    recent_breakout = any(breakout_flags[-6:])

    latest_bar = regular_bars[-1]
    latest_close = latest_bar["close"]
    latest_time_et = bar_et(latest_bar)
    today_hod_before = max((b["high"] for b in regular_bars[:-1]), default=None)

    long_hit = recent_breakout

    return {
        "symbol": symbol,
        "side": "long" if long_hit else None,
        "price": r2(latest_close),
        "bar_time_et": latest_time_et.strftime("%H:%M"),
        "pmh": r2(pmh) if pmh is not None else None,
        "prev_day_high": r2(prev_day_high),
        "sma200": r2(sma200),
        "prev_day_close": r2(prev_day_close),
        "today_hod_before": r2(today_hod_before) if today_hod_before is not None else None,
    }


def justification_line(hit):
    return (
        f"  ↳ Long: prev close ${hit['prev_day_close']} > SMA200 ${hit['sma200']} (uptrend), "
        f"broke prev day high ${hit['prev_day_high']}, PMH ${hit['pmh']}, and today's high-so-far "
        f"${hit['today_hod_before']} within the last hour — current price ${hit['price']}"
    )


def format_option_suggestion(hit):
    """Looks up the qualifying long call via IBKR (delta/volume filters,
    next monthly OpEx >=45 days out) and formats it for the alert. Never
    raises - a lookup failure (IBKR down, market data unavailable, etc.)
    degrades to a one-line note rather than blocking the stock-signal alert,
    which is the part that actually matters if this piece breaks."""
    try:
        opt = momentum_option_picker.pick_for_signal(hit["symbol"], hit["price"])
    except Exception as e:
        return f"  ↳ Option suggestion unavailable: {e}"

    if "error" in opt:
        return f"  ↳ Option suggestion unavailable: {opt['error']}"

    qty = opt.get("suggested_qty")
    qty_text = f"{qty} contract(s)" if qty else f"qty n/a — {opt.get('qty_note', '')}"
    return (
        f"  ↳ 📞 Suggested: {opt['symbol']} {opt['expiry']} {opt['strike']:.0f}C "
        f"(delta {opt['delta']:.2f}, IV {opt['iv']*100:.0f}%{' ⚠️high' if opt['iv_flag']=='high' else ''}, "
        f"vol {opt['volume']:.0f}, OI {'unverified' if not opt['oi_verified'] else 'ok'}) — "
        f"{qty_text} (1% of {opt['account_ccy']} {opt['account_nlv']:,.0f} NLV). "
        f"Not an order — verify OI/ask in TWS before placing."
    )


def format_hit_line(hit):
    symbol, price = hit["symbol"], hit["price"]
    head = (
        f"• *{symbol} @ ${price}* "
        f"(PMH ${hit['pmh']}, prev_high ${hit['prev_day_high']}, SMA200 ${hit['sma200']})"
    )
    return head + "\n" + justification_line(hit) + "\n" + format_option_suggestion(hit)


def main():
    now_et = datetime.now(ET)
    today_et = now_et.date()
    state_file = SCRIPT_DIR / f"momentum_state_{today_et.isoformat()}.json"
    scan_out_file = SCRIPT_DIR / f"momentum_scan_{today_et.isoformat()}_{now_et.strftime('%H%M')}.json"

    is_first_run = not state_file.exists()
    state = {"first_run_sent": False, "hits": {}}
    if state_file.exists():
        state = json.loads(state_file.read_text())

    results = []
    errors = []
    for symbol in TICKERS:
        try:
            r = fetch_ticker_signal(symbol, today_et)
            results.append(r)
            if "error" in r:
                errors.append(f"{symbol}: {r['error']}")
        except WrongLayout as e:
            log(f"No usable '{SCAN_LAYOUT_NAME}' TradingView tab ({e}) - scan skipped, chart untouched.")
            print(f"Momentum Scanner: skipped (no usable '{SCAN_LAYOUT_NAME}' tab: {e}).")
            return
        except Exception as e:
            err = f"{symbol}: {e}"
            errors.append(err)
            results.append({"symbol": symbol, "error": str(e)})
            log(f"ERROR fetching {symbol}: {e}\n{traceback.format_exc()}")

    scan_out_file.write_text(json.dumps({"scanned_at_et": now_et.isoformat(), "results": results}, indent=2))
    log(f"Saved {scan_out_file}")

    current_hits = {r["symbol"]: r["side"] for r in results if r.get("side")}
    prior_hits = state.get("hits", {})
    new_hits = {sym: side for sym, side in current_hits.items() if prior_hits.get(sym) != side}

    should_send = False
    hits_to_report = {}

    if is_first_run:
        should_send = True
        hits_to_report = current_hits
    elif new_hits:
        should_send = True
        hits_to_report = new_hits
    elif errors:
        should_send = True
        hits_to_report = {}

    send_ok = True
    if should_send:
        hit_rows = [r for r in results if r.get("symbol") in hits_to_report]
        if hit_rows:
            body = "\n".join(format_hit_line(r) for r in hit_rows)
        else:
            body = "No momentum strat hits this run."
        message = f"🎯 *Momentum Strat Watchlist* — {now_et.strftime('%H:%M')} ET {today_et.isoformat()}\n{body}"
        if errors:
            message += "\n\n⚠️ " + "; ".join(errors)
        send_ok = send_telegram(message)

    if send_ok:
        state["first_run_sent"] = state.get("first_run_sent", False) or is_first_run
        state["hits"] = {**prior_hits, **current_hits}
    else:
        # Send failed - don't mark this run's new/changed hits as reported,
        # or a real signal could silently go unreported forever (this bit us
        # for real: an NVDA long hit failed to send with HTTP 400 and would
        # have been marked "already reported" without this guard, even
        # though the alert never reached Telegram). Unaffected hits (already
        # reported earlier, unchanged this run) still carry forward normally.
        log("Telegram send failed - not marking this run's hits as reported; will retry next run.")
        state["hits"] = {**prior_hits, **{k: v for k, v in current_hits.items() if k not in hits_to_report}}
    state_file.write_text(json.dumps(state, indent=2))

    n_hits = len(current_hits)
    print(f"Momentum Scanner: {n_hits} hit(s) this run. New: {list(new_hits.keys())}. Errors: {len(errors)}.")


if __name__ == "__main__":
    main()
