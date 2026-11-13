"""Day Trading Setup executor settings.

Broker credentials, ports and Telegram come from the repo's .env through
src.config (shared with the Momentum Strategy). Everything DTS-specific lives
here so the two systems never step on each other's state or IBKR client id.
"""
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STATE_DIR = REPO / "state"
STATE_FILE = STATE_DIR / "dts_positions.json"          # paper-order mode
DRY_STATE_FILE = STATE_DIR / "dts_positions_dry.json"  # dry-run mode (simulated fills)
LOCK_FILE = STATE_DIR / "dts_executor.lock"
INBOX_FILE = STATE_DIR / "dts_alert_inbox.jsonl"       # every alert received, as received
LOG_DIR = REPO / "logs"

# Orders are only ever placed when this is "1" (set by the user, not by default).
# Anything else = dry run: full contract selection, sizing and quotes, no orders.
EXECUTE = os.getenv("DTS_EXECUTE", "0") == "1"
NOTIFY = os.getenv("DTS_NOTIFY", "1") == "1"      # Telegram messages; set 0 for silent tests

# Receiver (TradingView webhook -> this Mac). The secret path segment is kept
# outside the repo in ~/.dts-executor/secret and is created on first start.
RECEIVER_HOST = "127.0.0.1"
RECEIVER_PORT = 8090
SECRET_FILE = Path.home() / ".dts-executor" / "secret"

# Missed-alert backup (2026-11-11, user decision; see backfill.py): each minute in
# the session the sweep reads TradingView's alert fire log and re-sends any DTS fire
# the receiver never got (Nov 11: AVGO's 14:20 STOP webhook failed on TradingView's
# side and the call was held to the 15:56 sweep). Entries are re-sent only while
# fresh; exits and 2R sales always.
BACKFILL = os.getenv("DTS_BACKFILL", "1") == "1"
BACKFILL_FROM = (9, 30)
BACKFILL_UNTIL = (16, 10)          # a few minutes past the close, for the 15:55 exits
BACKFILL_MIN_AGE_S = 60            # leave a normal delivery time to land first
BACKFILL_ENTRY_MAX_MIN = 5         # older missed entries are reported, not bought
BACKFILL_STATE_FILE = STATE_DIR / "dts_backfill.json"

IBKR_CLIENT_ID = 47          # SPX scalp = 1, Momentum Strategy = 45
PAPER_PORTS = (7497, 4002)   # same hard guard as the Momentum trader

# Position sizing: option premium paid per trade, as a fraction of a NOTIONAL
# strategy account (not the IBKR paper account's value, which also holds other
# strategies). 1% of $100k = about $1,000 of premium per trade. Matches the
# starting balance of the DTS Trading Journal (2026-10-15).
SIZING_ACCOUNT = 100_000
# Size by confluence (2026-10-16, was a flat 1%): the entry alert carries DTS
# Entry's setup grade (points for clean trend, VWAP side, beyond the opening
# range, beyond the prior-week level; OUTSIDE = beyond both PDH/PMH or PDL/PML).
# Only the strongest confluence gets the full 5%. With MAX_OPEN_POSITIONS = 10,
# at most 25-50% of the account is in premium at once (2.5-5% each).
SIZE_BY_GRADE = {
    "A+ OUTSIDE": 0.05,    # all 4 confirmations and outside the day + premarket range
    "A OUTSIDE": 0.035,    # 3 of 4, outside
}
SIZE_DEFAULT = 0.025       # everything else, and alerts without a grade
# Choppy market (2026-10-22): when DTS Entry tags an entry "choppy (QQQ crosses N)" -
# QQQ has crossed its VWAP more than 8 times that day - the size is multiplied by this.
CHOP_SIZE_FACTOR = 0.5
MIN_CONTRACTS = 1            # if 1% buys less than one contract, buy one anyway

# Contract selection. Expiry: this week's last listed expiry (normally Friday),
# even on Friday itself (0DTE), per the agreed rule. Strike: nearest to the
# TradingView entry price; if that strike fails the liquidity check, the next
# nearest is tried.
# Never in the money (2026-10-16): the ladder starts at the at-the-money strike.
# A strike in the money by no more than this % of the price still counts as at
# the money (e.g. the 217.5 call with the stock at 217.70).
ATM_TOLERANCE_PCT = 0.10
# Strike ladder (2026-10-15): from the strike nearest the money, step out of the
# money until a strike buys TARGET_CONTRACTS within the budget; failing that,
# MIN_CONTRACTS_TO_TRIM (so half can be sold at 2R and half trailed); never
# below MIN_DELTA. If none qualifies, the entry is skipped: 1 contract can't be split at
# 2R (user decision, 2026-11-03; 3 single-contract trades Oct 23-Nov 2 at market prices: -$1,317, none reached 2R).
TARGET_CONTRACTS = 4
MIN_CONTRACTS_TO_TRIM = 2
MIN_DELTA = 0.15             # weeklies held 1-2 days: further OTM is fine, rides the volatility spike (was 0.25 until 2026-10-16)
# On expiry day (normally Friday) the same-day option is bought only inside this New
# York window: from the open until noon. Afternoon entries (little left to run for)
# buy next week's expiry instead (user decision, 2026-10-18).
ZERO_DTE_WINDOW = ((9, 30), (12, 0))
# 0DTE time stop (2026-10-18): a same-day option that has not reached the 2R target
# within this many minutes of entry is closed. Winners took a median 45 min to 2R;
# a 0DTE that goes nowhere bleeds most of its value by the close (TSLA, Oct 16).
ZERO_DTE_TIME_STOP_MIN = 90
# Journal days from this date use fills re-priced from 1-minute history (reprice.py);
# earlier days keep the executor's recorded figures (user decision, 2026-10-16).
REPRICE_FROM = "2026-10-16"
# Exits at the close (user decision, 2026-11-09, from that day on): any sale from
# 15:50 ET (the 15:55 EOD exit, a trail that fires on the last bar, the 15:56 sweep)
# is booked at the mid if the option is liquid, else at the bid: liquid = it traded
# in at least CLOSE_LIQUID_MINUTES of the 30 minutes up to the sale. Every other
# fill stays at the last traded price.
CLOSE_RULE_FROM = "2026-11-09"
CLOSE_EXIT_FROM = (15, 50)
CLOSE_LIQUID_MINUTES = 15
# Time limit TEST (user decision, 2026-11-11; report only, nothing is sold): two hours
# after entry, a trade that has not reached 2R (no sale yet) and whose option is below
# the entry price would be sold. reprice.py prices that moment from 1-minute candles and
# the journal shows the day's P&L with the rule beside the real one. Replay Oct 14-Nov 11
# (197 trades): +$8.4k vs +$58.6k, positive in both halves (Oct +$5.7k, Nov +$2.7k);
# selling every not-yet-2R trade at 2 h was +$6.9k, at 30-60 min -$11k to -$28k.
# To be reviewed the weekend of Nov 14 on Nov 12-13; if confirmed, live from Nov 16.
TIME_LIMIT_MIN = 120
TIME_LIMIT_TEST_FROM = "2026-11-12"
# Premium ladder test (user, 2026-11-12, report only, nothing is sold): instead of waiting
# for the stock's 2R, sell the option in steps as its premium rises, e.g. 1/4 of the
# position at +25% over the entry price, another 1/4 at +50%, ... ; the stock exits
# (stop, 2R, trail, close) sell the same share of whatever is left. Each step is a limit
# at that price, filled in the first minute the option trades at or above it.
# Replay Nov 9-12 (35 trades, real 1-minute option prices): as traded -$315,
# 1/4 at +25/50/75/100% +$12,976, 1/3 at +25/50/100% +$13,741.
# {name: [(gain %, share of the starting position sold by then)]}; the last step sells all.
LADDER_TEST_FROM = "2026-11-12"
LADDER_TESTS = {
    "1/4 at +25/+50/+75/+100%": [(25, 0.25), (50, 0.50), (75, 0.75), (100, 1.0)],
    "1/3 at +25/+50/+100%": [(25, 1 / 3), (50, 2 / 3), (100, 1.0)],
}
# Closed trades from this time on get a RESULT message with the confirmed P&L
# (CLOSED messages no longer carry a P&L - the live quotes are delayed).
RESULTS_FROM = "2026-10-19T09:30"
# 2R sales from this time on get their own RESULT message with the confirmed P&L of
# the contracts sold, while the rest of the position is still open (user, 2026-10-26).
TRIM_RESULTS_FROM = "2026-10-26T11:00"
MIN_DELTA_0DTE = 0.15        # expiry day (normally Friday): explicit so a later weekday change doesn't touch it; 0.25 is hard to find out of the money on 0DTE
STRIKES_TO_SCAN = 30          # out-of-the-money strikes examined beyond the nearest one (was 12 until 2026-11-03: only 3.3% out on GEV's $2.50 steps; the delta floor sets the real limit)
MIN_OPEN_INTEREST = 100
MIN_OPTION_VOLUME = 100     # contracts traded today in that strike (2026-10-16); unknown volume fails
MAX_SPREAD_FRACTION = 0.15   # (ask - bid) / mid must be at or below this
MIN_OPTION_PRICE = 0.50      # mid of at least $0.50 (2026-10-18): KO 94C at $0.05 x277 lost 90% on Oct 16
# Opening liquidity (user decision, 2026-11-09). The option quotes are 15 minutes
# delayed (TastyTrade demo feed; IBKR's API options too), so before 10:00 ET the
# executor sees the opening minute: almost no volume yet and wide opening spreads.
# On Nov 9 that skipped 6 of 7 first-hour entries on a trend day, TSLA among them
# (09:45 alert saw the 09:30:53 quotes: 380C volume 34, 385C spread 27%). Until
# EARLY_LIQUIDITY_UNTIL the volume test is dropped and the spread limit is
# EARLY_MAX_SPREAD_FRACTION; open interest (known, >= MIN_OPEN_INTEREST) and the
# $0.50 minimum still apply. The fill is repriced from 1-minute candles later.
EARLY_LIQUIDITY_UNTIL = (10, 0)
EARLY_MAX_SPREAD_FRACTION = 0.30

# Trade filters (2026-10-16, from the Oct 14-15 dry runs).
# Skip entries whose TradingView stop is closer than this to the entry, as a %
# of price: on those days every such stop was a one-bar stop-out or a target
# smaller than the option spread (8 trades, -$2,557 realistic).
MIN_STOP_PCT = 0.20
# The option's expected gain at the 2R target (|delta| x the stock move to the
# target) must be at least this multiple of the strike's bid/ask spread, or the
# spread eats the win (JNJ on Oct 14: TradingView 2R winner, options -$1,755).
MIN_TARGET_TO_SPREAD = 2.0

# At the 2R alert: sell half (rounded down). A single contract left after a partial fill
# is held through 2R and exits on the trail, break-even, stop or time exit.
TRIM_FRACTION = 0.5

# Hard limits.
# Losing-streak brake (2026-10-22): after this many stop-outs in a row in one day, new
# entries are skipped for the rest of the day - except setups graded in
# BRAKE_OVERRIDE_GRADES (the strongest conviction, full 5% size). A trade that reached
# 2R, or exited at break-even / trail / time, resets the streak; a 0DTE time stop counts
# as a loss only if it lost money.
# Loosened from 3 to 5 on 2026-11-07 (user decision). Replay Oct 14-Nov 6 with the
# first-hour QQQ rule below: brake 3 +81.9R, 4 +80.8R, 5 +79.8R, none +79.8R; without that
# rule brake 3 +46.8R vs none +63.6R (it skipped 14 live signals in Nov). At 5 it never
# fired in the sample: a backstop for a disaster day only. 0 turns it off.
LOSS_BRAKE = 5
BRAKE_OVERRIDE_GRADES = {"A+ OUTSIDE"}
MAX_OPEN_POSITIONS = 10      # raised from 5 (2026-10-18): on Oct 16 all 5 signals the cap blocked reached 2R; 10 would have taken them all
MAX_HOLD_SESSIONS = 2        # mirrors "Max sessions held" on DTS Entry (backup only)
# Overnight rule (2026-10-22, the user's idea): at the 15:56 sweep, a position that has not
# reached 2R is sold; only runners past 2R (stop at break-even) are held overnight. On
# Oct 14-21 the not-yet-2R overnight holds lost -$2,112 held vs +$1,896 sold at the close
# (TSLA / MRVL / AMD gapped through their stops on Oct 22).
# From 2026-11-04 (user decision) this has no exceptions: the strong-close hold, the late
# A+ OUTSIDE hold and the Friday weekend swing below no longer hold a position that hasn't
# reached 2R. Under those exceptions (Oct 24-Nov 3) 3 positions were held before 2R: MRVL
# +$1,256, META -$1,250, CRWD -$752 (-$746); no Friday swing was held. The -$5,174 quoted on
# Nov 4 for 9 such holds included 6 entered Oct 19-21, before this rule existed (TSLA
# -$2,840, MRVL -$2,040, AMD -$2,020 gapped through their stops). Runners past 2R made
# +$2,735 on the next day.
HOLD_OVERNIGHT_ONLY_PAST_2R = True
# Strong-close exception (rule C, 2026-10-26, user decision): a not-yet-2R position is held
# overnight when DTS Entry's 15:55 STRONG CLOSE alert says the stock is closing strong in
# the trade's direction (beyond its daily 8 EMA, outer third of the day's range, better than
# at 15:00). Oct 14-23: 4 such holds, +14.2R vs +1.9R selling all (+6.1R without UNH's
# earnings gap). Every overnight gap-through-stop (TSLA / AMD / MRVL, Oct 22) failed it.
# Off from 2026-11-04 (user decision, see HOLD_OVERNIGHT_ONLY_PAST_2R): its holds since
# Oct 27 were META -$1,250 and CRWD -$752 against MRVL +$1,256. The 15:55 alert is still
# recorded, so the rule can be studied.
STRONG_CLOSE_HOLD = False
# Earnings (2026-10-29, user decision): nothing is held overnight into an earnings report -
# at the 15:56 sweep a position is sold, even a runner past 2R or a strong-close hold, if
# the stock reports after today's close or before the next session's open (TastyTrade's
# expected report date; an unknown time of day counts as a risk).
NO_OVERNIGHT_INTO_EARNINGS = True
# Friday rules (2026-10-24, user decision). No new entries from FRIDAY_ENTRY_END: with
# under 3 hours to the 15:56 sweep, 1 in 10 entries after 13:00 reached 2R the same day
# and all 3 Friday entries after 13:00 lost (Oct 14-23, -$3,705 at market prices).
# Friday entries from FRIDAY_SWING_FROM buy next week's expiry and are swing trades. They
# were held over the weekend even before 2R until 2026-11-04; now, like every position,
# they are sold at the 15:56 sweep unless they have reached 2R.
FRIDAY_ENTRY_END = (13, 0)
FRIDAY_SWING_FROM = (12, 0)
# Friday swings are tighter (2026-10-24, user decision): only the strongest setups,
# small whatever the grade, and few of them. OUTSIDE setups made +$309 per $1k of
# premium on Oct 14-23 vs +$115 inside; grade B lost (-$2,088).
FRIDAY_SWING_GRADES = {"A+ OUTSIDE", "A OUTSIDE"}
FRIDAY_SWING_SIZE = 0.0125   # premium as a fraction of the account, any grade (halved again when choppy)
FRIDAY_SWING_MAX = 2         # swing positions open at once

# Time rules (New York). Entries only inside the regular session; the safety
# sweep runs after TradingView's own 15:55 alerts have had time to arrive.
ENTRY_START = (9, 30)
ENTRY_END = (16, 0)
# Afternoon cutoff (user decision; 12:00 on Oct 28, 14:00 on Oct 29, 13:00 from Oct 30).
# From this New York time (Monday-Thursday) a signal is taken only if
#   - its grade is in LATE_ENTRY_GRADES (held overnight even before 2R until 2026-11-04;
#     now sold at the 15:56 sweep if it hasn't reached 2R), or
#   - the market has strong momentum in the trade's direction (DTS Entry v15's QQQ tag):
#     QQQ at least MOMENTUM_DAY_PCT from the open and MOMENTUM_HOUR_PCT over the last hour
#     that way, on that side of its VWAP. A normal day trade (sold at the sweep before 2R).
# Other late signals are logged as SKIPPED, so their outcome can be studied.
# Entry-time P&L, Oct 19-29 at market prices: before 12:00 +$2.4k (59 trades, 20 reached 2R),
# 12:00-13:00 -$0.4k (19, 4 reached 2R), after 13:00 -$0.3k (18, 3 reached 2R). After 13:00,
# the 3 entries with QQQ trending their way made +$747; the other 12 -$1,560 (small sample).
ENTRY_CUTOFF = (13, 0)
# First hour with the market (2026-11-07, user decision): before this New York time an
# entry is taken only in QQQ's direction - QQQ up from the 09:30 open and above its VWAP
# for a long, down and below for a short (DTS Entry's QQQ tag); others are SKIPPED.
# Replay Oct 14-Nov 6 (17 days, no brake): +63.6R -> +79.8R, normal days -9.2R -> -1.1R,
# better on 11 days, worse on 4; Oct 14-26 +28.5 -> +41.9R, Oct 27-Nov 6 +35.1 -> +37.9R.
# First 30 min only (to 10:00): +71.7R. None = off.
QQQ_DIRECTION_UNTIL = (10, 30)
LATE_ENTRY_GRADES = {"A+ OUTSIDE"}
MOMENTUM_DAY_PCT = 0.5
MOMENTUM_HOUR_PCT = 0.25
SWEEP_AT = (15, 56)
# Market calendar, New York dates, from nyse.com/trade/hours-calendars (published to 2028;
# add each new year there). Half days (2026-11-02, user decision): no new entries on a
# 13:00 ET early close, the market is muted. A position carried into one keeps its usual
# exits, with the sweep moved up to HALF_DAY_SWEEP_AT. On a full holiday nothing is swept.
HALF_DAYS = {"2026-11-27", "2026-12-24", "2027-11-26", "2028-07-03", "2028-11-24"}
HALF_DAY_CLOSE = (13, 0)
HALF_DAY_SWEEP_AT = (12, 56)
MARKET_HOLIDAYS = {
    "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
    "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
    "2028-01-17", "2028-02-21", "2028-04-14", "2028-05-29", "2028-06-19", "2028-07-04",
    "2028-09-04", "2028-11-23", "2028-12-25",
}

ORDER_FILL_TIMEOUT_SECONDS = 30

# IBKR connection watch. Set while TWS isn't accepting API connections, so the
# Telegram warning goes out once per outage, plus once when it comes back.
IBKR_DOWN_FILE = STATE_DIR / "dts_ibkr_down"
HEALTH_START = (9, 25)        # the sweep job checks the TWS port every 5 min from here
HEALTH_END = (16, 0)
