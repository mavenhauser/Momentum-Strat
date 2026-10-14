import unittest
from datetime import date

from day_trading_setup.executor import contracts, signals


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

    def test_expiry_holiday_friday_and_fallback(self):
        exp = [date(2026, 11, 26), date(2026, 12, 4)]   # no Friday listing that week
        self.assertEqual(contracts.pick_expiry(exp, date(2026, 11, 23)), date(2026, 11, 26))
        self.assertEqual(contracts.pick_expiry([date(2026, 11, 20)], date(2026, 10, 12)), date(2026, 11, 20))
        self.assertIsNone(contracts.pick_expiry([], date(2026, 10, 12)))

    def test_strikes(self):
        self.assertEqual(contracts.strikes_by_distance([170, 175, 177.5, 180, 182.5], 177.16, 3), [177.5, 175, 180])

    def test_liquidity(self):
        self.assertTrue(contracts.liquid({"bid": 2.0, "ask": 2.2, "open_interest": 500}))
        self.assertFalse(contracts.liquid({"bid": 2.0, "ask": 2.6, "open_interest": 500}))   # spread 26%
        self.assertFalse(contracts.liquid({"bid": 0, "ask": 0.1, "open_interest": 500}))
        self.assertFalse(contracts.liquid({"bid": 2.0, "ask": 2.1, "open_interest": 10}))

    def test_sizing(self):
        self.assertEqual(contracts.contracts_for(100_000, 2.50), 4)   # $1,000 / $250
        self.assertEqual(contracts.contracts_for(100_000, 15.0), 1)   # below one contract -> 1
        self.assertEqual(contracts.contracts_for(100_000, None), 0)

    def test_trim(self):
        self.assertEqual([contracts.trim_quantity(q) for q in (0, 1, 2, 3, 5)], [0, 1, 1, 1, 2])

    def test_sessions(self):
        self.assertEqual(contracts.sessions_held(date(2026, 10, 15), date(2026, 10, 15)), 1)   # Thu
        self.assertEqual(contracts.sessions_held(date(2026, 10, 16), date(2026, 10, 19)), 2)   # Fri -> Mon


if __name__ == "__main__":
    unittest.main()
