"""
Fully autonomous options-execution script for Momentum Strategy Variant H
(see Algo Trading/docs/momentum_strategy_backtest_record.html for the full
backtest/variant record this is built from) - long-only equity options
against an IBKR paper account.

Meant to be invoked once per hour, 9:45-14:00 ET on weekdays, by cron/
launchd (see scripts/run_momentum_paper_trader.sh) - NOT a long-lived
process like scripts/run_paper_trader.py. Each invocation: loads position
state from disk, manages any open positions (stop / catalyst / trim /
time-exit - the doc's underlying-price swing exit, translated into option
sell orders), scans flat tickers in the 10-ticker universe for a fresh
Variant H entry (SMA200 + triple-confirmed breakout, trailing 6-candle
lookback), places option buy orders as triggered, persists state, exits.

Order execution is entirely on IBKR (paper account). TastyTrade supplies
real-time option quotes/greeks only - it never places an order (same split
as scripts/run_paper_trader.py).

SAFETY: refuses to run against anything but a known paper-trading port
(7497 = paper TWS, 4002 = paper Gateway) - hard exit before connecting if
config.IBKR_PORT doesn't match either, independent of anything else.

Prereqs:
  - Trader Workstation running, logged into the PAPER account (confirm
    IBKR_PORT in .env before running)
  - .env populated with TT_CLIENT_SECRET / TT_REFRESH_TOKEN (Tastytrade
    OAuth) - run scripts/check_tastytrade_connection.py first to verify
  - Strongly recommended before a live run: scripts/check_momentum_option_order.py
    (validates the equity-option order-placement path end-to-end)

Run from the 'Algo Trading' directory:
  source venv/bin/activate
  python scripts/run_momentum_paper_trader.py [--dry-run]

--dry-run evaluates every entry/exit decision and logs what it WOULD do,
without placing any order or persisting state - use this for a few real
hourly cycles before letting it place actual (paper) orders.
"""
import argparse
import sys
import traceback
from datetime import date, datetime
from datetime import time as dtime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_insync import Stock  # noqa: E402

from src import config  # noqa: E402
from src.ibkr_client import IBKRClient  # noqa: E402
from src.momentum_option_picker import pick_contract  # noqa: E402
from src.momentum_signals import (  # noqa: E402
    ET,
    add_trading_days,
    build_daily_context,
    build_ema_context,
    check_variant_h_entry,
    fetch_daily_bars,
    fetch_intraday_bars,
    find_swing_leg,
    is_same_week,
    r_multiple,
    split_session_bars,
    trading_days_since,
)
from src.momentum_state import acquire_lock, load_state, release_lock, save_state  # noqa: E402
from src.tastytrade_client import TastytradeClient  # noqa: E402
from src.telegram_client import send_message  # noqa: E402

PAPER_PORTS = (7497, 4002)


def send(text):
    print(text)
    try:
        send_message(text)
    except Exception as e:
        print(f"Telegram send failed: {e}")


def assert_paper_account():
    if config.IBKR_PORT not in PAPER_PORTS:
        msg = (
            f"REFUSING TO RUN: IBKR_PORT={config.IBKR_PORT} is not a known paper-trading port "
            f"{PAPER_PORTS}. This script only ever trades a paper account - fix .env before retrying."
        )
        print(msg)
        try:
            send_message(f"*Momentum Trader*\n{msg}")
        except Exception:
            pass
        sys.exit(1)


def within_entry_window(now_et):
    start_h, start_m, end_h, end_m = config.MOMENTUM_ENTRY_WINDOW_ET
    return now_et.weekday() < 5 and dtime(start_h, start_m) <= now_et.time() <= dtime(end_h, end_m)


# --- entry side --------------------------------------------------------------

def scan_for_entry(ib, ticker):
    """Fetch daily+intraday bars for `ticker` and check Variant H's entry
    condition against today's regular-session bars so far. Returns
    (triggered: bool, current_price: float | None, target_price: float | None).
    target_price is the measured-move trim target, pivot-based (None if no
    confirmed prior swing leg is found within
    config.MOMENTUM_SWING_PIVOT_MAX_LOOKBACK_DAYS) - see find_swing_leg()."""
    contract = Stock(ticker, "SMART", "USD")
    ib.qualifyContracts(contract)

    daily_bars = fetch_daily_bars(ib, contract, duration="2 Y")
    context = build_daily_context(daily_bars, sma_window=config.MOMENTUM_SMA_WINDOW)
    ctx = context.get(date.today())
    if ctx is None:
        return False, None, None
    prev_day_high, _prev_day_low, prev_day_close, sma = ctx

    intraday_bars = fetch_intraday_bars(ib, contract, date.today(), date.today())
    premarket_bars, regular_bars = split_session_bars(intraday_bars)
    if not regular_bars:
        return False, None, None
    premarket_high = max((b.high for b in premarket_bars), default=None)

    triggered = check_variant_h_entry(
        prev_day_close, sma, prev_day_high, premarket_high, regular_bars,
        lookback=config.MOMENTUM_ENTRY_LOOKBACK_CANDLES,
    )
    current_price = regular_bars[-1].close
    target_price = None
    if triggered:
        swing_high, swing_low = find_swing_leg(
            daily_bars, date.today(),
            config.MOMENTUM_SWING_PIVOT_WIDTH, config.MOMENTUM_SWING_PIVOT_MAX_LOOKBACK_DAYS,
        )
        if swing_high is not None:
            target_price = current_price + (swing_high - swing_low)
    return triggered, current_price, target_price


def cluster_open_count(state, cluster):
    return sum(1 for t in cluster if t in state)


def try_open_position(client, tt, ib, ticker, state, nlv, dry_run=False):
    triggered, current_price, target_price = scan_for_entry(ib, ticker)
    if not triggered or current_price is None:
        return

    contract_info = pick_contract(tt, ticker)
    if contract_info is None:
        send(f"*Momentum Trader* - *{ticker}*: entry signal fired but no contract cleared the "
             f"delta/volume/OI filters - skipped, no order placed.")
        return

    ask = contract_info["ask"]
    premium_budget = nlv * config.MOMENTUM_SIZING_PCT_NLV
    contracts = int(premium_budget // (ask * 100))
    if contracts < 1:
        send(f"*Momentum Trader* - *{ticker}*: entry signal fired but the {config.MOMENTUM_SIZING_PCT_NLV:.0%} "
             f"NLV budget (${premium_budget:,.0f}) doesn't cover even 1 contract at ${ask:.2f} - skipped.")
        return

    if dry_run:
        print(f"[dry-run] would BUY {ticker} {contract_info['strike']:.0f}C {contract_info['expiry']} "
              f"x{contracts} @ ~{ask:.2f} (underlying {current_price:.2f}, target {target_price}, "
              f"delta {contract_info['delta']})")
        return

    limit_price = ask * (1 + config.MOMENTUM_LIMIT_SLIPPAGE_PCT)
    trade = client.buy_to_open(
        contract_info["expiry"], contract_info["strike"], "C", contracts, limit_price,
        trading_class=None, symbol=ticker,
    )
    trade = client.wait_for_fill(trade)
    if trade.orderStatus.status != "Filled":
        send(f"*Momentum Trader* - *{ticker}*: entry order did not fill in time - skipped, check TWS.")
        return

    state.setdefault(ticker, []).append({
        "strike": contract_info["strike"],
        "expiry": contract_info["expiry"].strftime("%Y%m%d"),
        "streamer_symbol": contract_info["streamer_symbol"],
        "contracts_remaining": contracts,
        "entry_premium": trade.orderStatus.avgFillPrice,
        "entry_underlying_price": current_price,
        "target_price": target_price,
        "entry_date": date.today().isoformat(),
        "trims_done": [],
        "breakeven_active": False,
        "itm_extension_deadline": None,
    })
    target_line = f"\nTarget: {target_price:.2f}" if target_price is not None else "\nTarget: n/a (no confirmed swing leg at entry)"
    send(
        f"*Momentum Trader* - Entered *{ticker}* *{contract_info['strike']:.0f}C* {contract_info['expiry']} "
        f"x{contracts} @ {trade.orderStatus.avgFillPrice:.2f} (underlying {current_price:.2f})\n"
        f"Reason: SMA200 uptrend + triple-confirmed breakout (close above prior-day high, premarket high, "
        f"and fresh high-of-day){target_line}"
    )


# --- exit side -----------------------------------------------------------------

def _close_full(client, tt, ticker, position, reason, dry_run=False):
    """Attempts to close the full remaining position. Returns True if fully
    closed (caller should drop this ticker from state), False if the order
    didn't fully fill (position stays open, contracts_remaining corrected
    to whatever actually filled - zero if nothing did).

    2026-08-31: fixed a real bug found live - this used to send "closed Nx"
    and let the caller drop the ticker regardless of what the order
    actually did. Caught it corrupting 3 real positions in one cycle: a
    GOOGL sell that filled only 1 of 13 (reported as fully closed, 12 left
    completely untracked), and NVDA/MU sells that were fully Cancelled (0
    filled) but still reported as closed and dropped."""
    qty_attempted = position["contracts_remaining"]
    if dry_run:
        print(f"[dry-run] would CLOSE {ticker} ({reason}) {qty_attempted}x")
        return True
    quote = tt.get_option_market_snapshot([position["streamer_symbol"]]).get(position["streamer_symbol"]) or {}
    bid = quote.get("bid")
    limit_price = bid * (1 - config.MOMENTUM_LIMIT_SLIPPAGE_PCT) if bid else position["entry_premium"]
    trade = client.sell_to_close(
        position["expiry"], position["strike"], "C", qty_attempted, limit_price,
        trading_class=None, symbol=ticker,
    )
    trade = client.wait_for_fill(trade)
    filled = trade.orderStatus.filled
    if filled <= 0:
        send(f"*Momentum Trader* - *{ticker}* *{position['strike']:.0f}C*: close attempt ({reason}) did not "
             f"fill (status={trade.orderStatus.status}) - still holding {qty_attempted}x, will retry.")
        return False
    position["contracts_remaining"] -= filled
    fully_closed = position["contracts_remaining"] <= 0
    if fully_closed:
        send(f"*Momentum Trader* - *{ticker}* *{position['strike']:.0f}C*: closed ({reason}) "
             f"{filled}x @ {trade.orderStatus.avgFillPrice:.2f}")
    else:
        send(f"*Momentum Trader* - *{ticker}* *{position['strike']:.0f}C*: PARTIAL close ({reason}) "
             f"{filled}x of {qty_attempted}x @ {trade.orderStatus.avgFillPrice:.2f} - "
             f"{position['contracts_remaining']}x still open, will retry the rest.")
    return fully_closed


def _trim_one_quarter(client, tt, ticker, position, level, reason, dry_run=False):
    """Returns True if this level is fully done (trims_done should record
    it), False if it didn't fill at all this cycle (nothing changed,
    retries next cycle) - same fill-verification fix as _close_full
    (2026-08-31)."""
    trim_qty_target = max(1, int(round(position["contracts_remaining"] * config.MOMENTUM_TRIM_FRACTION)))
    trim_qty_target = min(trim_qty_target, position["contracts_remaining"])
    if dry_run:
        print(f"[dry-run] would TRIM {ticker} {trim_qty_target}x ({reason})")
        return True
    quote = tt.get_option_market_snapshot([position["streamer_symbol"]]).get(position["streamer_symbol"]) or {}
    bid = quote.get("bid")
    if bid is None:
        return False
    limit_price = bid * (1 - config.MOMENTUM_LIMIT_SLIPPAGE_PCT)
    trade = client.sell_to_close(
        position["expiry"], position["strike"], "C", trim_qty_target, limit_price,
        trading_class=None, symbol=ticker,
    )
    trade = client.wait_for_fill(trade)
    filled = trade.orderStatus.filled
    if filled <= 0:
        send(f"*Momentum Trader* - *{ticker}* *{position['strike']:.0f}C*: trim attempt ({reason}) did not "
             f"fill (status={trade.orderStatus.status}) - will retry.")
        return False
    position["contracts_remaining"] -= filled
    position["breakeven_active"] = True
    fully_done = filled >= trim_qty_target
    if fully_done:
        position["trims_done"].append(level)
        send(f"*Momentum Trader* - *{ticker}* *{position['strike']:.0f}C*: trimmed {filled}x @ "
             f"{trade.orderStatus.avgFillPrice:.2f} ({reason}, {position['contracts_remaining']} remaining)")
    else:
        send(f"*Momentum Trader* - *{ticker}* *{position['strike']:.0f}C*: PARTIAL trim ({reason}) "
             f"{filled}x of {trim_qty_target}x target @ {trade.orderStatus.avgFillPrice:.2f} - "
             f"{position['contracts_remaining']}x remaining, will retry the rest of this level.")
    return fully_done


def manage_position(client, tt, ib, ticker, position, dry_run=False):
    """Evaluates the swing exit in priority order: stop -> theta-decay ->
    pending trim -> time-exit/ITM extension. Returns True if the position
    is still open afterward.

    2026-08-31 redesign: option premium is the PRIMARY basis for both stop
    and trim; the underlying is only ever a secondary filter/confirmation
    on top of a premium-triggered decision, never an independent trigger
    of its own - see src/config.py (MOMENTUM_PREMIUM_STOP_R,
    MOMENTUM_STOP_EMA_PERIOD, MOMENTUM_TRIM_LEVELS) for the full
    rationale."""
    contract = Stock(ticker, "SMART", "USD")
    ib.qualifyContracts(contract)
    intraday_bars = fetch_intraday_bars(ib, contract, date.today(), date.today())
    _premarket, regular_bars = split_session_bars(intraday_bars)
    if not regular_bars:
        return True  # no data yet this run - don't act blind

    current_underlying = regular_bars[-1].close
    today_low_so_far = min(b.low for b in regular_bars)

    daily_bars = fetch_daily_bars(ib, contract, duration="2 Y")
    daily_ctx = build_daily_context(daily_bars, sma_window=config.MOMENTUM_SMA_WINDOW).get(date.today())
    prev_day_low = daily_ctx[1] if daily_ctx is not None else None
    lod_reference = min(prev_day_low, today_low_so_far) if prev_day_low is not None else today_low_so_far
    ema = build_ema_context(daily_bars, config.MOMENTUM_STOP_EMA_PERIOD).get(date.today())

    snapshot = tt.get_option_market_snapshot([position["streamer_symbol"]]).get(position["streamer_symbol"]) or {}
    theta, delta, current_bid = snapshot.get("theta"), snapshot.get("delta"), snapshot.get("bid")

    # 1. Stop - the option's own premium collapsing MOMENTUM_PREMIUM_STOP_R
    # is the PRIMARY trigger; the underlying only ever gates whether a
    # triggered stop actually executes, never triggers on its own:
    #   underlying >= its EMA(period) -> HOLD (still at support, don't sell)
    #   underlying <= LoD reference (trailing: min of prev-day low and
    #     today's low-so-far, only ever tightens) -> CLOSE (confirmed
    #     breakdown)
    #   neither -> HOLD (premium alone isn't enough without confirmation)
    premium_stop_hit = (
        current_bid is not None
        and r_multiple(position["entry_premium"], current_bid, config.MOMENTUM_R_PCT) <= config.MOMENTUM_PREMIUM_STOP_R
    )
    if premium_stop_hit:
        at_or_above_support = ema is not None and current_underlying >= ema
        broke_down = current_underlying <= lod_reference
        if broke_down and not at_or_above_support:
            if _close_full(client, tt, ticker, position, "premium stop (LoD break)", dry_run):
                return False
            return True  # didn't fully fill - reassess fresh next cycle
        # else: at/above the EMA, or no LoD confirmation yet - hold

    expiry_date = datetime.strptime(position["expiry"], "%Y%m%d").date()
    is_itm = current_underlying > position["strike"]

    # 2. "Catalyst" exit - fires unconditionally, ahead of trim/time-exit,
    # if either holds (user-specified definition, 2026-08-02):
    #   (a) theta exceeds delta
    #   (b) still holding into the contract's own expiry week
    theta_exceeds_delta = theta is not None and delta is not None and abs(theta) > delta
    in_expiry_week = is_same_week(date.today(), expiry_date)
    if theta_exceeds_delta or in_expiry_week:
        reason = "theta > delta" if theta_exceeds_delta else "expiry week"
        if _close_full(client, tt, ticker, position, reason, dry_run):
            return False
        return True  # didn't fully fill - reassess fresh next cycle

    # 3. Trim ladder - pure option-premium R-multiples (2026-08-31),
    # ascending: 25% of whatever remains at each of MOMENTUM_TRIM_LEVELS.
    # The measured-move target_price is only a secondary confirmation noted
    # on the first level's alert now - it no longer independently triggers
    # a trim on its own (same option-primary principle as the stop above).
    if current_bid is not None:
        premium_r = r_multiple(position["entry_premium"], current_bid, config.MOMENTUM_R_PCT)
        for level in config.MOMENTUM_TRIM_LEVELS:
            if level in position["trims_done"]:
                continue
            if premium_r < level:
                break  # ascending levels - none higher can be hit either
            reasons = [f"+{level:.0f}R premium"]
            if level == config.MOMENTUM_TRIM_LEVELS[0]:
                target_price = position.get("target_price")
                if target_price is not None and current_underlying >= target_price:
                    reasons.append(f"target {target_price:.2f} also hit")
            filled_ok = _trim_one_quarter(client, tt, ticker, position, level, " & ".join(reasons), dry_run)
            if position["contracts_remaining"] <= 0:
                return False
            if not filled_ok:
                break  # this attempt didn't fill - don't stack more orders this cycle

    # 4. Time exit, with a bounded ITM extension. The expiry-week catalyst
    # check above already guarantees this never runs past expiry week, so
    # no separate calendar-day floor is needed here.
    entry_date = date.fromisoformat(position["entry_date"])
    days_held = trading_days_since(entry_date, date.today())

    if position["itm_extension_deadline"] is not None:
        deadline = date.fromisoformat(position["itm_extension_deadline"])
        if date.today() >= deadline:
            if _close_full(client, tt, ticker, position, "time exit (ITM extension expired)", dry_run):
                return False
            return True  # didn't fully fill - reassess fresh next cycle
    elif days_held >= config.MOMENTUM_TIME_EXIT_TRADING_DAYS:
        if is_itm:
            deadline = add_trading_days(date.today(), config.MOMENTUM_ITM_EXTENSION_TRADING_DAYS)
            if not dry_run:
                position["itm_extension_deadline"] = deadline.isoformat()
            send(f"*Momentum Trader* - *{ticker}* *{position['strike']:.0f}C*: time exit reached but ITM - "
                 f"extending to {deadline.isoformat()}.")
        else:
            if _close_full(client, tt, ticker, position, "time exit", dry_run):
                return False
            return True  # didn't fully fill - reassess fresh next cycle

    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Log every decision without placing orders.")
    args = parser.parse_args()

    assert_paper_account()

    now_et = datetime.now(ET)
    if not within_entry_window(now_et):
        print(f"Outside the {config.MOMENTUM_ENTRY_WINDOW_ET} ET / weekday window (now: {now_et}) - no-op exit.")
        return

    if args.dry_run:
        print("--- DRY RUN: no orders will be placed, state will not be saved ---")

    if not acquire_lock():
        print("Another instance is still running (lock held by a live PID) - skipping this run "
              "rather than risking an overlapping IBKR connection / duplicate order.")
        return

    try:
        try:
            _run(args)
        except Exception as e:
            print(f"Run failed: {e}")
            traceback.print_exc()
            send("*Momentum Trader* - No strat run.")
    finally:
        release_lock()


def _run(args):
    client = IBKRClient(client_id=config.MOMENTUM_IBKR_CLIENT_ID)
    tt = TastytradeClient()
    print(f"Connecting to IBKR at {client.host}:{client.port} "
          f"(clientId={client.client_id}, paper, confirmed by assert_paper_account)...")
    client.connect()
    print("Connecting to TastyTrade...")
    tt.connect()

    try:
        state = load_state()
        nlv = client.get_account_value("NetLiquidation")
        if nlv is None:
            print("Could not read account NLV this run - skipping entry scan (exits still evaluated).")

        # 1. Manage open positions first. A ticker can hold more than one lot
        # (see src/momentum_state.py) - each is evaluated independently, so
        # one can close while another stays open.
        for ticker in list(state.keys()):
            remaining_lots = []
            for position in state[ticker]:
                try:
                    still_open = manage_position(client, tt, client.ib, ticker, position, args.dry_run)
                except Exception as e:
                    print(f"Failed managing {ticker}, leaving position untouched this run: {e}")
                    traceback.print_exc()
                    still_open = True
                if still_open or args.dry_run:
                    remaining_lots.append(position)
            if not args.dry_run:
                if remaining_lots:
                    state[ticker] = remaining_lots
                else:
                    del state[ticker]

        # 2. Scan flat tickers for entries, respecting concurrency/cluster caps.
        if nlv is not None:
            for ticker in config.MOMENTUM_TICKERS:
                if ticker in state:
                    continue
                if len(state) >= config.MOMENTUM_MAX_CONCURRENT:
                    print(f"Max concurrent positions ({config.MOMENTUM_MAX_CONCURRENT}) reached - stopping scan.")
                    break
                if ticker in config.MOMENTUM_CLUSTER_SEMIS:
                    if cluster_open_count(state, config.MOMENTUM_CLUSTER_SEMIS) >= config.MOMENTUM_CLUSTER_MAX:
                        continue
                try:
                    try_open_position(client, tt, client.ib, ticker, state, nlv, args.dry_run)
                except Exception as e:
                    print(f"Failed scanning/opening {ticker}, skipping this run: {e}")
                    traceback.print_exc()

        if not args.dry_run:
            save_state(state)

    finally:
        client.disconnect()
        tt.disconnect()
        print("Disconnected.")


if __name__ == "__main__":
    main()
