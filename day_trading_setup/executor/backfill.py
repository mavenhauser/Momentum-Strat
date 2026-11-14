"""Missed-alert backup: re-send DTS alerts whose TradingView webhook never arrived.

Nov 11 2026: TradingView fired AVGO's STOP at 14:20 ET, but its webhook failed on
TradingView's side ("something went wrong", 1 of 50 fires that day). The executor
has no stop of its own, so the call was held to the 15:56 sweep (-$1,208 instead of
about -$600). TradingView's own fire log still records every fire, so each minute
during the session (from the every-minute sweep) this compares the log with the
receiver's inbox and sends any DTS fire that never arrived to the receiver, as if
TradingView had delivered it now:

  - exits, 2R/4R sales, 2-hour checks and strong-close notes: always re-sent, however late
    (the position is still open, so selling now is what a person would do);
  - entries: re-sent only up to BACKFILL_ENTRY_MAX_MIN after the fire, later
    ones are reported but not bought (the price has moved on);
  - watch alerts (A+ OUTSIDE) and other information: ignored.

A fire counts as delivered when the inbox has the same message received from 2
minutes before to 10 minutes after the fire time (each inbox line matches one fire).
Re-sent fires are remembered in state/dts_backfill.json so nothing is sent twice.
The fire log is read from the TradingView desktop app (debugging port 9222, the
same requirement as the morning list refresh); if it can't be read, one message a
day says so.
"""
import json
import logging
import subprocess
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from day_trading_setup.executor import settings, signals

NY = ZoneInfo("America/New_York")
log = logging.getLogger("dts.backfill")
ACTIONABLE = ("ENTRY", "TARGET", "EXIT", "CHECK", "STRONG")
MATCH_BEFORE = timedelta(minutes=2)
MATCH_AFTER = timedelta(minutes=10)


def _utc(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)


def active(t) -> bool:
    """Weekdays from the open to a few minutes after the close (catches 15:55 exits)."""
    return t.weekday() < 5 and settings.BACKFILL_FROM <= (t.hour, t.minute) < settings.BACKFILL_UNTIL


def missed(fires, inbox, sent, now):
    """DTS fires from today (New York) with no matching inbox message, at least
    BACKFILL_MIN_AGE_S old and not already re-sent. fires: rows from tv_fires.mjs;
    inbox: [{"received": iso, "message": str}]; sent: {fire_id: ...}."""
    today = now.astimezone(NY).date()
    got = sorted((_utc(r["received"]), (r.get("message") or "").strip()) for r in inbox)
    used = set()
    out = []
    for f in sorted(fires, key=lambda f: f["fire_time"]):
        msg = (f.get("message") or "").strip()
        ft = _utc(f["fire_time"])
        if not msg.startswith("DTS:") or ft.astimezone(NY).date() != today:
            continue
        match = next((i for i, (rt, m) in enumerate(got)
                      if i not in used and m == msg and ft - MATCH_BEFORE <= rt <= ft + MATCH_AFTER), None)
        if match is not None:
            used.add(match)
            continue
        if str(f["fire_id"]) in sent or (now - ft).total_seconds() < settings.BACKFILL_MIN_AGE_S:
            continue
        out.append(f)
    return out


def decide(fire, now):
    """'send', 'late entry' or 'ignore' for a missed fire."""
    kind = signals.parse(fire["message"].strip()).kind
    if kind not in ACTIONABLE:
        return "ignore"
    if kind == "ENTRY" and now - _utc(fire["fire_time"]) > timedelta(minutes=settings.BACKFILL_ENTRY_MAX_MIN):
        return "late entry"
    return "send"


def _inbox_today(now):
    today = now.astimezone(NY).date()
    rows = []
    if settings.INBOX_FILE.exists():
        for line in settings.INBOX_FILE.read_text(errors="replace").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if _utc(r["received"]).astimezone(NY).date() == today:
                rows.append(r)
    return rows


def _load():
    try:
        return json.loads(settings.BACKFILL_STATE_FILE.read_text())
    except (OSError, ValueError):
        return {"sent": {}}


def _save(st):
    keep = sorted(st["sent"].items(), key=lambda kv: kv[1]["at"])[-500:]
    st["sent"] = dict(keep)
    settings.BACKFILL_STATE_FILE.write_text(json.dumps(st, indent=1))


def read_fires():
    r = subprocess.run(["node", str(settings.REPO / "day_trading_setup" / "executor" / "tv_fires.mjs")],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or f"exit {r.returncode}").strip()[:200])
    return json.loads(r.stdout)


def deliver(message, fire_time):
    secret = settings.SECRET_FILE.read_text().strip()
    body = json.dumps({"message": message, "redelivered_from": fire_time}).encode()
    req = urllib.request.Request(f"http://{settings.RECEIVER_HOST}:{settings.RECEIVER_PORT}/dts/{secret}",
                                 data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status != 200:
            raise RuntimeError(f"receiver answered {resp.status}")


def run(now, notify):
    """One pass. notify: the executor's Telegram function."""
    if not settings.BACKFILL or not active(now.astimezone(NY)):
        return
    st = _load()
    try:
        fires = read_fires()
    except Exception as e:
        log.warning("missed-alert backup: TradingView fire log unavailable: %s", e)
        day = now.astimezone(NY).date().isoformat()
        if st.get("unavailable_notified") != day:
            st["unavailable_notified"] = day
            _save(st)
            notify("<b>DTS · BACKUP OFF</b>\nThe missed-alert backup can't read TradingView's alert log "
                   f"(<code>{str(e)[:120]}</code>).\n\n<i>Alerts still arrive as usual; a failed webhook "
                   "just won't be re-sent until TradingView is open with its debugging port.</i>")
        return
    hm = lambda iso: f"{_utc(iso).astimezone(NY):%H:%M:%S}"
    for f in missed(fires, _inbox_today(now), st["sent"], now):
        what = decide(f, now)
        why = f.get("webhook_error") or ("no webhook result" if not f.get("webhook_ok") else "delivered per TradingView, never received")
        rec = {"at": now.isoformat(), "fire_time": f["fire_time"], "message": f["message"], "action": what, "why": why}
        if what == "send":
            try:
                deliver(f["message"].strip(), f["fire_time"])
            except Exception as e:
                log.warning("missed-alert backup: re-send of %s failed: %s", f["message"][:80], e)
                continue                                   # try again next minute
            log.info("missed-alert backup: re-sent %s (fired %s, %s)", f["message"], f["fire_time"], why)
            notify(f"<b>DTS · ALERT RECOVERED</b>\nTradingView fired this at <b>{hm(f['fire_time'])} ET</b> but the "
                   f"webhook never arrived ({why}); sent to the executor now ({now.astimezone(NY):%H:%M:%S} ET).\n\n"
                   f"<i>{f['message'].strip()[len('DTS: '):][:200]}</i>")
        elif what == "late entry":
            log.info("missed-alert backup: entry not taken, %s old: %s", now - _utc(f["fire_time"]), f["message"])
            notify(f"<b>DTS · MISSED ENTRY</b>\nTradingView fired this entry at <b>{hm(f['fire_time'])} ET</b> but the "
                   f"webhook never arrived ({why}). Found more than {settings.BACKFILL_ENTRY_MAX_MIN} min later, so it is "
                   f"not taken.\n\n<i>{f['message'].strip()[len('DTS: '):][:200]}</i>")
        st["sent"][str(f["fire_id"])] = rec
        _save(st)
