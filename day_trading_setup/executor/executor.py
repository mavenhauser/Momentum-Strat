"""Act on one DTS Entry alert by trading weekly options on the IBKR paper account.

TradingView decides every entry, stop, target and exit; this only executes.
LONG -> buy a call, SHORT -> buy a put, this week's expiry, strike nearest the
TradingView entry price, about 1% of net liquidation in premium. The 2R alert
sells half, any EXIT alert (STOP / BE / TRAIL / EOD / TIME) sells the rest.
IBKR prices are used only to fill the option orders.

Stateless per invocation (like the Momentum trader): one alert in, state in
state/dts_positions.json, a file lock so alerts are handled one at a time in
arrival order.

  python -m day_trading_setup.executor.executor --message "DTS: LONG NVDA @ ..."
  python -m day_trading_setup.executor.executor --sweep    # 15:56 ET safety close
  python -m day_trading_setup.executor.executor --status

Dry run unless DTS_EXECUTE=1: contract choice, sizing and quotes are real, no
orders are sent, fills are simulated at mid and kept in a separate state file.
SAFETY: refuses to start unless IBKR_PORT is a paper port (7497 / 4002), and
never sells more contracts than IBKR reports actually held.
"""
import argparse
import fcntl
import hashlib
import html
import json
import re
import logging
import os
import socket
import sys
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from src import config
from day_trading_setup.executor import contracts, settings, signals

NY = ZoneInfo("America/New_York")
log = logging.getLogger("dts")


# ---------------------------------------------------------------- utilities

def now_ny():
    return datetime.now(NY)


def notify(text):
    """Telegram message in HTML: <b>bold</b> for the key facts, blank lines between
    paragraphs. The log gets the same text without tags, on one line."""
    plain = re.sub(r"<[^>]+>", "", text)
    log.info("notify: %s", html.unescape(re.sub(r"\n+", " | ", plain)))
    if not settings.NOTIFY:
        return
    try:
        from src import telegram_client
        telegram_client.send_message(text, parse_mode="HTML")
    except Exception as e:  # a Telegram outage must never block trading
        log.warning("telegram failed: %s", e)


def state_path():
    return settings.STATE_FILE if settings.EXECUTE else settings.DRY_STATE_FILE


def load_state():
    p = state_path()
    if p.exists():
        return json.loads(p.read_text())
    return {"positions": {}, "closed": [], "processed": {}}


def save_state(state):
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # keep the duplicate-check memory and history bounded
    state["processed"] = dict(sorted(state["processed"].items(), key=lambda kv: kv[1])[-500:])
    state["closed"] = state["closed"][-300:]
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    os.replace(tmp, p)


def mode_tag():
    return "PAPER" if settings.EXECUTE else "DRY RUN"


def ibkr_status(up, detail=""):
    """Record IBKR reachability and tell the user on a change: a warning when it
    first goes down, a note when it comes back. Repeated failures stay quiet."""
    f = settings.IBKR_DOWN_FILE
    if up:
        if f.exists():
            since = f.read_text().strip()
            f.unlink()
            notify(f"{title('IBKR RECONNECTED')}\nTWS is accepting connections again on port {config.IBKR_PORT}.\n\n"
                   f"<i>Down since {h(since)}</i>")
        return
    if f.exists():
        return
    settings.STATE_DIR.mkdir(parents=True, exist_ok=True)
    f.write_text(now_ny().strftime("%H:%M ET"))
    effect = ("<b>Paper orders cannot be placed</b> until it is back: entries are skipped, "
              "exits fail and are retried on the next alert." if settings.EXECUTE else
              "Trading continues on the broker's quotes; sizing is unaffected (notional account).")
    notify(f"{title('⚠️ IBKR NOT CONNECTED')}\n<b>TWS isn't accepting connections on port {config.IBKR_PORT}.</b>\n"
           f"Check TWS is open and logged in to the paper account.\n\n{effect}\n\n<i>{h(detail)}</i>")


def check_tws_port(t):
    """Cheap reachability probe (a plain TCP connect, no API session), run by the
    sweep job every 5 minutes during market hours."""
    if t.weekday() >= 5 or not (settings.HEALTH_START <= (t.hour, t.minute) < settings.HEALTH_END) or t.minute % 5:
        return
    try:
        with socket.create_connection((config.IBKR_HOST, config.IBKR_PORT), timeout=3):
            ibkr_status(True)
    except OSError as e:
        ibkr_status(False, f"port check: {e}")


def assert_paper_port():
    if config.IBKR_PORT not in settings.PAPER_PORTS:
        sys.exit(f"REFUSING TO RUN: IBKR_PORT={config.IBKR_PORT} is not a paper port {settings.PAPER_PORTS}.")


# ---------------------------------------------------------------- broker

class Broker:
    """IBKR for account value, real positions, live quotes and orders; TastyTrade
    for the option chain and liquidity snapshot (same split as the Momentum trader)."""

    def __init__(self):
        self.ib = None
        self.tt = None
        self.last_quote = (None, None, None)   # (bid, ask, source) behind the last fill, for the journal

    def connect(self):
        assert_paper_port()
        from src.tastytrade_client import TastytradeClient
        self.tt = TastytradeClient()
        self.tt.connect()
        try:
            from src.ibkr_client import IBKRClient
            client = IBKRClient(client_id=settings.IBKR_CLIENT_ID)
            client.connect()
            self.ib = client
            ibkr_status(True)
        except Exception as e:
            ibkr_status(False, f"{type(e).__name__}: {e}")
            if settings.EXECUTE:
                raise RuntimeError(f"IBKR not reachable on port {config.IBKR_PORT}: {e}")
            log.warning("IBKR not reachable (%s) - dry run continues on TastyTrade quotes only", e)

    def close(self):
        for c in (self.ib, self.tt):
            try:
                if c:
                    c.disconnect()
            except Exception:
                pass

    def nlv(self):
        """Account size trades are sized off: the notional strategy account
        (settings.SIZING_ACCOUNT), so sizing never depends on IBKR being reachable."""
        return settings.SIZING_ACCOUNT, "notional"

    def chain(self, ticker):
        """Full option chain from TastyTrade. Large chains are slow (AMD: ~15 s for
        7,000+ contracts, which tripped the shared client's 15 s limit live on
        2026-10-14), so this allows 60 s and retries once."""
        from tastytrade.instruments import get_option_chain
        cache = self.tt._chain_cache
        if ticker not in cache:
            last = None
            for _ in range(2):
                try:
                    self.tt._run(self.tt.session.refresh())
                    cache[ticker] = self.tt._run(get_option_chain(self.tt.session, ticker), timeout=60)
                    break
                except Exception as e:
                    last = e
                    log.warning("option chain for %s failed (%s), retrying", ticker, type(e).__name__)
            else:
                raise RuntimeError(f"option chain for {ticker} unavailable: {type(last).__name__} {last}")
        return cache[ticker]

    def snapshot(self, streamer_symbols):
        return self.tt.get_option_market_snapshot(streamer_symbols)

    def quote(self, pos):
        """(bid, ask, source) for the contract: TastyTrade's quote first (for this
        account the 15-minute-delayed demo feed - found 2026-10-16; see reprice.py),
        IBKR's as fallback. This account's IBKR API option data is 15-minute
        delayed (error 10091, no real-time options subscription for the API -
        seen live 2026-10-14: IBKR 6.75/6.90 vs TastyTrade 6.85/7.00 on the same
        TSLA put), so IBKR is only used when TastyTrade has no two-sided quote.
        None if neither has one."""
        sym = pos.get("streamer")
        if not sym:                           # positions opened before the symbol was stored
            from tastytrade.instruments import OptionType
            exp = date.fromisoformat(pos["expiry"])
            sym = next((o.streamer_symbol for o in self.chain(pos["ticker"]).get(exp, [])
                        if float(o.strike_price) == pos["strike"] and o.option_type == OptionType(pos["right"])), None)
        if sym:
            q = self.snapshot([sym]).get(sym) or {}
            if q.get("bid") and q.get("ask"):
                return q["bid"], q["ask"], "TastyTrade"
        if self.ib:
            bid, ask = self.ib.get_option_bid_ask(pos["expiry_ib"], pos["strike"], pos["right"],
                                                  trading_class=None, symbol=pos["ticker"])
            if bid is not None:
                return bid, ask, "IBKR (may be 15-min delayed)"
        return None

    def real_position(self, pos):
        return self.ib.get_option_position(pos["expiry_ib"], pos["strike"], pos["right"], pos["ticker"])

    def _order(self, action, pos, qty, price):
        place = self.ib.buy_to_open if action == "BUY" else self.ib.sell_to_close
        trade = place(pos["expiry_ib"], pos["strike"], pos["right"], qty, price,
                      trading_class=None, symbol=pos["ticker"])
        trade = self.ib.wait_for_fill(trade, timeout_seconds=settings.ORDER_FILL_TIMEOUT_SECONDS)
        return int(trade.orderStatus.filled or 0), float(trade.orderStatus.avgFillPrice or 0)

    def fill(self, action, pos, qty):
        """Limit at mid; anything unfilled after the timeout is cancelled and retried
        once at the aggressive side (ask to buy, bid to sell). Returns
        (filled, avg_price, note)."""
        q = self.quote(pos)
        if not q:
            return 0, None, "no two-sided quote"
        bid, ask, src = q
        self.last_quote = (bid, ask, src)
        mid = round((bid + ask) / 2, 2)
        if not settings.EXECUTE:
            return qty, mid, f"filled at mid {mid:.2f} (Broker {bid:.2f}/{ask:.2f})"   # messages name no data source (user, 2026-10-18); the real one is kept in the event's quote_source
        filled, avg = self._order(action, pos, qty, mid)
        total, cost = filled, filled * avg
        if filled < qty:
            q2 = self.quote(pos)
            if q2:
                price2 = round(q2[1] if action == "BUY" else q2[0], 2)
                f2, a2 = self._order(action, pos, qty - filled, price2)
                total, cost = total + f2, cost + f2 * a2
        avg_all = round(cost / total, 4) if total else None
        return total, avg_all, f"{total}/{qty} filled (Broker quote {bid:.2f}/{ask:.2f})"


# ---------------------------------------------------------------- handlers

def describe(pos):
    kind = "CALL" if pos["right"] == "C" else "PUT"
    return f"{pos['ticker']} {pos['strike']:g} {kind}"


def h(x):
    return html.escape(str(x))


def title(action):
    # Messages read as live trading (user, 2026-10-18): no "DRY RUN" tag. Paper mode
    # keeps its PAPER tag because orders really are sent then.
    tag = " PAPER" if settings.EXECUTE else ""
    return f"<b>DTS{tag} · {action}</b>"


def expiry_label(pos):
    return date.fromisoformat(pos["expiry"]).strftime("%a %d %b")


def money(x):
    return f"{'+' if x >= 0 else '−'}${abs(x):,.0f}"


def signal_line(sig):
    return f"<b>{h(sig.side)} {h(sig.ticker)}</b>"


def choose_contract(broker, sig, t):
    today = t.date()
    from tastytrade.instruments import OptionType
    right = "C" if sig.side == "LONG" else "P"
    chain = broker.chain(sig.ticker)
    expiry = contracts.pick_expiry(list(chain.keys()), today, (t.hour, t.minute))
    if expiry is None:
        return None, "no option expiries listed"
    opts = {float(o.strike_price): o for o in chain[expiry] if o.option_type == OptionType(right)}
    look = set(contracts.strike_ladder(opts.keys(), sig.price, right, settings.STRIKES_TO_SCAN))
    snap = broker.snapshot([opts[k].streamer_symbol for k in look])
    quotes = {k: snap.get(opts[k].streamer_symbol) or {} for k in look}
    nlv, _ = broker.nlv()
    move = abs(sig.target - sig.price) if sig.target else None
    base = settings.FRIDAY_SWING_SIZE if contracts.swing_entry(t) else contracts.size_fraction(sig.grade)
    frac = base * (settings.CHOP_SIZE_FACTOR if sig.chop is not None else 1.0)
    floor = settings.MIN_DELTA_0DTE if expiry == today else settings.MIN_DELTA
    k, qty, rule, delta = contracts.choose_strike(quotes, sig.price, right, nlv * frac, move, floor)
    if k is None:
        return None, f"{rule} ({right}, {expiry})"
    if contracts.swing_entry(t) and (quotes[k].get("ask") or 0) * 100 > nlv * frac:
        return None, f"Friday swing: 1 contract costs ${quotes[k]['ask'] * 100:,.0f}, over the {frac:.2%} budget (${nlv * frac:,.0f})"
    return {"ticker": sig.ticker, "right": right, "expiry": expiry.isoformat(),
            "expiry_ib": expiry.strftime("%Y%m%d"), "strike": k, "streamer": opts[k].streamer_symbol,
            "qty_plan": qty, "delta": delta, "sizing_rule": rule,
            "grade": sig.grade, "size_fraction": frac, "chop": sig.chop}, None


def skip(state, sig, t, reason):
    state.setdefault("skipped", []).append({"t": t.isoformat(), "side": sig.side, "ticker": sig.ticker, "reason": reason})
    state["skipped"] = state["skipped"][-500:]


def on_entry(broker, state, sig, t):
    hm = (t.hour, t.minute)
    if t.weekday() >= 5 or not (settings.ENTRY_START <= hm < settings.ENTRY_END):
        return notify(f"{title('IGNORED')}\n{signal_line(sig)}\nOutside the regular session")
    if sig.ticker in state["positions"]:
        return notify(f"{title('IGNORED')}\n{signal_line(sig)}\nAlready holding <b>{h(describe(state['positions'][sig.ticker]))}</b>")
    if t.weekday() == 4 and hm >= settings.FRIDAY_ENTRY_END:
        why = f"no new entries on Friday from {settings.FRIDAY_ENTRY_END[0]}:{settings.FRIDAY_ENTRY_END[1]:02d}"
        skip(state, sig, t, why)
        return notify(f"{title('SKIPPED')}\n{signal_line(sig)}\n\n<b>{h(why)}</b>")
    if contracts.swing_entry(t):
        swings = sum(contracts.weekend_swing(p) for p in state["positions"].values())
        why = None
        if sig.grade not in settings.FRIDAY_SWING_GRADES:
            why = f"Friday swing: only {' / '.join(sorted(settings.FRIDAY_SWING_GRADES))} setups (this one: {sig.grade or 'no grade'})"
        elif swings >= settings.FRIDAY_SWING_MAX:
            why = f"Friday swing: {swings} already open (max {settings.FRIDAY_SWING_MAX})"
        if why:
            skip(state, sig, t, why)
            return notify(f"{title('SKIPPED')}\n{signal_line(sig)}\n\n<b>{h(why)}</b>")
    if sig.stop is not None and contracts.stop_pct(sig.price, sig.stop) < settings.MIN_STOP_PCT:
        why = f"stop too tight: {contracts.stop_pct(sig.price, sig.stop):.2f}% of price (min {settings.MIN_STOP_PCT:g}%)"
        skip(state, sig, t, why)
        return notify(f"{title('SKIPPED')}\n{signal_line(sig)}\nStop {sig.stop} · Target {sig.target}\n\n<b>{h(why)}</b>")
    streak = contracts.loss_streak(state["closed"], t.date())
    if streak >= settings.LOSS_BRAKE:
        if sig.grade in settings.BRAKE_OVERRIDE_GRADES:
            log.info("loss brake (%d in a row) overridden by a %s setup: %s", streak, sig.grade, sig.raw)
        else:
            why = f"loss brake: {streak} stop-outs in a row today (only {', '.join(sorted(settings.BRAKE_OVERRIDE_GRADES))} setups taken)"
            skip(state, sig, t, why)
            return notify(f"{title('SKIPPED')}\n{signal_line(sig)}\nGrade {h(sig.grade or 'none')}\n\n<b>{h(why)}</b>")
    if len(state["positions"]) >= settings.MAX_OPEN_POSITIONS:
        skip(state, sig, t, f"position limit ({settings.MAX_OPEN_POSITIONS} open)")
        return notify(f"{title('SKIPPED')}\n{signal_line(sig)}\nAlready at the limit of {settings.MAX_OPEN_POSITIONS} open positions")
    pos, why = choose_contract(broker, sig, t)
    if not pos:
        skip(state, sig, t, why)
        return notify(f"{title('SKIPPED')}\n{signal_line(sig)}\n{h(why)}")
    q = broker.quote(pos)
    if not q:
        skip(state, sig, t, "no quote")
        return notify(f"{title('SKIPPED')}\n{signal_line(sig)}\nNo quote for <b>{h(describe(pos))}</b>")
    nlv, nlv_src = broker.nlv()
    qty = pos.pop("qty_plan")
    filled, avg, note = broker.fill("BUY", pos, qty)
    if not filled:
        skip(state, sig, t, f"order not filled: {note}")
        return notify(f"{title('NOT OPENED')}\n{signal_line(sig)}\n<b>{qty} × {h(describe(pos))}</b> · expiry {expiry_label(pos)}\n\n<i>{h(note)}</i>")
    pos.update(side=sig.side, qty=filled, open_qty=filled, avg_cost=avg, tv_entry=sig.price, tv_stop=sig.stop,
               tv_target=sig.target, level=sig.level, opened_at=t.isoformat(), trimmed=False,
               events=[{"t": t.isoformat(), "alert": sig.raw, "action": f"BUY {filled} @ {avg}",
                        "qty": filled, "price": avg, "bid": broker.last_quote[0], "ask": broker.last_quote[1],
                        "quote_source": broker.last_quote[2]}],
               nlv=nlv, nlv_source=nlv_src)
    state["positions"][sig.ticker] = pos
    notify(f"{title('BOUGHT')}\n"
           f"<b>{filled} × {h(describe(pos))}</b>\n"
           f"Expiry {expiry_label(pos)} · price <b>{avg:.2f}</b>\n\n"
           f"TradingView {signal_line(sig)} @ {sig.price}\n"
           f"Stop {sig.stop} · Target {sig.target} · {h(sig.level)} breakout\n\n"
           f"Grade <b>{h(sig.grade or 'none (pre-v6 alert)')}</b> → size <b>{pos.get('size_fraction', 0):.1%}</b>"
           + (f" (half: choppy market, QQQ crossed VWAP {sig.chop}×)" if sig.chop is not None else "") + "\n"
           f"Premium <b>${filled * avg * 100:,.0f}</b> = {filled * avg * 100 / nlv:.1%} of ${nlv:,.0f} ({h(nlv_src)})\n"
           f"Strike chosen: delta {abs(pos['delta']) if pos.get('delta') is not None else float('nan'):.2f} · {h(pos.get('sizing_rule', ''))}\n"
           + ("Friday swing: smaller size, held over the weekend even before 2R\n" if contracts.weekend_swing(pos) else "")
           + f"<i>{h(note)}</i>")


def _sell(broker, state, pos, qty, t, why, alert_raw):
    if settings.EXECUTE:
        real = int(broker.real_position(pos))
        if real < qty:
            log.warning("state says %s open, IBKR holds %s - selling only what is held", qty, real)
            qty = max(real, 0)
    if qty <= 0:
        return 0, None, "nothing held on IBKR"
    filled, avg, note = broker.fill("SELL", pos, qty)
    pos["open_qty"] -= filled
    pos["proceeds"] = pos.get("proceeds", 0.0) + filled * (avg or 0.0)
    pos["events"].append({"t": t.isoformat(), "alert": alert_raw, "action": f"SELL {filled} @ {avg} ({why})",
                          "qty": filled, "price": avg, "bid": broker.last_quote[0], "ask": broker.last_quote[1],
                          "quote_source": broker.last_quote[2], "why": why})
    return filled, avg, note


def _close_record(state, pos, t, why):
    pnl = (pos.get("proceeds", 0.0) - pos["qty"] * pos["avg_cost"]) * 100
    state["closed"].append({**pos, "closed_at": t.isoformat(), "close_reason": why, "pnl_usd": round(pnl, 2)})
    del state["positions"][pos["ticker"]]
    return pnl


def on_target(broker, state, sig, t):
    pos = state["positions"].get(sig.ticker)
    if not pos or pos["trimmed"]:
        return log.info("2R alert for %s with no untrimmed position - ignored", sig.ticker)
    n = contracts.trim_quantity(pos["open_qty"])
    if n == 0:
        pos["trimmed"] = True
        pos["events"].append({"t": t.isoformat(), "alert": sig.raw, "action": "HOLD at 2R (single contract)"})
        return notify(f"{title('2R TARGET HIT')}\n<b>Holding 1 × {h(describe(pos))}</b>\n"
                      f"A single contract isn't split: it rides to the trail, break-even or stop\n\n"
                      f"TradingView: {h(sig.ticker)} reached {sig.price}")
    filled, avg, note = _sell(broker, state, pos, n, t, "2R trim", sig.raw)
    pos["trimmed"] = True
    if filled:
        notify(f"{title('2R TARGET HIT')}\n"
               f"<b>SOLD {filled} × {h(describe(pos))}</b> @ <b>{avg:.2f}</b>\n"
               + (f"{pos['open_qty']} left, riding to the trail or break-even\n\n" if pos["open_qty"] > 0
                  else "Nothing left: the whole position was sold\n\n") +
               f"TradingView: {h(sig.ticker)} reached {sig.price}\n"
               f"<i>{h(note)}</i>")
    else:
        notify(f"{title('2R TRIM NOT FILLED')}\n<b>{h(describe(pos))}</b>\nTradingView: {h(sig.ticker)} reached {sig.price}\n\n<i>{h(note)}</i>")
    if pos["open_qty"] <= 0:
        pnl = _close_record(state, pos, t, "2R (fully sold)")
        notify(f"{title('CLOSED')}\n<b>{h(describe(pos))}</b>\nFully sold at 2R\n\n<i>P&amp;L follows once prices are confirmed (about 15 min)</i>")


def close_position(broker, state, pos, t, why, alert_raw):
    filled, avg, note = _sell(broker, state, pos, pos["open_qty"], t, why, alert_raw)
    if pos["open_qty"] > 0:
        return notify(f"{title('CLOSE INCOMPLETE')}\n<b>{h(describe(pos))}</b>\n<b>{pos['open_qty']} still open</b>\nReason: {h(why)}\n\n<i>{h(note)}</i>")
    pnl = _close_record(state, pos, t, why)
    price = f"{avg:.2f}" if avg else "-"
    notify(f"{title('CLOSED')}\n"
           f"<b>{h(describe(pos))}</b>\n"
           f"Sold {filled} @ <b>{price}</b>\n"
           f"Reason: {h(why)}\n\n"
           f"<i>P&amp;L follows once prices are confirmed (about 15 min)</i>")


def _results_due(state, t):
    from day_trading_setup.executor import reprice
    return [p for p in state["closed"] if not p.get("result_sent") and p.get("closed_at", "") >= settings.RESULTS_FROM
            and datetime.fromisoformat(p["closed_at"]) <= t - reprice.DELAY]


def prefetch_results(t):
    """Fetch 1-minute prices for trades due a RESULT message. Network, up to a
    minute, so it runs before the lock: alerts are never held up by it."""
    from day_trading_setup.executor import reprice
    try:
        if _results_due(load_state(), t):
            reprice.run("paper" if settings.EXECUTE else "dry")
    except Exception as e:
        log.warning("reprice failed, results retried next minute: %s: %s", type(e).__name__, e)


def confirm_results(state, t):
    """Send each closed trade's P&L once its fills can be priced from 1-minute
    option history (the live quotes are ~15 minutes delayed): the CLOSED message
    carries no P&L; this RESULT message follows about 16 minutes after the exit
    (user, 2026-10-19). Realistic P&L, as in the journal: true price, buys plus and
    sells minus half the spread."""
    from day_trading_setup.executor import journal, reprice
    due = _results_due(state, t)
    if not due:
        return False
    rp = reprice.reprice_path("paper" if settings.EXECUTE else "dry")
    repriced = json.loads(rp.read_text()) if rp.exists() else {}
    lines = journal._log_lines()
    for p in due:
        if not all(reprice.event_key(p, ev) in repriced for ev in p["events"]):
            continue                          # prices not fetched yet: next minute
        e = journal.trade_entry(p, datetime.fromisoformat(p["closed_at"]).astimezone(NY).date(), lines, repriced)
        legs = [e["buy"]] + e["sells"]
        px = lambda leg: leg.get("true") if leg.get("true") is not None else leg.get("price")
        fills = f"Bought {e['buy']['qty']} @ <b>{px(e['buy']):.2f}</b> · " + ", ".join(
            f"sold {s['qty']} @ <b>{px(s):.2f}</b> ({h(s['kind'])})" for s in e["sells"])
        note = "; ".join(e["notes"]) if e["notes"] else "confirmed at market prices (1-minute option prices)"
        notify(f"{title('RESULT')}\n<b>{h(describe(p))}</b>\n{fills}\n\n"
               f"Trade P&amp;L: <b>{money(e['pnl_real'])}</b>\n<i>{h(note)}</i>")
        p["result_sent"] = True
        p["pnl_confirmed"] = e["pnl_real"]
    return True


def on_exit(broker, state, sig, t):
    pos = state["positions"].get(sig.ticker)
    if not pos:
        return log.info("exit alert for %s with no open position - ignored", sig.ticker)
    close_position(broker, state, pos, t, f"TradingView {sig.reason} @ {sig.price}", sig.raw)


ALERT_ATTEMPTS = 3
ALERT_RETRY_PAUSE = 5          # seconds between attempts
PENDING_GIVE_UP = 60           # sweep retries (about an hour) before telling the user


def retry_pending(state, t):
    """Retry exits / 2R sales that failed: called every minute by the sweep job.
    Returns True if anything changed."""
    pending = state.get("pending") or []
    if not pending:
        return False
    keep = []
    for item in pending:
        sig = signals.parse(item["raw"])
        if sig.ticker not in state["positions"]:
            log.info("pending %s dropped: no open position any more", item["raw"])
            continue
        broker = Broker()
        try:
            broker.connect()
            {"TARGET": on_target, "EXIT": on_exit}[sig.kind](broker, state, sig, t)
            log.info("pending %s handled on retry %d", item["raw"], item["tries"] + 1)
        except Exception as e:
            item["tries"] += 1
            log.warning("pending %s still failing (%s: %s)", item["raw"], type(e).__name__, e)
            if item["tries"] == PENDING_GIVE_UP:
                notify(f"{title('⚠️ EXIT STILL FAILING')}\n<code>{h(item['raw'][:120])}</code>\n\n"
                       f"Tried {item['tries']} times since {h(item['t'][11:16])} ET - check the position by hand.")
            keep.append(item)
        finally:
            broker.close()
    state["pending"] = keep
    return True


def sweep(broker, state, t):
    """Every minute in the session: the 0DTE time stop (no 2R within
    ZERO_DTE_TIME_STOP_MIN minutes). At 15:56 ET: anything expiring today; anything past
    the max hold if TradingView's TIME alert never arrived; and anything that has not
    reached 2R (only runners past 2R are held overnight)."""
    if t.weekday() >= 5 or not ((9, 30) <= (t.hour, t.minute) < (16, 0)):
        return
    for pos in list(state["positions"].values()):
        if contracts.time_stop_due(pos, t):
            close_position(broker, state, pos, t,
                           f"0DTE time stop: no 2R within {settings.ZERO_DTE_TIME_STOP_MIN} min", "time stop")
    if (t.hour, t.minute) < settings.SWEEP_AT:
        return
    for pos in list(state["positions"].values()):
        exp = date.fromisoformat(pos["expiry"])
        held = contracts.sessions_held(datetime.fromisoformat(pos["opened_at"]).date(), t.date())
        if exp <= t.date():
            close_position(broker, state, pos, t, "expiry day, closed by the 15:56 sweep", "sweep")
        elif held >= settings.MAX_HOLD_SESSIONS:
            close_position(broker, state, pos, t, f"held {held} sessions, closed by the 15:56 sweep", "sweep")
        elif contracts.close_before_overnight(pos):
            close_position(broker, state, pos, t, "no overnight hold before 2R: closed by the 15:56 sweep", "sweep")


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--message")
    ap.add_argument("--received", help="ISO time the alert was received (default: now)")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args(argv)

    settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(settings.LOG_DIR / "dts_executor.log"), logging.StreamHandler()])
    for noisy in ("tastytrade", "websockets", "httpx", "httpcore", "ib_insync", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    settings.STATE_DIR.mkdir(parents=True, exist_ok=True)

    if a.sweep:
        prefetch_results(datetime.fromisoformat(a.received).astimezone(NY) if a.received else now_ny())
    with open(settings.LOCK_FILE, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)          # one alert at a time, in arrival order
        state = load_state()
        if a.status:
            print(json.dumps({"mode": mode_tag(), "open": state["positions"], "closed": state["closed"][-10:]}, indent=2))
            return 0
        t = datetime.fromisoformat(a.received).astimezone(NY) if a.received else now_ny()

        if a.sweep:
            check_tws_port(t)
            if retry_pending(state, t):
                save_state(state)
            if confirm_results(state, t):
                save_state(state)
            in_session = t.weekday() < 5 and (9, 30) <= (t.hour, t.minute) < (16, 0)
            due = [p for p in state["positions"].values()
                   if contracts.time_stop_due(p, t) or (t.hour, t.minute) >= settings.SWEEP_AT]
            if not due or not in_session:      # connect to the brokers only when something is due
                return 0
            broker = Broker()
            try:
                broker.connect()
                sweep(broker, state, t)
            finally:
                broker.close()
                save_state(state)
            return 0

        sig = signals.parse(a.message or "")
        if sig.kind in ("IGNORED", "APLUS"):
            log.info("%s alert, no action: %s", sig.kind, sig.raw)
            return 0
        key = hashlib.sha1(f"{sig.raw}|{t.strftime('%Y-%m-%d %H')}:{t.minute // 5}".encode()).hexdigest()
        if key in state["processed"]:
            log.info("duplicate alert ignored: %s", sig.raw)
            return 0
        state["processed"][key] = t.isoformat()
        if sig.kind in ("TARGET", "EXIT") and sig.ticker not in state["positions"]:
            log.info("%s alert for %s with no open position - ignored", sig.kind, sig.ticker)
            save_state(state)
            return 0

        # Retry a failed alert (2026-10-22: a CRWD trail exit was lost to one ReadTimeout and
        # the runner stayed open a day). Each attempt reconnects. An exit / 2R sale that still
        # fails is queued, and the every-minute sweep keeps retrying it.
        err = None
        for attempt in range(ALERT_ATTEMPTS):
            broker = Broker()
            try:
                broker.connect()
                {"ENTRY": on_entry, "TARGET": on_target, "EXIT": on_exit}[sig.kind](broker, state, sig, t)
                err = None
                break
            except Exception as e:
                err = e
                log.exception("failed handling %s (attempt %d of %d)", sig.raw, attempt + 1, ALERT_ATTEMPTS)
            finally:
                broker.close()
                save_state(state)
            time.sleep(ALERT_RETRY_PAUSE)
        if err is not None:
            queued = sig.kind in ("TARGET", "EXIT") and sig.ticker in state["positions"]
            if queued:
                state.setdefault("pending", []).append({"raw": sig.raw, "t": t.isoformat(), "tries": ALERT_ATTEMPTS})
                save_state(state)
            notify(f"{title('ERROR')}\nAlert: <code>{h(sig.raw[:120])}</code>\n\n<b>{type(err).__name__}</b> {h(err)}"
                   + ("\n\n<i>Failed after %d tries; retrying every minute until it goes through.</i>" % ALERT_ATTEMPTS if queued
                      else "\n\n<i>Failed after %d tries.</i>" % ALERT_ATTEMPTS))
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
