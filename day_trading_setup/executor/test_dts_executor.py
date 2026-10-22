import unittest
from datetime import date

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

    def test_exits(self):
        for reason in ("STOP", "BE", "TRAIL", "EOD", "TIME"):
            s = signals.parse(f"DTS: EXIT SHORT TSLA ({reason}) @ 379.68")
            self.assertEqual((s.kind, s.ticker, s.side, s.reason, s.price), ("EXIT", "TSLA", "SHORT", reason, 379.68))

    def test_aplus_and_other(self):
        self.assertEqual(signals.parse("DTS: A+ OUTSIDE LONG MSFT @ 539.96 - watch for the 8 EMA retest entry").kind, "APLUS")
        self.assertEqual(signals.parse("DTS screener: $KO ▲ above PDH + PMH @ 88.77").kind, "IGNORED")
        self.assertEqual(signals.parse("").kind, "IGNORED")


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

    def test_choose_single_contract(self):
        qs = {500: self.q(11.00, 11.40, 0.52), 505: self.q(8.40, 8.80, 0.43), 510: self.q(6.10, 6.40, 0.33), 515: self.q(5.80, 6.05, 0.12)}
        k, qty, rule, d = contracts.choose_strike(qs, 500.3, "C", 1000)
        self.assertEqual((k, qty), (500, 1))
        self.assertIn("held through 2R", rule)

    def test_single_contract_searches_out_the_ladder(self):
        # the near strikes are too wide to trade: take the first liquid one further out
        wide = lambda d: self.q(5.00, 7.00, d)
        qs = {500: wide(0.52), 505: wide(0.45), 510: wide(0.38), 495: wide(0.60),
              515: wide(0.30), 520: self.q(9.00, 9.40, 0.22)}
        k, qty, rule, d = contracts.choose_strike(qs, 500.5, "C", 1000)
        self.assertEqual((k, qty), (520, 1))

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

    def test_sessions(self):
        self.assertEqual(contracts.sessions_held(date(2026, 10, 15), date(2026, 10, 15)), 1)   # Thu
        self.assertEqual(contracts.sessions_held(date(2026, 10, 16), date(2026, 10, 19)), 2)   # Fri -> Mon


if __name__ == "__main__":
    unittest.main()
