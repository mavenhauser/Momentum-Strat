import json
import unittest
from datetime import date, datetime
from unittest import mock
import types

from day_trading_setup.executor import settings, contracts, signals


class ParseTest(unittest.TestCase):
    def test_entry(self):
        s = signals.parse("DTS: LONG NVDA @ 177.16 | stop 176.40 | target 178.68 (2R) | ORH breakout + 8 EMA retest")
        self.assertEqual((s.kind, s.ticker, s.side, s.price, s.stop, s.target, s.r_multiple, s.level),
                         ("ENTRY", "NVDA", "LONG", 177.16, 176.40, 178.68, 2.0, "ORH"))

    def test_short_entry_big_price(self):
        s = signals.parse("DTS: SHORT MU @ 1,022.32 | stop 1,030.10 | target 1,006.76 (2R) | PDL breakout + 8 EMA retest")
        self.assertEqual((s.kind, s.side, s.price, s.stop, s.level), ("ENTRY", "SHORT", 1022.32, 1030.10, "PDL"))

    def test_target(self):
        s = signals.parse("DTS: SHOP hit 2R target @ 177.47 - take partial, stop to break-even 176.42")
        self.assertEqual((s.kind, s.ticker, s.price, s.stop), ("TARGET", "SHOP", 177.47, 176.42))

    def test_runner_target(self):
        s = signals.parse("DTS: SHOP hit 4R target @ 179.57 - take partial, the rest rides the trail")
        self.assertEqual((s.kind, s.ticker, s.r_multiple, s.price, s.stop), ("TARGET", "SHOP", 4.0, 179.57, None))

    def test_exits(self):
        for reason in ("STOP", "BE", "TRAIL", "RUNNER", "WEAK", "EOD", "TIME"):
            s = signals.parse(f"DTS: EXIT SHORT TSLA ({reason}) @ 379.68")
            self.assertEqual((s.kind, s.ticker, s.side, s.reason, s.price), ("EXIT", "TSLA", "SHORT", reason, 379.68))

    def test_aplus_and_other(self):
        self.assertEqual(signals.parse("DTS: A+ OUTSIDE LONG MSFT @ 539.96 - watch for the 8 EMA retest entry").kind, "APLUS")
        self.assertEqual(signals.parse("DTS screener: $KO ▲ above PDH + PMH @ 88.77").kind, "IGNORED")
        self.assertEqual(signals.parse("").kind, "IGNORED")
        self.assertEqual(signals.parse("DTS: EXTENDED SHORT GEV @ 812.47 - already 4.2R from the open, no entry | grade A+ OUTSIDE").kind, "IGNORED")


class ContractTest(unittest.TestCase):
    def test_expiry_this_friday(self):
        exp = [date(2026, 10, 14), date(2026, 10, 16), date(2026, 10, 23), date(2026, 11, 20)]
        self.assertEqual(contracts.pick_expiry(exp, date(2026, 10, 12)), date(2026, 10, 16))  # Monday
        self.assertEqual(contracts.pick_expiry(exp, date(2026, 10, 16)), date(2026, 10, 16))  # Friday 0DTE
        self.assertEqual(contracts.pick_expiry(exp, date(2026, 10, 16), (9, 40)), date(2026, 10, 16))   # Friday morning: 0DTE
        self.assertEqual(contracts.pick_expiry(exp, date(2026, 10, 16), (10, 0)), date(2026, 10, 16))
        self.assertEqual(contracts.pick_expiry(exp, date(2026, 10, 16), (10, 30)), date(2026, 10, 16))
        self.assertEqual(contracts.pick_expiry(exp, date(2026, 10, 16), (11, 59)), date(2026, 10, 16))
        self.assertEqual(contracts.pick_expiry(exp, date(2026, 10, 16), (12, 0)), date(2026, 10, 23))   # Friday afternoon: next week
        self.assertEqual(contracts.pick_expiry(exp, date(2026, 10, 16), (14, 30)), date(2026, 10, 23))
        self.assertEqual(contracts.pick_expiry(exp, date(2026, 10, 12), (9, 40)), date(2026, 10, 16))   # Monday: unaffected
        self.assertEqual(contracts.pick_expiry([date(2026, 10, 16)], date(2026, 10, 16), (13, 0)), date(2026, 10, 16))  # no next week listed

    def test_expiry_holiday_friday_and_fallback(self):
        exp = [date(2026, 11, 26), date(2026, 12, 4)]   # no Friday listing that week
        self.assertEqual(contracts.pick_expiry(exp, date(2026, 11, 23)), date(2026, 11, 26))
        self.assertEqual(contracts.pick_expiry([date(2026, 11, 20)], date(2026, 10, 12)), date(2026, 11, 20))
        self.assertIsNone(contracts.pick_expiry([], date(2026, 10, 12)))

    def test_strikes(self):
        self.assertEqual(contracts.strikes_by_distance([170, 175, 177.5, 180, 182.5], 177.16, 3), [177.5, 175, 180])

    def test_liquidity(self):
        v = {"volume": 800}
        self.assertTrue(contracts.liquid({"bid": 2.0, "ask": 2.2, "open_interest": 500, **v}))
        self.assertFalse(contracts.liquid({"bid": 2.0, "ask": 2.6, "open_interest": 500, **v}))   # spread 26%
        self.assertFalse(contracts.liquid({"bid": 0, "ask": 0.1, "open_interest": 500, **v}))
        self.assertFalse(contracts.liquid({"bid": 2.0, "ask": 2.1, "open_interest": 10, **v}))
        self.assertTrue(contracts.liquid({"bid": 2.0, "ask": 2.1, "open_interest": 500, "volume": 401}))    # CVX 10-14
        self.assertFalse(contracts.liquid({"bid": 2.0, "ask": 2.1, "open_interest": 500, "volume": 24}))    # IBM 10-14
        self.assertFalse(contracts.liquid({"bid": 2.0, "ask": 2.1, "open_interest": 500, "volume": float("nan")}))
        self.assertFalse(contracts.liquid({"bid": 2.0, "ask": 2.1, "open_interest": 500}))                  # unknown
        self.assertFalse(contracts.liquid({"bid": 0.04, "ask": 0.05, "open_interest": 5000, **v}))         # KO 10-16: under $0.50
        self.assertTrue(contracts.liquid({"bid": 0.48, "ask": 0.53, "open_interest": 5000, **v}))

    def test_option_r(self):
        self.assertAlmostEqual(contracts.option_r(0.31, 166.34, 163.91), 0.7533, places=3)   # PLTR 2026-11-09
        self.assertAlmostEqual(contracts.option_r(-0.40, 100.0, 102.0), 0.80)                 # put, short
        self.assertIsNone(contracts.option_r(None, 100.0, 99.0))
        self.assertIsNone(contracts.option_r(float("nan"), 100.0, 99.0))
        self.assertIsNone(contracts.option_r(0.3, 100.0, 100.0))
        from day_trading_setup.executor import executor
        line = executor._option_2r_line({"delta": 0.31}, types.SimpleNamespace(price=166.34, stop=163.91), 13, 3.22)
        self.assertIn("4.73", line)
        self.assertIn("+$1,959 on 13", line)
        self.assertEqual(executor._option_2r_line({}, types.SimpleNamespace(price=1, stop=0.5), 1, 1.0), "")

    def test_opening_liquidity(self):
        # TSLA 2026-11-09 09:45 ET alert saw the 09:30:53 quotes (15-min delayed feed)
        c380 = {"bid": 3.35, "ask": 3.65, "open_interest": 7571, "volume": 34}
        c385 = {"bid": 2.12, "ask": 2.78, "open_interest": 2088, "volume": 125}
        self.assertFalse(contracts.liquid(c380))
        self.assertFalse(contracts.liquid(c385))
        self.assertTrue(contracts.liquid(c380, early=True))
        self.assertTrue(contracts.liquid(c385, early=True))                                       # spread 27%
        self.assertFalse(contracts.liquid({"bid": 2.0, "ask": 3.0, "open_interest": 5000}, early=True))   # spread 40%
        self.assertFalse(contracts.liquid({"bid": 2.0, "ask": 2.1, "volume": 500}, early=True))          # OI unknown
        self.assertFalse(contracts.liquid({"bid": 2.0, "ask": 2.1, "open_interest": 50}, early=True))
        self.assertFalse(contracts.liquid({"bid": 0.3, "ask": 0.35, "open_interest": 5000}, early=True)) # under $0.50

    def test_sizing(self):
        self.assertEqual(contracts.contracts_for(100_000, 2.50), 10)          # 2.5% default: $2,500 / $250
        self.assertEqual(contracts.contracts_for(100_000, 2.50, 0.05), 20)    # A+ OUTSIDE: $5,000 / $250
        self.assertEqual(contracts.contracts_for(100_000, 30.0), 1)           # below one contract -> 1
        self.assertEqual(contracts.contracts_for(100_000, None), 0)

    def test_size_by_grade(self):
        self.assertEqual(contracts.size_fraction("A+ OUTSIDE"), 0.05)
        self.assertEqual(contracts.size_fraction("A OUTSIDE"), 0.035)
        self.assertEqual(contracts.size_fraction("A+ inside"), settings.SIZE_DEFAULT)
        self.assertEqual(contracts.size_fraction(None), settings.SIZE_DEFAULT)
        self.assertTrue(all(f <= 0.05 for f in settings.SIZE_BY_GRADE.values()))

    def test_parse_grade(self):
        base = "DTS: LONG NVDA @ 177.16 | stop 176.40 | target 178.68 (2R) | ORH breakout + 8 EMA retest"
        self.assertIsNone(signals.parse(base).grade)                              # pre-v6 alert
        self.assertEqual(signals.parse(base + " | grade A+ OUTSIDE").grade, "A+ OUTSIDE")
        s = signals.parse(base + " | grade B inside")
        self.assertEqual((s.kind, s.grade, s.target), ("ENTRY", "B inside", 178.68))

    def test_parse_chop(self):
        base = "DTS: SHORT ORCL @ 129.21 | stop 129.97 | target 127.69 (2R) | ORL breakout + 8 EMA retest"
        s = signals.parse(base + " | choppy (QQQ crosses 9) | grade A inside")
        self.assertEqual((s.kind, s.chop, s.grade), ("ENTRY", 9, "A inside"))
        self.assertIsNone(signals.parse(base + " | grade A inside").chop)

    def test_parse_trend_day_entry(self):
        # v19.0: trend-day entries say "VWAP band retest" and carry a "| trend day" tag
        s = signals.parse("DTS: SHORT MU @ 731.20 | stop 738.90 | target 715.80 (2R) | ORL breakout + VWAP band retest"
                          " | choppy (QQQ crosses 9) | trend day | QQQ -1.84% (1h -0.92%) below VWAP | grade A+ OUTSIDE")
        self.assertEqual((s.kind, s.side, s.ticker, s.price, s.stop, s.target, s.level, s.chop, s.grade),
                         ("ENTRY", "SHORT", "MU", 731.2, 738.9, 715.8, "ORL", 9, "A+ OUTSIDE"))
        self.assertEqual(s.qqq, (-1.84, -0.92, False))

    def test_min_delta_override(self):
        # 0DTE floor passed explicitly: a 0.12-delta strike only qualifies if the floor is lowered
        qs = {100: self.q(4.90, 5.10, 0.55), 102.5: self.q(3.80, 4.00, 0.45), 107.5: self.q(1.90, 2.00, 0.12)}
        self.assertEqual(contracts.choose_strike(qs, 100.2, "C", 1000, min_delta=0.15)[:2], (102.5, 2))
        self.assertEqual(contracts.choose_strike(qs, 100.2, "C", 1000, min_delta=0.10)[:2], (107.5, 5))

    def test_trim(self):
        self.assertEqual([contracts.trim_quantity(q) for q in (0, 1, 2, 3, 4, 5)], [0, 0, 1, 1, 2, 2])

    def q(self, bid, ask, delta, oi=1000):
        return {"bid": bid, "ask": ask, "delta": delta, "open_interest": oi, "volume": 1000}

    def test_ladder(self):
        ks = [210, 212.5, 215, 217.5, 220, 222.5, 225]
        self.assertEqual(contracts.strike_ladder(ks, 218.1, "P", 3), [217.5, 215, 212.5, 210])
        self.assertEqual(contracts.strike_ladder(ks, 218.1, "C", 3), [220, 222.5, 225])        # 217.5 call is ITM
        self.assertEqual(contracts.strike_ladder(ks, 217.6, "C", 3), [217.5, 220, 222.5, 225])  # 0.05% ITM = at the money
        self.assertEqual(contracts.strike_ladder(ks, 217.4, "P", 2), [217.5, 215, 212.5])       # 0.05% ITM put = at the money
        self.assertEqual(contracts.strike_ladder(ks, 216.0, "P", 2), [215, 212.5, 210])         # 217.5 put is ITM

    def test_never_in_the_money(self):
        # only the ITM strike is cheap and liquid enough: nothing ITM is ever bought
        qs = {215: self.q(3.00, 3.10, 0.70), 220: self.q(0.50, 0.70, 0.30), 222.5: self.q(0.20, 0.40, 0.18)}
        k, qty, rule, d = contracts.choose_strike(qs, 218.1, "C", 1000)
        self.assertNotEqual(k, 215)
        self.assertIsNone(k)

    def test_choose_four_at_the_money(self):
        # NVDA-like puts, entry 218: the at-the-money strike already buys 6
        qs = {225: self.q(4.95, 5.05, -0.87), 220: self.q(2.33, 2.36, -0.66), 217.5: self.q(1.49, 1.53, -0.53),
              215: self.q(0.95, 0.96, -0.40), 212.5: self.q(0.58, 0.60, -0.28)}
        self.assertEqual(contracts.choose_strike(qs, 218.0, "P", 1000)[:2], (217.5, 6))

    def test_choose_steps_out_for_four(self):
        # MSFT-like puts, entry 536: step out until 4 contracts, delta still >= 0.15
        qs = {537.5: self.q(6.05, 7.00, -0.72), 535: self.q(4.70, 5.35, -0.62), 532.5: self.q(3.65, 3.95, -0.53),
              530: self.q(2.60, 2.85, -0.43), 527.5: self.q(2.20, 2.45, -0.33), 525: self.q(1.40, 1.55, -0.24)}
        k, qty, rule, d = contracts.choose_strike(qs, 536.0, "P", 1000)
        self.assertEqual((k, qty), (527.5, 4))
        self.assertTrue(rule.startswith("4+"))

    def test_choose_further_otm_down_to_min_delta(self):
        # weeklies: the 0.20-delta strike is allowed (floor 0.15) and buys 5
        qs = {100: self.q(4.90, 5.10, 0.55), 102.5: self.q(3.80, 4.00, 0.45), 105: self.q(2.85, 3.00, 0.34),
              107.5: self.q(1.90, 2.00, 0.20)}
        k, qty, rule, d = contracts.choose_strike(qs, 100.2, "C", 1000)
        self.assertEqual((k, qty), (107.5, 5))

    def test_choose_two_when_four_needs_too_low_delta(self):
        # only the 0.10-delta strike would buy 4: settle for 2 contracts closer in
        qs = {100: self.q(4.90, 5.10, 0.55), 102.5: self.q(3.80, 4.00, 0.45), 105: self.q(2.85, 3.00, 0.34),
              107.5: self.q(1.90, 2.00, 0.10)}
        k, qty, rule, d = contracts.choose_strike(qs, 100.2, "C", 1000)
        self.assertEqual((k, qty), (102.5, 2))

    def test_no_entry_when_only_one_contract_fits(self):
        qs = {500: self.q(11.00, 11.40, 0.52), 505: self.q(8.40, 8.80, 0.43), 510: self.q(6.10, 6.40, 0.33), 515: self.q(5.80, 6.05, 0.12)}
        k, qty, rule, d = contracts.choose_strike(qs, 500.3, "C", 1000)
        self.assertEqual((k, qty), (None, 0))
        self.assertIn("under 2 contracts", rule)

    def test_high_priced_stock_buys_further_out_past_twelve_strikes(self):
        # GEV-like: $2.50 steps, and only the 930 call, 14 strikes out, is liquid and buys 2
        qs = {k: self.q(11.00, 11.40, 0.40) for k in [895 + 2.5 * i for i in range(14)]}
        qs[930] = self.q(5.60, 6.10, 0.17)
        k, qty, rule, d = contracts.choose_strike(qs, 895.41, "C", 1750)
        self.assertEqual((k, qty), (930, 2))

    def test_no_entry_when_a_cheap_strike_is_below_the_delta_floor(self):
        # 515 would buy 2, but only under the 0.15 delta floor
        qs = {500: self.q(11.00, 11.40, 0.52), 510: self.q(6.10, 6.40, 0.33), 515: self.q(4.80, 5.00, 0.12)}
        k, qty, rule, d = contracts.choose_strike(qs, 500.3, "C", 1000)
        self.assertEqual((k, qty), (None, 0))
        self.assertIn("under 2 contracts", rule)

    def test_two_contracts_still_trade_further_out_the_ladder(self):
        # the near strikes are too wide to trade: take the first liquid one that buys 2
        wide = lambda d: self.q(5.00, 7.00, d)
        qs = {500: wide(0.52), 505: wide(0.45), 510: wide(0.38), 495: wide(0.60),
              515: wide(0.30), 520: self.q(4.60, 4.80, 0.22)}
        k, qty, rule, d = contracts.choose_strike(qs, 500.5, "C", 1000)
        self.assertEqual((k, qty), (520, 2))

    def test_stop_pct(self):
        self.assertAlmostEqual(contracts.stop_pct(250.0, 249.75), 0.1)
        self.assertLess(contracts.stop_pct(250.0, 249.75), settings.MIN_STOP_PCT)
        self.assertGreaterEqual(contracts.stop_pct(460.98, 464.66), settings.MIN_STOP_PCT)

    def test_spread_filter_moves_to_a_tighter_strike(self):
        # target $0.50 away: the wide at-the-money strike fails (0.5*0.5 < 2*0.40),
        # the tight one next to it passes (0.45*0.5 >= 2*0.10)
        qs = {250: self.q(2.00, 2.40, 0.50), 252.5: self.q(1.00, 1.10, 0.45)}
        k, qty, rule, d = contracts.choose_strike(qs, 250.1, "C", 1000, move=0.50)
        self.assertEqual((k, qty), (252.5, 9))

    def test_spread_filter_skips(self):
        # JNJ-like: every strike's spread is too wide for a $0.50 target
        qs = {250: self.q(2.00, 2.30, 0.50), 252.5: self.q(1.00, 1.15, 0.40)}
        k, qty, rule, d = contracts.choose_strike(qs, 250.1, "C", 1000, move=0.50)
        self.assertIsNone(k)
        self.assertIn("too small for the option spread", rule)

    def test_zero_dte_time_stop_off(self):
        from zoneinfo import ZoneInfo
        ny = ZoneInfo("America/New_York")
        pos = {"opened_at": "2026-10-16T13:40:00-04:00", "expiry": "2026-10-16", "trimmed": False}
        self.assertEqual(settings.ZERO_DTE_TIME_STOP_MIN, 0)              # removed 2026-11-24
        self.assertFalse(contracts.time_stop_due(pos, datetime(2026, 10, 16, 15, 30, tzinfo=ny)))

    def test_qqq_open_exit(self):
        from zoneinfo import ZoneInfo
        ny = ZoneInfo("America/New_York")
        t = datetime(2026, 11, 24, 12, 30, tzinfo=ny)
        lng = {"side": "LONG", "opened_at": "2026-11-24T12:05:00-05:00", "qty": 4, "open_qty": 4, "trimmed": False}
        sht = {**lng, "side": "SHORT"}
        self.assertTrue(contracts.qqq_open_exit_due(lng, 639.0, 640.05, t))     # QQQ below its open: long out
        self.assertFalse(contracts.qqq_open_exit_due(lng, 641.0, 640.05, t))
        self.assertTrue(contracts.qqq_open_exit_due(sht, 641.0, 640.05, t))     # above: short out
        self.assertFalse(contracts.qqq_open_exit_due(sht, 639.0, 640.05, t))
        self.assertFalse(contracts.qqq_open_exit_due({**lng, "trimmed": True}, 639.0, 640.05, t))          # past 2R
        self.assertFalse(contracts.qqq_open_exit_due({**lng, "open_qty": 2}, 639.0, 640.05, t))           # 2R half sold
        self.assertFalse(contracts.qqq_open_exit_due({**lng, "opened_at": "2026-11-23T12:05:00-05:00"}, 639.0, 640.05, t))  # held overnight
        self.assertFalse(contracts.qqq_open_exit_due({**lng, "opened_at": "2026-11-24T12:31:00-05:00"}, 639.0, 640.05, t))  # after this bar
        with mock.patch.object(settings, "QQQ_OPEN_EXIT", False):
            self.assertFalse(contracts.qqq_open_exit_due(lng, 639.0, 640.05, t))

    def test_qqq_bar_parse(self):
        s = signals.parse("DTS: QQQ 5M CLOSE 650.12 | open 640.05")
        self.assertEqual((s.kind, s.ticker, s.price, s.stop), ("QQQBAR", "QQQ", 650.12, 640.05))

    def test_limits_from_review(self):
        self.assertEqual(settings.LOSS_BRAKE, 0)
        self.assertEqual(settings.MAX_OPEN_POSITIONS, 12)

    @mock.patch.object(settings, "ZERO_DTE_TIME_STOP_MIN", 90)   # the mechanism, if it is ever switched back on
    def test_zero_dte_time_stop(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        ny = ZoneInfo("America/New_York")
        pos = {"opened_at": datetime(2026, 10, 16, 13, 40, tzinfo=ny).isoformat(), "expiry": "2026-10-16", "trimmed": False}
        self.assertFalse(contracts.time_stop_due(pos, datetime(2026, 10, 16, 15, 9, tzinfo=ny)))    # 89 min
        self.assertTrue(contracts.time_stop_due(pos, datetime(2026, 10, 16, 15, 10, tzinfo=ny)))    # 90 min: TSLA would be out
        self.assertFalse(contracts.time_stop_due({**pos, "trimmed": True}, datetime(2026, 10, 16, 15, 30, tzinfo=ny)))  # reached 2R
        self.assertFalse(contracts.time_stop_due({**pos, "expiry": "2026-10-23"}, datetime(2026, 10, 16, 15, 30, tzinfo=ny)))  # next week's

    def test_loss_streak(self):
        d = "2026-10-21"
        c = lambda hm, why, trimmed=False: {"closed_at": f"{d}T{hm}:00-04:00", "close_reason": why, "trimmed": trimmed}
        stops = [c("10:25", "TradingView STOP @ 1"), c("10:35", "TradingView STOP @ 2"), c("10:55", "TradingView STOP @ 3")]
        self.assertEqual(contracts.loss_streak(stops, date(2026, 10, 21)), 3)
        self.assertEqual(contracts.loss_streak(stops + [c("11:30", "TradingView TRAIL @ 4", True)], date(2026, 10, 21)), 0)   # a winner resets
        self.assertEqual(contracts.loss_streak(stops[:2] + [c("10:40", "TradingView BE @ 5", True)] + stops[2:3], date(2026, 10, 21)), 1)
        self.assertEqual(contracts.loss_streak(stops, date(2026, 10, 22)), 0)                                               # new day
        tsla = {**c("11:30", "0DTE time stop: no 2R within 90 min"), "pnl_usd": 1250.0}
        self.assertEqual(contracts.loss_streak(stops + [tsla], date(2026, 10, 21)), 0)                                      # profitable time stop resets (Nov 6)
        self.assertEqual(contracts.loss_streak(stops + [{**tsla, "pnl_confirmed": -300}], date(2026, 10, 21)), 4)            # losing one counts

    def test_loss_brake_threshold(self):
        from day_trading_setup.executor import executor
        d = "2026-10-21"
        stops = [{"closed_at": f"{d}T10:{m}:00-04:00", "close_reason": "TradingView STOP @ 1", "trimmed": False} for m in (25, 35, 45, 55)]
        sig = signals.parse("DTS: SHORT TSLA @ 371.26 | stop 374.52 | target 364.74 (2R) | PML breakout | grade A inside")
        def run(closed, brake):
            sent = []
            with mock.patch.object(executor, "notify", sent.append), mock.patch.object(settings, "LOSS_BRAKE", brake), \
                 mock.patch.object(executor, "choose_contract", lambda b, s, t: (None, "stub")):
                executor.on_entry(None, {"positions": {}, "closed": closed}, sig, datetime(2026, 10, 21, 11, 0, tzinfo=executor.NY))
            return sent[0]
        self.assertNotIn("loss brake", run(stops, 5))          # 4 in a row: still trading
        self.assertIn("loss brake: 5 stop-outs", run(stops + [{**stops[0], "closed_at": f"{d}T10:58:00-04:00"}], 5))
        self.assertNotIn("loss brake", run(stops * 2, 0))     # 0 = off

    def test_first_hour_with_qqq(self):
        from day_trading_setup.executor import executor
        base = "DTS: LONG NVDA @ 199.90 | stop 197.20 | target 205.30 (2R) | PDH breakout + 8 EMA retest"
        up = signals.parse(base + " | QQQ +0.40% (1h +0.40%) above VWAP | grade A inside")
        down = signals.parse(base + " | QQQ -0.30% (1h -0.30%) below VWAP | grade A inside")
        up_below = signals.parse(base + " | QQQ +0.10% (1h -0.20%) below VWAP | grade A inside")
        self.assertTrue(contracts.with_qqq(up))
        self.assertFalse(contracts.with_qqq(down))
        self.assertFalse(contracts.with_qqq(up_below))
        self.assertTrue(contracts.with_qqq(signals.parse(base)))                     # no QQQ tag: never blocked
        short = signals.parse(base.replace("LONG", "SHORT") + " | QQQ -0.30% (1h -0.30%) below VWAP | grade A inside")
        self.assertTrue(contracts.with_qqq(short))
        def run(sig, hm):
            sent = []
            state = {"positions": {}, "closed": []}
            with mock.patch.object(executor, "notify", sent.append), \
                 mock.patch.object(executor, "choose_contract", lambda b, s, t: (None, "stub")):
                executor.on_entry(None, state, sig, datetime(2026, 11, 6, *hm, tzinfo=executor.NY))
            return sent[0], state
        msg, state = run(down, (9, 45))
        self.assertIn("before 10:30 only trades in the direction of QQQ (QQQ -0.30% (1h -0.30%) below VWAP)", msg)
        self.assertEqual(state["skipped"][0]["ticker"], "NVDA")
        self.assertNotIn("direction of QQQ", run(up, (9, 45))[0])
        self.assertNotIn("direction of QQQ", run(down, (10, 30))[0])                   # from 10:30 the usual rules

    def test_close_before_overnight(self):
        thu = "2026-10-22T13:20:00-04:00"
        self.assertTrue(contracts.close_before_overnight({"trimmed": False, "opened_at": thu}))   # not yet 2R: sold at 15:56
        self.assertFalse(contracts.close_before_overnight({"trimmed": True, "opened_at": thu}))   # runner past 2R: held
        self.assertTrue(contracts.close_before_overnight({"trimmed": False, "opened_at": "2026-10-23T11:30:00-04:00"}))   # Friday morning 0DTE
        self.assertTrue(contracts.close_before_overnight({"trimmed": False, "opened_at": "2026-10-23T12:36:00-04:00"}))   # Friday swing: sold too (2026-11-04)
        self.assertFalse(contracts.close_before_overnight({"trimmed": True, "opened_at": "2026-10-23T12:36:00-04:00"}))   # Friday swing past 2R: held

    def test_afternoon_cutoff(self):
        from day_trading_setup.executor import executor
        sent = []
        with mock.patch.object(executor, "notify", sent.append):
            state = {"positions": {}, "closed": []}
            sig = signals.parse("DTS: LONG AAPL @ 347.52 | stop 346.16 | target 350.24 (2R) | PDH breakout | grade A OUTSIDE")
            executor.on_entry(None, state, sig, datetime(2026, 10, 27, 13, 0, tzinfo=executor.NY))
        self.assertIn("from 13:00 only A+ OUTSIDE setups or a strong market trend in the same direction "
                      "(this one: A OUTSIDE, QQQ trend not in the alert)", sent[0])
        self.assertTrue(contracts.late_entry(datetime(2026, 10, 27, 13, 5)))
        self.assertFalse(contracts.late_entry(datetime(2026, 10, 27, 12, 59)))
        self.assertFalse(contracts.late_entry(datetime(2026, 10, 30, 14, 5)))      # Friday: FRIDAY_ENTRY_END rules
        thu = "2026-10-27T14:20:00-04:00"
        self.assertTrue(contracts.close_before_overnight({"trimmed": False, "opened_at": thu, "late": "overnight"}))   # late A+: sold too (2026-11-04)
        self.assertTrue(contracts.close_before_overnight({"trimmed": False, "opened_at": thu, "late": "momentum"}))     # day trade
        self.assertTrue(contracts.close_before_overnight({"trimmed": False, "opened_at": "2026-10-27T13:20:00-04:00"}))

    def test_late_entry_on_market_momentum(self):
        from day_trading_setup.executor import executor
        base = "DTS: SHORT MSFT @ 523.11 | stop 527.12 | target 515.10 (2R) | ORL breakout + 8 EMA retest"
        sig = signals.parse(base + " | QQQ -1.05% (1h -0.53%) below VWAP | grade A OUTSIDE")
        self.assertEqual(sig.qqq, (-1.05, -0.53, False))
        self.assertEqual(sig.grade, "A OUTSIDE")
        self.assertTrue(contracts.market_momentum(sig))
        stalled = signals.parse(base + " | QQQ -0.79% (1h +0.05%) below VWAP | grade A OUTSIDE")    # trend stalled
        self.assertFalse(contracts.market_momentum(stalled))
        wrong_side = signals.parse(base + " | QQQ -0.60% (1h -0.30%) above VWAP | grade A OUTSIDE")
        self.assertFalse(contracts.market_momentum(wrong_side))
        sent = []
        with mock.patch.object(executor, "notify", sent.append):
            state = {"positions": {}, "closed": []}
            executor.on_entry(None, state, stalled, datetime(2026, 10, 27, 13, 20, tzinfo=executor.NY))
        self.assertIn("QQQ -0.79% (1h +0.05%) below VWAP", sent[0])
        self.assertEqual(state["skipped"][0]["ticker"], "MSFT")

    def test_friday_entry_cutoff(self):
        from day_trading_setup.executor import executor
        sent = []
        with mock.patch.object(executor, "notify", sent.append), mock.patch.object(settings, "ENTRY_CUTOFF", (16, 0)):
            state = {"positions": {}, "closed": []}
            sig = signals.parse("DTS: SHORT TSLA @ 371.26 | stop 374.52 | target 364.74 (2R) | PML breakout")
            executor.on_entry(None, state, sig, datetime(2026, 10, 23, 13, 10, tzinfo=executor.NY))
        self.assertIn("no new entries on Friday from 13:00", sent[0])
        self.assertEqual(state["skipped"][0]["ticker"], "TSLA")

    def test_entry_only_on_the_days_list(self):
        # 2026-11-17 (user decision): a ticker held overnight keeps its alert for its exits,
        # but a new entry needs the ticker on the day's list. Missing or older list: no block.
        import tempfile
        from pathlib import Path
        from day_trading_setup.executor import executor
        sig = signals.parse("DTS: SHORT TSLA @ 371.26 | stop 374.52 | target 364.74 (2R) | PML breakout")
        t = datetime(2026, 11, 18, 11, 0, tzinfo=executor.NY)
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "list.json"
            with mock.patch.object(settings, "DAY_LIST_FILE", f):
                self.assertFalse(executor.off_day_list("TSLA", t.date()))          # no file
                f.write_text(json.dumps({"date": "2026-11-17", "tickers": ["AMD"]}))
                self.assertFalse(executor.off_day_list("TSLA", t.date()))          # yesterday's list
                f.write_text(json.dumps({"date": "2026-11-18", "tickers": ["AMD", "TSLA"]}))
                self.assertFalse(executor.off_day_list("TSLA", t.date()))
                f.write_text(json.dumps({"date": "2026-11-18", "tickers": ["AMD"], "held_only": []}))
                self.assertFalse(executor.off_day_list("TSLA", t.date()))          # missing from the file: never blocked
                f.write_text(json.dumps({"date": "2026-11-17", "tickers": ["AMD"], "held_only": ["TSLA"]}))
                self.assertFalse(executor.off_day_list("TSLA", t.date()))          # held-only yesterday
                f.write_text(json.dumps({"date": "2026-11-18", "tickers": ["AMD"], "held_only": ["TSLA"]}))
                sent = []
                with mock.patch.object(executor, "notify", sent.append):
                    state = {"positions": {}, "closed": []}
                    executor.on_entry(None, state, sig, t)
                self.assertIn("not on today", sent[-1])
                self.assertEqual(state["skipped"][0]["ticker"], "TSLA")
                self.assertEqual(state["positions"], {})

    def test_end_of_day_resize(self):
        executor_ny = lambda: __import__("day_trading_setup.executor.executor", fromlist=["NY"]).NY
        # 2026-11-17 (user decision): no resize during the session; at the end of the day, when
        # the journal is built, each trade opened that day is re-booked at budget / real price.
        from day_trading_setup.executor import resize, reprice
        def pos(tk, qty, est, sells=(), closed=True, frac=0.05, day="2026-11-18"):
            t0 = f"{day}T09:45:00-05:00"
            ev = [{"t": t0, "action": f"BUY {qty} @ {est}", "qty": qty, "price": est}]
            for i, (n, px, why) in enumerate(sells):
                ev.append({"t": f"{day}T10:{10 + i}:00-05:00", "action": f"SELL {n} @ {px} ({why})", "qty": n, "price": px})
            p = {"ticker": tk, "streamer": f".{tk}261120P100", "right": "P", "strike": 100.0, "qty": qty,
                 "open_qty": qty - sum(n for n, _, _ in sells), "avg_cost": est, "size_fraction": frac,
                 "nlv": 100000, "opened_at": t0, "events": ev, "trimmed": bool(sells), "pnl_confirmed": 1.0}
            if closed:
                p.update(closed_at=f"{day}T15:55:00-05:00", open_qty=0)
            return p
        unh = pos("UNH", 12, 4.07, [(12, 4.24, "TradingView STOP @ 395.13")])
        mu = pos("MU", 4, 8.41, [(2, 13.91, "2R trim"), (2, 12.44, "TradingView TRAIL @ 685.2")], frac=0.035)
        mu["events"][1]["pnl_confirmed"] = 1290
        held = pos("GOOGL", 4, 3.67, [(2, 6.12, "2R trim")], closed=False, frac=0.0175)   # runner held overnight
        ok = pos("AMZN", 9, 4.95, [(9, 3.96, "TradingView STOP @ 256.99")])
        unpriced = pos("META", 4, 10.4, [(4, 8.9, "TradingView STOP @ 621.5")])
        old_day = pos("AMD", 17, 1.98, [(17, 20.0, "EOD")], day="2026-11-17")
        state = {"positions": {"GOOGL": held}, "closed": [unh, mu, ok, unpriced, old_day]}
        k = lambda p, i: reprice.event_key(p, p["events"][i])
        rp = {k(unh, 0): {"true": 6.50}, k(unh, 1): {"true": 4.22},
              k(mu, 0): {"true": 9.15}, k(mu, 1): {"true": 15.60}, k(mu, 2): {"true": 11.73},
              k(held, 0): {"true": 3.50}, k(held, 1): {"true": 6.09}, k(ok, 0): {"true": 5.12},
              k(old_day, 0): {"true": 7.28}}
        out = resize.resize_day(state, date(2026, 11, 18), rp)
        self.assertEqual({r["ticker"]: (r["from"], r["to"]) for r in out["resized"]},
                         {"UNH": (12, 7), "MU": (4, 3), "GOOGL": (4, 5)})
        self.assertEqual((out["unchanged"], out["no_price"]), (1, ["META"]))
        self.assertEqual([e["qty"] for e in unh["events"]], [7, 7])
        self.assertEqual(unh["pnl_confirmed"], -1596)
        self.assertEqual([e["qty"] for e in mu["events"]], [3, 1, 2])           # 2R = half rounded down
        self.assertEqual((mu["events"][1]["pnl_confirmed"], mu["pnl_confirmed"]), (645, 1161))
        self.assertEqual((held["qty"], held["events"][1]["qty"], held["open_qty"]), (5, 2, 3))
        self.assertEqual(old_day["qty"], 17)                                     # another day: untouched
        self.assertNotIn("resize_checked", unpriced)                              # retried next run
        again = resize.resize_day(state, date(2026, 11, 18), rp)
        self.assertEqual((again["resized"], again["unchanged"]), ([], 0))         # once only
        # the command refuses a day that isn't over: no resizing during the session
        import io, contextlib
        for now, refused in ((datetime(2026, 11, 18, 9, 49, tzinfo=executor_ny()), True),
                             (datetime(2026, 11, 18, 8, 0, tzinfo=executor_ny()), True),
                             (datetime(2026, 11, 18, 16, 20, tzinfo=executor_ny()), False)):
            buf = io.StringIO()
            with mock.patch.object(resize, "datetime", mock.Mock(now=lambda tz=None: now, fromisoformat=datetime.fromisoformat)), \
                 mock.patch.object(settings, "EXECUTE", False), \
                 mock.patch("day_trading_setup.executor.reprice.run", lambda mode: None), \
                 mock.patch.object(resize, "resize_day", lambda st, d, rp: {"date": d.isoformat(), "resized": []}), \
                 mock.patch.object(settings, "DRY_STATE_FILE", mock.Mock(read_text=lambda: "{}", with_suffix=lambda s: mock.Mock())), \
                 mock.patch.object(resize.os, "replace", lambda a, b: None), \
                 mock.patch.object(settings, "LOCK_FILE", "/dev/null"), \
                 contextlib.redirect_stdout(buf):
                resize.main(["--date", "2026-11-18"])
            self.assertEqual("skipped" in buf.getvalue(), refused, now)
        early = pos("TSLA", 12, 4.0, [(12, 3.0, "EOD")], day="2026-11-17")
        self.assertEqual(resize.resize_day({"positions": {}, "closed": [early]}, date(2026, 11, 17),
                                           {k(early, 0): {"true": 6.5}})["resized"], [])   # before RESIZE_FROM

    def test_half_day_no_new_entries(self):
        from day_trading_setup.executor import executor
        sent = []
        sig = signals.parse("DTS: SHORT TSLA @ 371.26 | stop 374.52 | target 364.74 (2R) | PML breakout")
        for t in (datetime(2026, 11, 27, 10, 0, tzinfo=executor.NY), datetime(2026, 12, 24, 10, 0, tzinfo=executor.NY)):
            with mock.patch.object(executor, "notify", sent.append), mock.patch.object(settings, "HALF_DAY_TRADE_DATES", set()):
                state = {"positions": {}, "closed": []}
                executor.on_entry(None, state, sig, t)
            self.assertIn("half day", sent[-1])
            self.assertEqual(state["skipped"][0]["ticker"], "TSLA")
            self.assertEqual(state["positions"], {})

    def test_half_day_exception_trades_until_noon(self):
        from day_trading_setup.executor import executor
        sent = []
        sig = signals.parse("DTS: SHORT TSLA @ 371.26 | stop 374.52 | target 364.74 (2R) | PML breakout")
        with mock.patch.object(executor, "notify", sent.append), \
             mock.patch.object(settings, "HALF_DAY_TRADE_DATES", {"2026-11-27"}), \
             mock.patch.object(executor, "check_day_rules", lambda s, t: (_ for _ in ()).throw(RuntimeError("past the half-day check"))):
            state = {"positions": {}, "closed": []}
            executor.on_entry(None, state, sig, datetime(2026, 11, 27, 12, 0, tzinfo=executor.NY))
            self.assertIn("no new entries from 12:00", sent[-1])
            with self.assertRaisesRegex(RuntimeError, "past the half-day check"):     # 11:59: carries on to the usual checks
                executor.on_entry(None, state, sig, datetime(2026, 11, 27, 11, 59, tzinfo=executor.NY))
            with mock.patch.object(executor, "skip", lambda *a: None):
                executor.on_entry(None, state, sig, datetime(2026, 12, 24, 10, 0, tzinfo=executor.NY))
            self.assertIn("half day (13:00 close): no new entries", sent[-1])          # other half days unchanged

    def test_first_hour_up_on_the_day(self):
        """QQQ_UP_ON_DAY (2026-11-28): QQQ down from its open but up on the day lets a first-hour long in,
        and its QQQ early exit uses yesterday's close."""
        from day_trading_setup.executor import executor
        NY = executor.NY
        state = {"positions": {}, "closed": [], "qqq_bar": {"t": "2026-11-27T09:50:00-05:00", "close": 640.03, "open": 645.72},
                 "qqq_close": {"2026-11-25": {"t": "2026-11-25T16:00:21-05:00", "close": 633.95}}}
        self.assertEqual(contracts.prev_session(date(2026, 11, 27)), date(2026, 11, 25))     # Thanksgiving skipped
        self.assertEqual(contracts.prev_session(date(2026, 11, 30)), date(2026, 11, 27))     # weekend skipped
        lng = signals.parse("DTS: LONG TXN @ 272.96 | stop 268.53 | target 281.82 (2R) | PDH breakout + 8 EMA retest | QQQ -1.01% (1h +1.11%) below VWAP | grade B inside")
        sht = signals.parse("DTS: SHORT TXN @ 272.96 | stop 276.00 | target 266.88 (2R) | PDL breakout + 8 EMA retest | QQQ -1.01% (1h +1.11%) below VWAP | grade B inside")
        day = date(2026, 11, 27)
        self.assertEqual(contracts.qqq_day_ref(state, lng, day), 633.95)          # 645.72 x (1 - 1.01%) = 639.2 > 633.95
        self.assertIsNone(contracts.qqq_day_ref(state, sht, day))                 # a short with QQQ down from its open: the normal rule
        deep = signals.parse(lng.raw.replace("QQQ -1.01%", "QQQ -2.10%"))         # 632.2: below yesterday's close too
        self.assertIsNone(contracts.qqq_day_ref(state, deep, day))
        self.assertIsNone(contracts.qqq_day_ref({**state, "qqq_close": {}}, lng, day))         # no close kept: the normal rule
        self.assertIsNone(contracts.qqq_day_ref({**state, "qqq_bar": {**state["qqq_bar"], "t": "2026-11-25T15:55:00-05:00"}}, lng, day))  # stale bar
        with mock.patch.object(settings, "QQQ_UP_ON_DAY", False):
            self.assertIsNone(contracts.qqq_day_ref(state, lng, day))
        # on_entry lets it past the first-hour rule (the next check raises to stop the test there)
        sent = []
        with mock.patch.object(executor, "notify", sent.append), mock.patch.object(executor, "off_day_list", lambda *a: False), \
             mock.patch.object(contracts, "late_entry", lambda t: (_ for _ in ()).throw(RuntimeError("past the QQQ rule"))):
            with self.assertRaisesRegex(RuntimeError, "past the QQQ rule"):
                executor.on_entry(None, dict(state), lng, datetime(2026, 11, 27, 9, 55, tzinfo=NY))
            st2 = dict(state, skipped=[])
            executor.on_entry(None, st2, deep, datetime(2026, 11, 27, 9, 55, tzinfo=NY))
            self.assertIn("only trades in the direction of QQQ", sent[-1])
        # the early exit for such a position: yesterday's close, not the open
        pos = {"side": "LONG", "qty": 4, "open_qty": 4, "trimmed": False, "opened_at": "2026-11-27T09:55:05-05:00", "qqq_ref": 633.95}
        t = datetime(2026, 11, 27, 10, 0, tzinfo=NY)
        self.assertFalse(contracts.qqq_open_exit_due(pos, 637.76, 645.72, t))     # below the open, above yesterday's close: kept
        self.assertTrue(contracts.qqq_open_exit_due(pos, 633.50, 645.72, t))
        self.assertTrue(contracts.qqq_open_exit_due(dict(pos, qqq_ref=None), 637.76, 645.72, t))   # a normal position: the open

    def test_no_entry_when_the_qqq_exit_is_due(self):
        """NO_ENTRY_WHEN_QQQ_EXIT_DUE (2026-11-28): from 10:30, no entry while QQQ is on the wrong side of its open."""
        from day_trading_setup.executor import executor
        NY = executor.NY
        sent = []
        lng = signals.parse("DTS: LONG AMZN @ 251.76 | stop 248.92 | target 257.44 (2R) | ORH breakout + 8 EMA retest | QQQ -0.50% (1h +0.43%) above VWAP | grade A inside")
        sht = signals.parse(lng.raw.replace("LONG", "SHORT").replace("stop 248.92", "stop 254.60").replace("target 257.44", "target 246.08"))
        up = signals.parse(lng.raw.replace("QQQ -0.50%", "QQQ +0.10%"))
        stop_here = lambda t: (_ for _ in ()).throw(RuntimeError("past the check"))
        with mock.patch.object(executor, "notify", sent.append), mock.patch.object(executor, "off_day_list", lambda *a: False), \
             mock.patch.object(contracts, "late_entry", stop_here):
            state = {"positions": {}, "closed": []}
            executor.on_entry(None, state, lng, datetime(2026, 11, 30, 10, 35, tzinfo=NY))
            self.assertIn("QQQ early exit would sell it", state["skipped"][-1]["reason"])
            for sig in (sht, up):                                                    # QQQ on the trade's side: carries on
                with self.assertRaisesRegex(RuntimeError, "past the check"):
                    executor.on_entry(None, state, sig, datetime(2026, 11, 30, 10, 35, tzinfo=NY))
            with mock.patch.object(settings, "NO_ENTRY_WHEN_QQQ_EXIT_DUE", False), self.assertRaisesRegex(RuntimeError, "past the check"):
                executor.on_entry(None, state, lng, datetime(2026, 11, 30, 10, 35, tzinfo=NY))

    def test_half_day_and_holiday_sweep(self):
        from day_trading_setup.executor import executor
        closed = []
        pos = {"ticker": "TSLA", "expiry": "2026-11-27", "opened_at": "2026-11-25T10:00:00-05:00"}
        def run(*when):
            state = {"positions": {"TSLA": pos}, "closed": []}
            with mock.patch.object(executor, "close_position", lambda b, s, p, t, why, a: closed.append(why)), \
                 mock.patch.object(contracts, "time_stop_due", lambda p, t: False), \
                 mock.patch.object(settings, "NO_OVERNIGHT_INTO_EARNINGS", False):
                executor.sweep(None, state, datetime(*when, tzinfo=executor.NY))
        run(2026, 11, 26, 15, 56)                 # Thanksgiving: market shut, nothing is sold
        run(2026, 11, 27, 12, 55)                 # half day, before its sweep
        run(2026, 11, 27, 13, 0)                  # half day, market shut
        self.assertEqual(closed, [])
        run(2026, 11, 27, 12, 56)
        self.assertEqual(closed, ["expiry day, closed by the 12:56 sweep"])
        self.assertTrue(contracts.in_session(datetime(2026, 11, 2, 15, 59)))       # a normal day is unchanged
        self.assertEqual(contracts.sweep_at(date(2026, 11, 2)), settings.SWEEP_AT)

    def test_friday_swing_filters(self):
        from day_trading_setup.executor import executor
        fri = datetime(2026, 10, 23, 12, 36, tzinfo=executor.NY)
        sent = []
        with mock.patch.object(executor, "notify", sent.append), mock.patch.object(settings, "ENTRY_CUTOFF", (16, 0)):
            state = {"positions": {}, "closed": []}
            sig = signals.parse("DTS: LONG MU @ 887.82 | stop 878.54 | target 906.38 (2R) | PMH breakout | grade A inside")
            executor.on_entry(None, state, sig, fri)
            self.assertIn("only A OUTSIDE / A+ OUTSIDE", sent[-1])
            state["positions"] = {x: {"opened_at": "2026-10-23T12:10:00-04:00"} for x in ("AAA", "BBB")}
            sig = signals.parse("DTS: LONG MU @ 887.82 | stop 878.54 | target 906.38 (2R) | PMH breakout | grade A+ OUTSIDE")
            executor.on_entry(None, state, sig, fri)
            self.assertIn("2 already open (max 2)", sent[-1])
        self.assertTrue(contracts.swing_entry(fri))
        self.assertFalse(contracts.swing_entry(datetime(2026, 10, 23, 11, 59, tzinfo=executor.NY)))

    def test_strong_close(self):
        sig = signals.parse("DTS: STRONG CLOSE SHORT GOOGL @ 316.90 | daily 8 EMA 320.10 | range 6% | 15:00 317.80")
        self.assertEqual((sig.kind, sig.ticker, sig.side, sig.price), ("STRONG", "GOOGL", "SHORT", 316.90))
        now = datetime(2026, 10, 26, 15, 56)
        strong = {"side": "SHORT", "t": "2026-10-26T15:55:02-04:00"}
        self.assertFalse(contracts.strong_close_holds({"side": "SHORT"}, strong, now))      # rule C off since 2026-11-04
        with mock.patch.object(settings, "STRONG_CLOSE_HOLD", True):
            self.assertTrue(contracts.strong_close_holds({"side": "SHORT"}, strong, now))
        self.assertFalse(contracts.strong_close_holds({"side": "LONG"}, strong, now))       # wrong direction
        self.assertFalse(contracts.strong_close_holds({"side": "SHORT"}, strong, datetime(2026, 10, 27, 15, 56)))  # stale
        self.assertFalse(contracts.strong_close_holds({"side": "SHORT"}, None, now))

    def test_sweep_sells_strong_close_before_2r(self):
        from day_trading_setup.executor import executor
        pos = {"ticker": "GOOGL", "side": "SHORT", "right": "P", "strike": 305.0, "expiry": "2026-10-30", "qty": 4,
               "open_qty": 4, "trimmed": False, "opened_at": "2026-10-26T12:01:00-04:00", "events": []}
        state = {"positions": {"GOOGL": dict(pos, events=[])}, "closed": [],
                 "strong_close": {"GOOGL": {"side": "SHORT", "t": "2026-10-26T15:55:02-04:00", "raw": "DTS: STRONG CLOSE SHORT GOOGL @ 316.90"}}}
        closed, sent = [], []
        with mock.patch.object(executor, "close_position", lambda b, st, p, t, why, raw: closed.append(p["ticker"])), \
             mock.patch.object(executor, "notify", sent.append):
            executor.sweep(None, state, datetime(2026, 10, 26, 15, 56, tzinfo=executor.NY))
            self.assertEqual(closed, ["GOOGL"])                                              # rule C off since 2026-11-04
            self.assertEqual(sent, [])
            with mock.patch.object(settings, "STRONG_CLOSE_HOLD", True):                     # the rule itself still works
                executor.sweep(None, state, datetime(2026, 10, 26, 15, 57, tzinfo=executor.NY))
            self.assertEqual(closed, ["GOOGL"])
            self.assertIn("HELD OVERNIGHT", sent[0])

    def test_journal_skips_hold_notes(self):
        from day_trading_setup.executor import journal
        t = {"ticker": "MRVL", "right": "C", "strike": 242.5, "expiry": "2026-10-30", "side": "LONG", "qty": 4,
             "opened_at": "2026-10-27T11:43:00-04:00", "tv_entry": 242.34, "tv_stop": 238.39, "tv_target": 250.24, "level": "ORH",
             "events": [{"t": "2026-10-27T11:43:00-04:00", "action": "BUY 4 @ 7.88", "qty": 4, "price": 7.88, "bid": 7.8, "ask": 7.96},
                        {"t": "2026-10-27T15:56:40-04:00", "action": "HOLD overnight (strong close)"},
                        {"t": "2026-10-28T10:05:00-04:00", "action": "SELL 4 @ 9.10 (TradingView TRAIL @ 246.1)", "qty": 4, "price": 9.10, "bid": 9.0, "ask": 9.2}]}
        buy, sells = journal._events(t, [])
        self.assertEqual((buy["qty"], len(sells), sells[0]["price"]), (4, 1, 9.10))

    def test_earnings_before_next_session(self):
        thu, fri = date(2026, 10, 29), date(2026, 10, 30)
        f = contracts.earnings_before_next_session
        self.assertTrue(f(thu, "AMC", thu))                       # reports after today's close
        self.assertTrue(f(thu, None, thu))                        # time unknown: treated as a risk
        self.assertFalse(f(thu, "BMO", thu))                      # already reported this morning
        self.assertTrue(f(fri, "BMO", thu))                       # before tomorrow's open
        self.assertFalse(f(fri, "AMC", thu))                      # after tomorrow's close: max hold sells first
        self.assertTrue(f(date(2026, 11, 2), "BMO", fri))         # Monday before the open, held over the weekend
        self.assertFalse(f(date(2026, 11, 3), None, thu))
        self.assertFalse(f(None, None, thu))

    def test_sweep_sells_runner_before_earnings(self):
        from day_trading_setup.executor import executor
        broker = mock.Mock()
        broker.earnings.return_value = {"AMZN": (date(2026, 10, 29), "AMC"), "AMD": (date(2026, 11, 3), None)}
        runner = {"side": "LONG", "right": "C", "strike": 255.0, "expiry": "2026-10-30", "qty": 4, "open_qty": 2,
                  "trimmed": True, "opened_at": "2026-10-29T10:30:00-04:00", "events": []}
        state = {"positions": {"AMZN": dict(runner, ticker="AMZN"), "AMD": dict(runner, ticker="AMD")}, "closed": []}
        closed = []
        with mock.patch.object(executor, "close_position", lambda b, st, p, t, why, raw: closed.append((p["ticker"], why))):
            executor.sweep(broker, state, datetime(2026, 10, 29, 15, 56, tzinfo=executor.NY))
        self.assertEqual([c[0] for c in closed], ["AMZN"])
        self.assertIn("earnings Thu 29 Oct AMC", closed[0][1])

    def test_true_price(self):
        from day_trading_setup.executor import reprice
        NY = reprice.NY
        m = lambda hh, mm: int(datetime(2026, 11, 9, hh, mm, tzinfo=NY).timestamp() * 1000)
        opt = {m(9, 58): (6.0, 6.0), m(10, 15): (7.45, 7.45)}                     # DELL 372.5C, Nov 9
        stock = {m(10, 10): (346.10, 347.43), m(10, 15): (350.80, 350.74), m(9, 58): (344.0, 344.2)}
        fill = datetime(2026, 11, 9, 10, 10, 24, tzinfo=NY)
        self.assertEqual(reprice.true_price(opt, fill)[0], 7.45)                     # old: unadjusted
        px, how = reprice.true_price(opt, fill, stock, 0.25)
        self.assertAlmostEqual(px, 6.28, places=2)                                   # 7.45 + 0.25 x (346.10 - 350.80)
        self.assertIn("delta", how)
        self.assertEqual(reprice.true_price({m(10, 10): (6.5, 6.6)}, fill, stock, 0.25)[0], 6.5)   # own minute: as traded
        # nothing within 5 minutes: nearest within 30, adjusted (DELL exit: last trade 15:38)
        opt2 = {m(15, 38): (5.97, 5.97)}
        st2 = {m(15, 38): (346.9, 346.9), m(15, 55): (346.76, 346.70)}
        exit_t = datetime(2026, 11, 9, 15, 55, 6, tzinfo=NY)
        self.assertIsNone(reprice.true_price(opt2, exit_t)[0])
        self.assertAlmostEqual(reprice.true_price(opt2, exit_t, st2, 0.25)[0], 5.94, places=2)
        # a put: delta negative, stock up -> option down
        self.assertAlmostEqual(reprice.true_price({m(10, 15): (3.0, 3.0)}, fill, stock, -0.3)[0], 4.41, places=2)
        self.assertAlmostEqual(reprice.true_price(opt, fill, stock, 0.25, at=346.38)[0], 6.34, places=2)   # alert price
        self.assertEqual(reprice.alert_price({"alert": "DTS: LRCX hit 2R target @ 221.12 - take partial"}), 221.12)
        self.assertIsNone(reprice.alert_price({"alert": None}))
        self.assertEqual(reprice.signed_delta({"right": "P", "delta": 0.3}), -0.3)
        self.assertEqual(reprice.signed_delta({"right": "C", "delta": -0.3}), 0.3)

    def test_close_exit_rule(self):
        from day_trading_setup.executor import journal
        self.assertTrue(journal.close_exit("2026-11-09T15:55:03-05:00"))
        self.assertFalse(journal.close_exit("2026-11-09T15:20:48-05:00"))
        self.assertFalse(journal.close_exit("2026-11-06T15:55:03-05:00"))       # before CLOSE_RULE_FROM
        buy_t, mid_t, eod_t = "2026-11-09T09:56:16-05:00", "2026-11-09T13:27:18-05:00", "2026-11-09T15:55:03-05:00"
        trade = {"ticker": "ANET", "events": [
            {"t": buy_t, "action": "BUY 7 @ 4.12", "qty": 7, "price": 4.12, "bid": 3.6, "ask": 4.65},
            {"t": mid_t, "action": "SELL 3 @ 6.0 (2R trim)", "qty": 3, "price": 6.0, "bid": 5.9, "ask": 6.1},
            {"t": eod_t, "action": "SELL 4 @ 6.83", "qty": 4, "price": 6.83, "bid": 6.5, "ask": 7.15}]}
        buy, sells = journal._events(trade, [])
        rp = lambda n: {f"ANET|{buy_t}": {"true": 4.99, "how": "x", "trade_minutes_30": 3},
                        f"ANET|{mid_t}": {"true": 6.20, "how": "x", "trade_minutes_30": 2},
                        f"ANET|{eod_t}": {"true": 5.99, "how": "x", "trade_minutes_30": n}}
        thin = journal._true_pnl(trade, buy, sells, rp(7))
        liquid = journal._true_pnl(trade, buy, sells, rp(20))
        # entry and the 13:27 sale at the last trade either way; the 15:55 sale at the bid when thin
        self.assertEqual([b[0] for b in thin[4]], [4.99, 6.20, 5.67])
        self.assertEqual([b[0] for b in liquid[4]], [4.99, 6.20, 5.99])
        self.assertAlmostEqual(thin[0], 3 * (6.20 - 4.99) * 100 + 4 * (5.67 - 4.99) * 100, places=2)

    def test_sessions(self):
        self.assertEqual(contracts.sessions_held(date(2026, 10, 15), date(2026, 10, 15)), 1)   # Thu
        self.assertEqual(contracts.sessions_held(date(2026, 10, 16), date(2026, 10, 19)), 2)   # Fri -> Mon

    def test_trim_result(self):
        import json, tempfile
        from pathlib import Path
        from day_trading_setup.executor import executor, reprice
        buy_t, trim_t = "2026-10-26T10:40:00-04:00", "2026-10-26T11:10:00-04:00"
        pos = {"ticker": "TSLA", "right": "C", "strike": 400.0, "expiry": "2026-10-30", "side": "LONG",
               "qty": 4, "open_qty": 2, "avg_cost": 5.70, "trimmed": True, "opened_at": buy_t, "level": "ORH",
               "tv_entry": 394.27, "tv_stop": 386.31, "tv_target": 410.2,
               "events": [{"t": buy_t, "action": "BUY 4 @ 5.7", "qty": 4, "price": 5.70, "bid": 5.6, "ask": 5.8},
                          {"t": trim_t, "action": "SELL 2 @ 8.1 (2R trim)", "qty": 2, "price": 8.10, "bid": 8.0, "ask": 8.2}]}
        state = {"positions": {"TSLA": pos}, "closed": []}
        rp = Path(tempfile.mkdtemp()) / "rp.json"
        rp.write_text(json.dumps({f"TSLA|{buy_t}": {"true": 5.50, "how": "1m open 10:40", "spread": 0.2}, f"TSLA|{trim_t}": {"true": 8.50, "how": "1m open 11:10", "spread": 0.2}}))
        sent = []
        with mock.patch.object(executor, "notify", sent.append), mock.patch.object(reprice, "reprice_path", lambda m: rp):
            self.assertFalse(executor.confirm_results(state, datetime(2026, 10, 26, 11, 20, tzinfo=executor.NY)))   # not 16 min yet
            self.assertTrue(executor.confirm_results(state, datetime(2026, 10, 26, 11, 27, tzinfo=executor.NY)))
            executor.confirm_results(state, datetime(2026, 10, 26, 11, 40, tzinfo=executor.NY))                    # sent once
        self.assertEqual(len(sent), 1)
        self.assertIn("+$600", sent[0])            # 2 x (8.50 - 5.50) x 100
        self.assertIn("2 still open", sent[0])
        self.assertEqual(pos["events"][1]["pnl_confirmed"], 600)

    def _runner(self, open_qty):
        return {"ticker": "TSLA", "right": "C", "strike": 400.0, "expiry": "2026-11-06", "side": "LONG",
                "qty": open_qty * 2, "open_qty": open_qty, "avg_cost": 5.70, "trimmed": True,
                "opened_at": "2026-11-04T10:40:00-05:00", "events": []}

    def _run_target(self, state, msg):
        from day_trading_setup.executor import executor
        broker, sent = mock.Mock(), []
        broker.fill.side_effect = lambda side, pos, qty: (qty, 12.0, "filled at mid")
        broker.last_quote = (11.9, 12.1, "TastyTrade")
        with mock.patch.object(executor, "notify", sent.append):
            executor.on_target(broker, state, signals.parse(msg), datetime(2026, 11, 4, 11, 30, tzinfo=executor.NY))
        return sent

    def test_runner_target_sells_half_of_the_runner(self):
        state = {"positions": {"TSLA": self._runner(3)}, "closed": []}
        sent = self._run_target(state, "DTS: TSLA hit 4R target @ 410.0 - take partial, the rest rides the trail")
        pos = state["positions"]["TSLA"]
        self.assertEqual((pos["open_qty"], pos["runner_trimmed"]), (2, True))
        self.assertIn("4R TARGET HIT", sent[0])
        self.assertIn("(4R trim)", pos["events"][-1]["action"])
        self.assertFalse(contracts.close_before_overnight(pos))                         # still a runner past 2R
        self._run_target(state, "DTS: TSLA hit 4R target @ 410.0 - take partial, the rest rides the trail")
        self.assertEqual(pos["open_qty"], 2)                                            # once only

    def test_runner_target_sells_a_single_contract(self):
        state = {"positions": {"TSLA": self._runner(1)}, "closed": []}
        sent = self._run_target(state, "DTS: TSLA hit 4R target @ 410.0 - take partial, the rest rides the trail")
        self.assertEqual(state["positions"], {})
        self.assertEqual(state["closed"][0]["close_reason"], "4R (fully sold)")
        self.assertIn("CLOSED", sent[-1])

    def test_runner_target_before_2r_is_ignored(self):
        pos = dict(self._runner(4), trimmed=False)
        state = {"positions": {"TSLA": pos}, "closed": []}
        self._run_target(state, "DTS: TSLA hit 4R target @ 410.0 - take partial, the rest rides the trail")
        self.assertEqual(pos["open_qty"], 4)

    def test_journal_labels_runner_sales(self):
        from day_trading_setup.executor import journal
        self.assertEqual(journal._exit_kind({"action": "SELL 2 @ 8.1 (2R trim)", "why": "2R trim"}), "2R")
        self.assertEqual(journal._exit_kind({"action": "SELL 1 @ 12.0 (4R trim)", "why": "4R trim"}), "4R")
        self.assertEqual(journal._exit_kind({"action": "SELL 1 @ 9.0 (TradingView WEAK @ 410.1)"}), "WEAK")


class BackfillTest(unittest.TestCase):
    """Missed-alert backup (Nov 11: AVGO's 14:20 STOP webhook failed on TradingView's side)."""
    NY = __import__("zoneinfo").ZoneInfo("America/New_York")
    STOP = "DTS: EXIT LONG AVGO (STOP) @ 349.52"
    TRAIL = "DTS: EXIT LONG DELL (TRAIL) @ 376.54"

    def _fire(self, fid, utc, msg, ok=True):
        return {"fire_id": fid, "fire_time": utc, "message": msg, "webhook_ok": ok,
                "webhook_error": None if ok else "something went wrong"}

    def _now(self, h, m, s=0):
        return datetime(2026, 11, 11, h, m, s, tzinfo=self.NY)

    def test_failed_stop_is_found_and_delivered_one_is_not(self):
        from day_trading_setup.executor import backfill
        fires = [self._fire(1, "2026-11-11T19:20:00Z", self.STOP, ok=False),
                 self._fire(2, "2026-11-11T19:45:01Z", self.TRAIL)]
        inbox = [{"received": "2026-11-11T14:45:09.756-05:00", "message": self.TRAIL}]
        got = backfill.missed(fires, inbox, {}, self._now(14, 46))
        self.assertEqual([f["fire_id"] for f in got], [1])
        self.assertEqual(backfill.decide(got[0], self._now(14, 46)), "send")

    def test_not_resent_twice_and_waits_for_normal_delivery(self):
        from day_trading_setup.executor import backfill
        fires = [self._fire(1, "2026-11-11T19:20:00Z", self.STOP, ok=False)]
        self.assertEqual(backfill.missed(fires, [], {"1": {}}, self._now(14, 30)), [])
        self.assertEqual(backfill.missed(fires, [], {}, self._now(14, 20, 30)), [])     # under a minute old
        self.assertEqual(len(backfill.missed(fires, [], {}, self._now(14, 21, 5))), 1)

    def test_same_message_twice_needs_two_inbox_lines(self):
        from day_trading_setup.executor import backfill
        msg = "DTS: A+ OUTSIDE LONG AVGO @ 354.80 - watch for the 8 EMA retest entry"
        fires = [self._fire(1, "2026-11-11T17:00:00Z", msg), self._fire(2, "2026-11-11T17:05:00Z", msg)]
        inbox = [{"received": "2026-11-11T12:00:01-05:00", "message": msg}]
        got = backfill.missed(fires, inbox, {}, self._now(12, 30))
        self.assertEqual([f["fire_id"] for f in got], [2])
        self.assertEqual(backfill.decide(got[0], self._now(12, 30)), "ignore")          # watch alerts aren't re-sent

    def test_other_days_and_non_dts_fires_are_skipped(self):
        from day_trading_setup.executor import backfill
        fires = [self._fire(1, "2026-11-10T19:20:00Z", self.STOP, ok=False),
                 self._fire(2, "2026-11-11T19:20:00Z", "DTS Screener updated", ok=False)]
        self.assertEqual(backfill.missed(fires, [], {}, self._now(14, 30)), [])

    def test_entries_only_while_fresh(self):
        from day_trading_setup.executor import backfill
        e = self._fire(1, "2026-11-11T16:25:31Z", "DTS: LONG AVGO @ 353.38 | stop 349.52 | target 361.10 (2R) | "
                       "PMH breakout + 8 EMA retest | grade A OUTSIDE", ok=False)
        self.assertEqual(backfill.decide(e, self._now(11, 28)), "send")
        self.assertEqual(backfill.decide(e, self._now(11, 40)), "late entry")
        self.assertEqual(backfill.decide(self._fire(2, "2026-11-11T16:25:31Z", self.STOP), self._now(15, 50)), "send")

    def test_active_hours(self):
        from day_trading_setup.executor import backfill
        self.assertTrue(backfill.active(self._now(15, 58)))
        self.assertFalse(backfill.active(self._now(16, 15)))
        self.assertFalse(backfill.active(self._now(9, 20)))
        self.assertFalse(backfill.active(datetime(2026, 11, 14, 11, 0, tzinfo=self.NY)))  # Saturday


class TimeLimitTestTest(unittest.TestCase):
    """2-hour time limit, report only (user decision 2026-11-11)."""
    def _trade(self, opened, sells=()):
        ev = [{"t": opened, "action": "BUY 4 @ 2.0", "qty": 4, "price": 2.0}]
        ev += [{"t": t, "action": f"SELL {q} @ 3.0 ({why})", "qty": q, "price": 3.0} for t, q, why in sells]
        return {"ticker": "MU", "opened_at": opened, "qty": 4, "events": ev}

    def test_checkpoint_two_hours_after_entry(self):
        from day_trading_setup.executor import reprice
        with mock.patch.object(settings, "TIME_LIMIT_TEST_FROM", "2026-11-11"):
            ev = reprice.time_limit_check(self._trade("2026-11-12T11:20:58-05:00", [("2026-11-12T15:55:04-05:00", 4, "TradingView EOD")]))
            self.assertEqual(ev["t"][11:16], "13:20")
            self.assertEqual(reprice.event_key(self._trade("2026-11-12T11:20:58-05:00"), ev), "MU|2026-11-12T11:20:58-05:00|TL120")

    def test_no_checkpoint_after_a_sale_late_entry_or_before_the_test(self):
        from day_trading_setup.executor import reprice
        with mock.patch.object(settings, "TIME_LIMIT_TEST_FROM", "2026-11-12"):
            self.assertIsNone(reprice.time_limit_check(self._trade("2026-11-12T10:00:00-05:00", [("2026-11-12T11:00:00-05:00", 2, "2R trim")])))
            self.assertIsNone(reprice.time_limit_check(self._trade("2026-11-12T14:00:00-05:00")))   # 16:00 is past the close rule
            self.assertIsNone(reprice.time_limit_check(self._trade("2026-11-11T10:00:00-05:00")))   # before the test starts

    def test_summary_sells_only_losing_trades(self):
        from day_trading_setup.executor import journal
        trades = [{"ticker": "META", "pnl_real": -360.0, "time_limit": {"sell": True, "pnl_if_sold": -292.0}},
                  {"ticker": "AMD", "pnl_real": -110.0, "time_limit": {"sell": False, "pnl_if_sold": 690.0}},
                  {"ticker": "MSFT", "pnl_real": 2546.0, "time_limit": None}]
        s = journal.time_limit_summary(trades)
        self.assertEqual((s["checked"], s["would_sell"], s["pnl_real"], s["pnl_with_rule"], s["change"]), (2, 1, 2076.0, 2144.0, 68.0))
        self.assertIsNone(journal.time_limit_summary([trades[2]]))


class PremiumLadderTestTest(unittest.TestCase):
    """Premium ladder, report only (user decision 2026-11-12: trim in steps as the option rises)."""
    M = staticmethod(lambda hm: int(__import__("datetime").datetime.fromisoformat(f"2026-11-12T{hm}:00-05:00").timestamp() * 1000))
    STEPS = [(25, 0.25), (50, 0.50), (75, 0.75), (100, 1.0)]

    def test_steps_fill_at_their_price_and_the_stock_exit_sells_the_rest(self):
        from day_trading_setup.executor import journal
        m = self.M
        bars = [[m("11:05"), 2.0, 2.1], [m("11:11"), 2.4, 2.55], [m("11:20"), 3.05, 2.9], [m("13:00"), 2.0, 1.9]]
        pnl, hits = journal.ladder_pnl(self.STEPS, 2.0, 8, m("11:05"), [(m("15:55"), 8, 1.5)], bars)
        # +25% (2.50) at 11:11: 2; +50% (3.00) at 11:20 opened above: 2 @ 3.05; rest 4 at the close 1.50
        self.assertEqual([(g, q, p) for g, _, q, p in hits], [(25, 2, 2.5), (50, 2, 3.05)])
        self.assertEqual(pnl, 2 * 50 + 2 * 105 + 4 * -50)

    def test_trim_shares_and_last_step_sells_all(self):
        from day_trading_setup.executor import journal
        m = self.M
        bars = [[m("10:01"), 1.0, 2.1]]
        pnl, hits = journal.ladder_pnl(self.STEPS, 1.0, 4, m("10:00"), [(m("15:55"), 4, 0.5)], bars)
        self.assertEqual([q for _, _, q, _ in hits], [1, 1, 1, 1])        # every step in one minute
        # a 2R sale of half before any step: the stock exits sell the same share of the rest
        pnl, hits = journal.ladder_pnl(self.STEPS, 1.0, 4, m("10:00"), [(m("10:30"), 2, 1.2), (m("15:55"), 2, 0.5)], [[m("10:45"), 1.3, 1.3]])
        self.assertEqual((pnl, hits[0][2]), (2 * 20 + 1 * 30 + 1 * -50, 1))

    def test_bars_kept_for_closed_trades_and_original_exit_used(self):
        from day_trading_setup.executor import reprice
        orig = {"t": "2026-11-12T15:55:04-05:00", "action": "SELL 4 @ 9.0 (TradingView EOD)", "qty": 4, "price": 9.0}
        t = {"ticker": "AMD", "opened_at": "2026-11-12T11:00:50-05:00", "closed_at": "2026-11-12T11:17:00-05:00", "qty": 4,
             "events": [{"t": "2026-11-12T11:00:50-05:00", "action": "BUY 4 @ 5.47", "qty": 4, "price": 5.47},
                        {"t": "2026-11-12T11:17:00-05:00", "action": "SELL 4 @ 15.8 (PEAK)", "qty": 4, "price": 15.8, "replaced_event": orig}]}
        ev = reprice.bars_check(t)
        self.assertEqual((ev["t"], reprice.event_key(t, ev)), (orig["t"], "AMD|2026-11-12T11:00:50-05:00|BARS"))
        self.assertEqual(reprice.as_traded_exits(t), [orig])
        self.assertIsNone(reprice.bars_check(dict(t, closed_at=None)))


class QuoteEstimateTest(unittest.TestCase):
    """Delayed quotes repriced to the alert's live stock price (user decision 2026-11-14)."""
    T = datetime(2026, 11, 13, 9, 51, tzinfo=__import__("zoneinfo").ZoneInfo("America/New_York"))

    def test_amd_nov13_put(self):
        from day_trading_setup.executor import estimate
        # delayed quote 1.91/2.04 with AMD at 562.61; alert at 548.22; traded 7.28
        px = estimate.price_now(1.975, 562.61, 548.22, 547.5, date(2026, 11, 13), "P", self.T)
        self.assertTrue(6.0 < px < 7.3, px)
        q = estimate.shift_quote({"bid": 1.91, "ask": 2.04, "delta": -0.24, "volume": 500}, 562.61, 548.22, 547.5,
                                 date(2026, 11, 13), "P", self.T)
        self.assertTrue(q["adjusted"])
        self.assertAlmostEqual(q["ask"] - q["bid"], 0.13, delta=0.011)      # the delayed spread is kept (to rounding)
        self.assertEqual((q["delayed_bid"], q["volume"]), (1.91, 500))
        self.assertLess(q["delta"], -0.24)                                  # deeper in the money now
        self.assertEqual(int(3500 // (q["ask"] * 100)), 5)                  # 5 contracts, not 17

    def test_no_move_no_change_and_missing_data(self):
        from day_trading_setup.executor import estimate
        px = estimate.price_now(2.0, 300.0, 300.0, 300.0, date(2026, 11, 13), "C", self.T)
        self.assertAlmostEqual(px, 2.0, delta=0.06)                          # only 15 min of time decay
        self.assertIsNone(estimate.price_now(2.0, None, 300.0, 300.0, date(2026, 11, 13), "C", self.T))
        self.assertFalse(estimate.shift_quote({"bid": None, "ask": 2.0}, 300, 301, 300, date(2026, 11, 13), "C", self.T)["adjusted"])


class TimeLimitLiveTest(unittest.TestCase):
    """The 2-hour time limit sells from TIME_LIMIT_LIVE_FROM (user decision 2026-11-14)."""
    NY = __import__("zoneinfo").ZoneInfo("America/New_York")

    def _run(self, quote, opened="2026-11-16T10:00:00-05:00", at=(12, 0), trimmed=False, msg="DTS: CHECK LONG MU @ 795.10 | 2h without 2R | entry 790.00"):
        from day_trading_setup.executor import executor
        pos = {"ticker": "MU", "side": "LONG", "right": "C", "strike": 800.0, "expiry": "2026-11-20", "qty": 4,
               "open_qty": 2 if trimmed else 4, "trimmed": trimmed, "avg_cost": 6.0, "est_entry": 6.0,
               "opened_at": opened, "events": [], "delta": 0.4}
        state = {"positions": {"MU": pos}, "closed": []}
        broker = types.SimpleNamespace(quote=lambda p: (quote - 0.1, quote + 0.1, "TastyTrade"), adjust=lambda p, q: q)
        closed, sent = [], []
        with mock.patch.object(executor, "close_position", lambda b, st, p, t, why, raw: closed.append(why)), \
             mock.patch.object(executor, "notify", sent.append):
            executor.on_check(broker, state, signals.parse(msg), datetime(2026, 11, 16, *at, tzinfo=self.NY))
        return closed, pos["events"]

    def test_sells_below_entry_and_holds_above(self):
        closed, _ = self._run(5.5)
        self.assertEqual(len(closed), 1)
        self.assertIn("time limit", closed[0])
        closed, ev = self._run(6.5)
        self.assertEqual(closed, [])
        self.assertIn("HOLD at the 2-hour check", ev[0]["action"])

    def test_ignored_after_2r_too_early_wrong_side_or_before_go_live(self):
        self.assertEqual(self._run(5.5, trimmed=True), ([], []))
        self.assertEqual(self._run(5.5, at=(11, 30)), ([], []))
        self.assertEqual(self._run(5.5, msg="DTS: CHECK SHORT MU @ 795.10 | 2h without 2R | entry 790.00"), ([], []))
        with mock.patch.object(settings, "TIME_LIMIT_LIVE_FROM", "2026-11-17"):
            self.assertEqual(self._run(5.5), ([], []))
        self.assertEqual(self._run(5.5, at=(11, 56))[0][:1] and "sold", "sold")   # 4 minutes early (entry inside its bar) still counts

    def test_parse_backfill_and_report_test_stops(self):
        from day_trading_setup.executor import backfill, reprice
        sig = signals.parse("DTS: CHECK SHORT AMD @ 548.22 | 2h without 2R | entry 551.00")
        self.assertEqual((sig.kind, sig.ticker, sig.side, sig.price), ("CHECK", "AMD", "SHORT", 548.22))
        self.assertIn("CHECK", backfill.ACTIONABLE)
        t = {"ticker": "MU", "opened_at": "2026-11-16T10:00:00-05:00", "qty": 4,
             "events": [{"t": "2026-11-16T10:00:00-05:00", "action": "BUY 4 @ 2.0", "qty": 4, "price": 2.0}]}
        self.assertIsNone(reprice.time_limit_check(t))


class QqqOpenExitTest(unittest.TestCase):
    """Early exit when QQQ closes back through its 09:30 open (user decision 2026-11-24)."""
    NY = __import__("zoneinfo").ZoneInfo("America/New_York")

    def test_sells_only_positions_it_goes_against(self):
        from day_trading_setup.executor import executor
        base = {"right": "C", "strike": 100.0, "expiry": "2026-11-27", "qty": 4, "open_qty": 4, "trimmed": False,
                "avg_cost": 3.0, "opened_at": "2026-11-24T11:00:00-05:00", "events": []}
        state = {"positions": {"MU": {**base, "ticker": "MU", "side": "LONG"},
                               "AMD": {**base, "ticker": "AMD", "side": "SHORT", "right": "P"},
                               "NVDA": {**base, "ticker": "NVDA", "side": "LONG", "trimmed": True, "open_qty": 2}},
                 "closed": []}
        closed = []
        with mock.patch.object(executor, "close_position", lambda b, st, p, t, why, raw: closed.append((p["ticker"], why))):
            executor.on_qqq_bar(None, state, signals.parse("DTS: QQQ 5M CLOSE 639.50 | open 640.05"),
                                datetime(2026, 11, 24, 12, 30, tzinfo=self.NY))
        self.assertEqual([c[0] for c in closed], ["MU"])            # the long; not the short, not the 2R runner
        self.assertIn("early exit: QQQ closed back below its 09:30 open", closed[0][1])
        self.assertIn("QQQ 639.50 vs its open 640.05", closed[0][1])
        self.assertEqual(executor.qqq_exits_due(state, signals.parse("DTS: QQQ 5M CLOSE 639.50 | open 640.05"),
                                                datetime(2026, 11, 24, 16, 5, tzinfo=self.NY)), [])   # after the close
        from day_trading_setup.executor import journal, backfill
        self.assertEqual(journal._exit_kind({"why": closed[0][1], "action": "SELL 4 @ 2.0"}), "QQQ EXIT")
        self.assertNotIn("QQQBAR", backfill.ACTIONABLE)             # a stale bar is never re-sent


class OpeningEntryTest(unittest.TestCase):
    """09:35 opening entry, DTS Entry v22.0 (user decision 2026-11-26)."""
    NY = __import__("zoneinfo").ZoneInfo("America/New_York")
    MSG = ("DTS: SHORT AAPL @ 350.49 | stop 353.84 | target 343.79 (2R) | OPEN breakout + first 5-min bar, "
           "QQQ gap -0.45% | QQQ -0.30% (1h -0.50%) above VWAP | grade B inside")

    def _entry(self, state, msg=None, at=(9, 35)):
        from day_trading_setup.executor import executor
        reasons = []
        with mock.patch.object(executor, "notify", lambda *_: None), \
             mock.patch.object(executor, "off_day_list", lambda *_: False), \
             mock.patch.object(executor, "choose_contract", lambda *a, **k: (None, "reached contract choice")), \
             mock.patch.object(executor, "friday_fallback", lambda *a, **k: None):
            executor.on_entry(None, state, signals.parse(msg or self.MSG), datetime(2026, 11, 30, *at, 2, tzinfo=self.NY))
        return state["skipped"][-1]["reason"] if state.get("skipped") else None

    def test_parse_and_qqq_rule_exemption(self):
        sig = signals.parse(self.MSG)
        self.assertEqual((sig.kind, sig.side, sig.level, sig.stop), ("ENTRY", "SHORT", "OPEN", 353.84))
        self.assertTrue(contracts.is_opening(sig))
        # QQQ above its VWAP would block an ordinary short before 10:30; the opening entry goes on
        self.assertEqual(self._entry({"positions": {}, "closed": []}), "reached contract choice")
        normal = self.MSG.replace("OPEN breakout + first 5-min bar, QQQ gap -0.45%", "ORL breakout + 8 EMA retest")
        self.assertIn("only trades in the direction of QQQ", self._entry({"positions": {}, "closed": []}, normal))

    def test_cap_of_six_a_day(self):
        today = [{"ticker": f"T{i}", "opening": True, "opened_at": "2026-11-30T09:35:05-05:00"} for i in range(6)]
        self.assertIn("opening entries: 6 already taken today", self._entry({"positions": {}, "closed": today}))
        older = [{**p, "opened_at": "2026-11-27T09:35:05-05:00"} for p in today]
        self.assertEqual(self._entry({"positions": {}, "closed": older[:6] + today[:5]}), "reached contract choice")

    def test_never_sold_by_the_early_exit(self):
        t = datetime(2026, 11, 30, 10, 0, tzinfo=self.NY)
        pos = {"side": "SHORT", "opened_at": "2026-11-30T09:35:05-05:00", "qty": 4, "open_qty": 4, "trimmed": False}
        self.assertTrue(contracts.qqq_open_exit_due(pos, 641.0, 640.0, t))
        self.assertFalse(contracts.qqq_open_exit_due({**pos, "opening": True}, 641.0, 640.0, t))
        self.assertEqual(settings.OPENING_MAX, 6)


class DayRulesTest(unittest.TestCase):
    """D4 day rules (user decision 2026-11-26): protect a green day at +3u -> +1u, loss limit -4u."""
    NY = __import__("zoneinfo").ZoneInfo("America/New_York")

    def _pos(self, tk, side="LONG", e=100.0, stop=99.0, qty=4, open_qty=4, size=0.025, sells=()):
        return {"ticker": tk, "side": side, "tv_entry": e, "tv_stop": stop, "qty": qty, "open_qty": open_qty,
                "size_fraction": size, "opened_at": "2026-11-30T10:00:00-05:00",
                "events": [{"action": f"SELL {q} @ 1.0", "qty": q, "stock_px": px} for q, px in sells]}

    def test_units(self):
        at = "2026-11-30T10:30:00-05:00"
        marks = {"A": {"t": at, "price": 102.0}, "B": {"t": at, "price": 99.5}}
        a = self._pos("A")                                   # +2R open, 2.5% size -> +2u
        b = self._pos("B", side="SHORT", e=100.0, stop=101.0, size=0.05)   # short +0.5R at 5% -> +1u
        self.assertAlmostEqual(contracts.trade_units(a, marks), 2.0)
        self.assertAlmostEqual(contracts.trade_units(b, marks), 1.0)
        c = self._pos("A", open_qty=2, sells=[(2, 102.0)])    # half sold at 2R, rest marked at 102 -> +2u
        self.assertAlmostEqual(contracts.trade_units(c, marks), 2.0)
        state = {"positions": {"A": a}, "closed": [self._pos("C", open_qty=0, sells=[(4, 99.0)])]}   # stopped: -1u
        self.assertAlmostEqual(contracts.day_units(state, date(2026, 11, 30), marks), 1.0)
        # a sale without a recorded stock price falls back to its TradingView alert's price
        d = self._pos("D", open_qty=0); d["events"] = [{"action": "SELL 4 @ 1.0", "qty": 4, "alert": "DTS: EXIT LONG D (STOP) @ 99.00"}]
        self.assertAlmostEqual(contracts.trade_units(d, {}), -1.0)
        # v27 marks come only on 0.25R moves: one left by an earlier trade in the stock is not used
        old = {"A": {"t": "2026-11-30T09:50:00-05:00", "price": 95.0}}
        self.assertAlmostEqual(contracts.trade_units(a, old), 0.0)                   # valued at its entry
        self.assertIsNone(contracts.mark_price(a, old))
        self.assertEqual(contracts.mark_price(a, marks), 102.0)

    def test_triggers(self):
        day = date(2026, 11, 30); st = {}
        self.assertIsNone(contracts.day_rule_due(st, day, 2.5))
        self.assertIsNone(contracts.day_rule_due(st, day, 3.2))          # armed
        self.assertIsNone(contracts.day_rule_due(st, day, 1.4))
        self.assertIn("green day protected", contracts.day_rule_due(st, day, 0.9))
        st = {}
        self.assertIsNone(contracts.day_rule_due(st, day, 0.5))            # never armed: +0.5 is fine
        self.assertIsNone(contracts.day_rule_due(st, day, -3.9))
        self.assertIn("daily loss limit", contracts.day_rule_due(st, day, -4.0))
        self.assertIsNone(contracts.day_rule_due(st, date(2026, 12, 1), 0.9))   # new day: not armed
        # protect only from 13:00 (2026-11-28): armed in the morning, a dip to +0.9u before 13:00 doesn't sell
        st2 = {}; d2 = date(2026, 12, 2)
        self.assertEqual(settings.DAY_PROTECT_FROM, (13, 0))
        self.assertIsNone(contracts.day_rule_due(st2, d2, 3.5, (10, 0)))           # armed
        self.assertIsNone(contracts.day_rule_due(st2, d2, 0.9, (10, 30)))          # morning dip: kept
        self.assertIsNone(contracts.day_rule_due(st2, d2, 0.9, (12, 55)))
        self.assertIn("daily loss limit", contracts.day_rule_due(dict(st2), d2, -4.2, (10, 35)))   # the loss limit works all day
        self.assertIn("green day protected", contracts.day_rule_due(st2, d2, 0.8, (13, 0)))
        with mock.patch.object(settings, "DAY_RULES", False):
            self.assertIsNone(contracts.day_rule_due({}, day, -9))

    def test_protect_sale_keeps_runners(self):
        """DAY_PROTECT_KEEP_RUNNERS (2026-11-28): the protect sale sells only positions not yet at 2R;
        the loss limit sells everything."""
        from day_trading_setup.executor import executor
        t = datetime(2026, 12, 2, 13, 0, tzinfo=self.NY)
        for why, sold_expected in (("green day protected: up +3.5u at best, back to +0.8u", ["B"]),
                                   ("daily loss limit: the day reached -4.2u (limit -4u)", ["A", "B"])):
            runner = dict(self._pos("A", open_qty=2, sells=[(2, 102.0)]), trimmed=True, events=[])
            fresh = dict(self._pos("B"), trimmed=False, events=[])
            state = {"positions": {"A": runner, "B": fresh}, "closed": [], "day_stop": {"date": "2026-12-02", "why": why}}
            sold, sent = [], []
            with mock.patch.object(executor, "close_position", lambda b, s, p, t, w, a: sold.append(p["ticker"])), \
                 mock.patch.object(executor, "notify", sent.append):
                executor.on_day_rule(None, state, signals.Signal("MARK", ticker="QQQ", price=1.0, raw="x"), t)
            self.assertEqual(sold, sold_expected, why)
            if len(sold_expected) == 1:
                self.assertIn("1 runner(s) past 2R kept", sent[-1])
                self.assertIn("runner past 2R kept", runner["events"][-1]["action"])

    def test_mark_sells_everything_and_blocks_entries(self):
        from day_trading_setup.executor import executor
        t = datetime(2026, 11, 30, 11, 0, tzinfo=self.NY)
        state = {"positions": {"A": self._pos("A"), "B": self._pos("B")}, "closed": [], "processed": {}}
        closed, sent = [], []
        class FakeBroker:
            def __init__(self): self.stock_now = None
            def connect(self): pass
            def close(self): pass
        def fake_close(b, st, p, tt, why, raw):
            closed.append((p["ticker"], why)); del st["positions"][p["ticker"]]
        with mock.patch.object(executor, "load_state", lambda: state), mock.patch.object(executor, "save_state", lambda s: None), \
             mock.patch.object(executor, "Broker", FakeBroker), mock.patch.object(executor, "close_position", fake_close), \
             mock.patch.object(executor, "notify", sent.append), mock.patch.object(executor.settings, "LOCK_FILE", "/tmp/dts_test.lock"), \
             mock.patch.object(executor.logging, "basicConfig", lambda **k: None), mock.patch.object(executor.logging, "FileHandler", lambda *a, **k: None):
            state.setdefault("marks", {})["B"] = {"t": t.isoformat(), "price": 98.0}          # B -2u
            executor.main(["--message", "DTS: MARK LONG A @ 98.10", "--received", t.isoformat()])   # A -1.9u -> -3.9u: nothing
            self.assertEqual(closed, [])
            executor.main(["--message", "DTS: MARK LONG A @ 97.90", "--received", (t.replace(minute=5)).isoformat()])  # -4.1u
        self.assertEqual(sorted(c[0] for c in closed), ["A", "B"])
        self.assertIn("daily loss limit", closed[0][1])
        self.assertIn("DAY STOP", sent[-1])
        self.assertIn("daily loss limit", contracts.day_stopped(state, t.date()))
        reasons = []
        with mock.patch.object(executor, "notify", lambda *_: None), mock.patch.object(executor, "off_day_list", lambda *_: False):
            executor.on_entry(None, state, signals.parse("DTS: LONG MU @ 700.00 | stop 690.00 | target 720.00 (2R) | ORH breakout + 8 EMA retest | grade A inside"), t.replace(minute=30))
        self.assertIn("day rules: no new entries today", state["skipped"][-1]["reason"])
        self.assertEqual(signals.parse("DTS: MARK SHORT NVDA @ 1,180.12").price, 1180.12)
        from day_trading_setup.executor import journal
        self.assertEqual(journal._exit_kind({"why": closed[0][1], "action": "SELL 4 @ 1.0"}), "DAY STOP")

    def test_loss_limit_with_nothing_open(self):
        """All trades closed at -4.5u: no MARK alerts come, so the 5-minute QQQ bar must stop the day."""
        from day_trading_setup.executor import executor
        t = datetime(2026, 11, 30, 11, 0, tzinfo=self.NY)
        lost = [self._pos(f"L{i}", open_qty=0, sells=[(4, 99.0)]) for i in range(4)] + [self._pos("L4", open_qty=0, sells=[(4, 99.5)])]
        state = {"positions": {}, "closed": lost, "processed": {}}
        self.assertAlmostEqual(contracts.day_units(state, t.date()), -4.5)
        class FakeBroker:
            def __init__(self): self.stock_now = None
            def connect(self): pass
            def close(self): pass
        sent = []
        with mock.patch.object(executor, "load_state", lambda: state), mock.patch.object(executor, "save_state", lambda s: None), \
             mock.patch.object(executor, "Broker", FakeBroker), mock.patch.object(executor, "notify", sent.append), \
             mock.patch.object(executor.settings, "LOCK_FILE", "/tmp/dts_test.lock"), \
             mock.patch.object(executor.logging, "basicConfig", lambda **k: None), mock.patch.object(executor.logging, "FileHandler", lambda *a, **k: None):
            executor.main(["--message", "DTS: QQQ 5M CLOSE 641.00 | open 640.00", "--received", t.isoformat()])
        self.assertIn("daily loss limit", contracts.day_stopped(state, t.date()))
        self.assertIn("DAY STOP", sent[-1])
        self.assertEqual(state["qqq_close"]["2026-11-30"]["close"], 641.00)        # QQQ_UP_ON_DAY: the day's latest QQQ close is kept

    def test_entry_checks_the_day_first(self):
        from day_trading_setup.executor import executor
        t = datetime(2026, 11, 30, 11, 2, tzinfo=self.NY)
        lost = [self._pos(f"L{i}", open_qty=0, sells=[(4, 99.0)]) for i in range(4)] + [self._pos("L4", open_qty=0, sells=[(4, 99.0)])]
        state = {"positions": {}, "closed": lost}
        with mock.patch.object(executor, "notify", lambda *_: None), mock.patch.object(executor, "off_day_list", lambda *_: False):
            executor.on_entry(None, state, signals.parse("DTS: LONG MU @ 700.00 | stop 690.00 | target 720.00 (2R) | ORH breakout + 8 EMA retest | grade A inside"), t)
        self.assertIn("day rules: no new entries today (daily loss limit", state["skipped"][-1]["reason"])


class FileAccessTest(unittest.TestCase):
    """fsio (2026-11-28): iCloud Drive can refuse a file briefly with EDEADLK; retried, not lost."""

    def test_retry_on_edeadlk(self):
        import errno
        from day_trading_setup.executor import fsio
        calls = []
        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise OSError(errno.EDEADLK, "Resource deadlock avoided")
            return "ok"
        with mock.patch.object(fsio, "PAUSE", 0):
            self.assertEqual(fsio.retry(flaky), "ok")
            self.assertEqual(len(calls), 3)
            with self.assertRaises(FileNotFoundError):                     # other errors are not retried
                fsio.retry(open, "/nonexistent/dir/x")

    def test_lock_outside_icloud(self):
        from day_trading_setup.executor import fsio
        self.assertNotIn("Documents", str(settings.LOCK_FILE))
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as d, mock.patch.object(settings, "LOCK_FILE", pathlib.Path(d) / "sub" / "x.lock"):
            with fsio.open_lock() as f:
                self.assertEqual(f.mode, "a")
            with fsio.open_lock() as f:                                     # reopened, never truncated
                pass


class FadeReportTest(unittest.TestCase):
    """Fade test, report only (user decision 2026-11-26)."""

    def test_finds_a_fade_and_prices_it(self):
        from day_trading_setup.executor import fade_report as fr
        d = "2026-11-30"; prev = "2026-11-27"
        rows = [(prev, f"{9 + (30 + 5 * i) // 60:02d}:{(30 + 5 * i) % 60:02d}", 100, 100.2, 99.8, 100, 1000) for i in range(40)]
        hm = lambda i: f"{9 + (30 + 5 * i) // 60:02d}:{(30 + 5 * i) % 60:02d}"
        day = [(d, hm(i), 100, 100.1, 99.9, 100, 1000) for i in range(6)]          # 09:30-09:55 flat around 100
        day += [(d, hm(6), 100.0, 103.0, 100.0, 102.8, 3000)]                      # 10:00 bar: stretched far above the band
        day += [(d, hm(7), 102.8, 102.9, 101.5, 101.6, 2000)]                      # 10:05 bar: closes back down -> short at 10:10
        day += [(d, hm(i), 101.5, 101.6, 100.0, 100.2, 1500) for i in range(8, 20)]  # falls back to VWAP
        fades = fr.find_fades({"XYZ": rows + day}, fr.date.fromisoformat(d))
        self.assertEqual(len(fades), 1)
        f = fades[0]
        self.assertEqual((f["side"], f["t_in"], f["why"]), ("SHORT", "10:10", "target"))
        self.assertGreater(f["R"], 0)
        self.assertTrue(f["stop"] > 103.0)
        f["cands"] = fr.option_candidates(f, fr.date.fromisoformat(d))
        self.assertEqual(f["cands"][0], ".XYZ261204P101")
        fr.price_fades(fades, {".XYZ261204P101": {"10:10": (2.00, 2.00), f["t_out"]: (2.60, 2.60)}})
        self.assertEqual((f["qty"], f["pnl"]), (12, 720.0))
        self.assertAlmostEqual(f["pnl_after_spread"], 720.0 - 0.043 * 2.0 * 100 * 12)
        self.assertIn("Fade test (not traded", fr.note(None, fr.summary(fades)))
        self.assertEqual(fr.find_fades({"XYZ": rows + day}, fr.date.fromisoformat(d), held={"XYZ": ("09:35", "12:00")}), [])


class SmallAccountTest(unittest.TestCase):
    """$10k shadow next to normal operations (user decisions 2026-11-26): sizing H, cheaper strikes."""

    def _t(self, tk, grade, px, sells, qty=10, frac=None, strike=100.0, right="C", t_buy="10:00", day="2026-11-30"):
        return {"ticker": tk, "grade": grade, "size_fraction": frac, "qty": qty, "strike": strike, "right": right,
                "expiry": "2026-12-04", "tv_entry": 100.0, "side": "LONG" if right == "C" else "SHORT",
                "buy": {"t": f"{day}T{t_buy}:00-05:00", "true": px, "qty": qty},
                "sells": [{"t": f"{day}T{h}:00-05:00", "true": sp, "qty": q, "kind": kd} for h, sp, q, kd in sells]}

    def _fake(self, prices):
        """prices: {strike: (entry price, {"HH:MM": price})} for .X calls expiring Dec 4."""
        def fetch(symbols, day):
            out = {}
            for sym in symbols:
                k = float(sym.split("C")[-1]) if "C" in sym[-8:] else None
                if k in prices:
                    p0, later = prices[k]
                    ser = {"10:00": (p0, p0, 500)}
                    ser.update({h: (v, v, 50) for h, v in later.items()})
                    out[sym] = ser
            return out
        return fetch

    def test_steps_out_for_a_cheaper_strike_and_mirrors_sales(self):
        from day_trading_setup.executor import small_account as sa
        # main bought the 100 call at $6 (too dear for a $500 budget: 0 contracts); 104 at $1.20 buys 4
        t = self._t("X", "B inside", 6.0, [("11:00", 8.0, 5, "2R"), ("12:00", 9.0, 5, "TRAIL")], frac=0.025)
        prices = {100.0: (6.0, {}), 101.0: (4.9, {}), 102.0: (3.4, {}), 103.0: (2.2, {}), 104.0: (1.2, {"11:00": 1.9, "12:00": 2.4}),
                  105.0: (0.70, {})}
        recs, carried = sa.build_day(sa.date(2026, 11, 30), [t], 10000.0, {}, fetch=self._fake(prices))
        r = recs[0]
        self.assertTrue(r["taken"])
        self.assertEqual((r["strike"], r["qty"], r["price"]), (104.0, 4, 1.2))
        self.assertGreaterEqual(r["delta"], 0.15)
        self.assertEqual([(s["qty"], s["price"]) for s in r["sells"]], [(2, 1.9), (2, 2.4)])
        self.assertAlmostEqual(r["pnl"], 2 * 0.7 * 100 + 2 * 1.2 * 100)
        self.assertEqual(carried, {})

    def test_skips_when_no_strike_fits_and_ledger(self):
        from day_trading_setup.executor import small_account as sa
        t = self._t("X", "B inside", 6.0, [("12:00", 5.0, 10, "STOP")], frac=0.025)
        recs, _ = sa.build_day(sa.date(2026, 11, 30), [t], 10000.0, {}, fetch=self._fake({100.0: (6.0, {})}))
        self.assertFalse(recs[0]["taken"])
        self.assertIn("buys 2", recs[0]["why"])
        day_doc, trades_doc = sa.page_docs({"date": "2026-11-30", "start": 10000.0, "pnl": 0.0, "balance": 10000.0, "taken": 0, "skipped": 1,
                                            "main_trades": 1}, recs, {"summary": {"pnl_real": -1234.0}})
        self.assertEqual((day_doc["main_pnl_tenth"], day_doc["skipped"]), (-123.4, 1))
        self.assertEqual(trades_doc["trades"][0]["id"], "2026-11-30_00")
        self.assertFalse(trades_doc["trades"][0]["taken"])
        self.assertIn("buys 2", trades_doc["trades"][0]["note"])
        rows = sa.ledger([("2026-11-30", {"records": [{"taken": True, "pnl": 250.0}], "main_trades": 3}),
                          ("2026-12-01", {"records": [{"taken": True, "pnl": -100.0}, {"taken": False}], "main_trades": 2})])
        self.assertEqual([(r["pnl"], r["balance"], r["taken"], r["skipped"]) for r in rows], [(250.0, 10250.0, 1, 0), (-100.0, 10150.0, 1, 1)])


class ExitTestsTest(unittest.TestCase):
    """Report-only exit tests: option break-even stop and volume fade (user decision 2026-11-14)."""
    NY = __import__("zoneinfo").ZoneInfo("America/New_York")
    m = staticmethod(lambda hm, d="2026-11-16": int(datetime.fromisoformat(f"{d}T{hm}:00-05:00").timestamp() * 1000))

    def _trade(self, sell_t="15:55", side="LONG"):
        return {"ticker": "MU", "side": side, "opened_at": "2026-11-16T10:00:30-05:00", "closed_at": f"2026-11-16T{sell_t}:00-05:00",
                "qty": 4, "events": [{"t": "2026-11-16T10:00:30-05:00", "action": "BUY 4 @ 2.0", "qty": 4, "price": 2.0},
                                     {"t": f"2026-11-16T{sell_t}:00-05:00", "action": "SELL 4 @ 1.0 (TradingView EOD)", "qty": 4, "price": 1.0,
                                      "bid": 0.95, "ask": 1.05}]}

    def _rp(self, t, bars, vol=None):
        from day_trading_setup.executor import reprice
        rp = {f"MU|{t['opened_at']}": {"true": 2.0}, f"MU|{t['events'][1]['t']}": {"true": 1.0, "trade_minutes_30": 30},
              f"MU|{t['opened_at']}|BARS": {"bars": bars}}
        if vol is not None:
            rp[f"MU|{t['opened_at']}|VOL5"] = {"bars": vol}
        return rp

    def test_break_even_stop(self):
        from day_trading_setup.executor import journal
        m, t = self.m, self._trade()
        bars = [[m("10:01"), 2.0, 2.2], [m("10:20"), 2.6, 2.4], [m("11:00"), 2.1, 1.9], [m("15:55"), 1.0, 1.0]]
        r = journal.be_test(t, self._rp(t, bars))
        self.assertEqual((r["armed"], r["fired"], r["price"], r["pnl"], r["pnl_booked"]), ("10:20", "11:00", 1.9, -40.0, -400.0))
        r = journal.be_test(t, self._rp(t, [[m("10:01"), 2.0, 2.2], [m("11:00"), 2.1, 1.9]]))   # never +25%: as booked
        self.assertEqual((r["fired"], r["pnl"]), (None, -400.0))

    def test_volume_fade(self):
        from day_trading_setup.executor import journal
        m, t = self.m, self._trade()
        vol = []
        for d in ("2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"):
            for hm in ("10:00", "10:05", "10:10", "10:15", "10:20", "10:25"):
                vol.append([m(hm, d), 100, 101, 99, 100, 1000])
        # entry bar 10:00 makes the high (105); then 3 quiet bars under it: fires at the 10:20 close (10:15 bar)
        today = [("10:00", 105, 1500), ("10:05", 104, 300), ("10:10", 103, 300), ("10:15", 104.5, 300), ("10:20", 106, 2000)]
        vol += [[m(hm), 102, hi, 101, 102, v] for hm, hi, v in today]
        bars = [[m("10:01"), 2.0, 2.1], [m("10:20"), 1.8, 1.7], [m("15:55"), 1.0, 1.0]]
        r = journal.vf_test(t, self._rp(t, bars, vol))
        self.assertEqual((r["fired"], r["price"], r["pnl"]), ("10:20", 1.8, -80.0))
        vol[-3][5] = 900                                                    # one bar not quiet: no fire
        self.assertIsNone(journal.vf_test(t, self._rp(t, bars, vol))["fired"])


if __name__ == "__main__":
    unittest.main()
