"""
Unit tests for the indicator / allocation / guard math — the pure functions the
live bots and backtests share. Fixtures are hand-computed so a silent regression
in any formula fails loudly here instead of quietly mis-trading.

Run: python tests_indicators.py    (stdlib unittest, no extra deps, no network)
"""

import json
import os
import tempfile
import unittest

from backtest_research3 import rsi, cumulative_rsi2
from backtest_fable import ibs_series, rsi2_series
from capital_allocator import _profit_factor, _consec_losses, _raw_weight
from agent_loop import _fmt
from project_optimizer import _is_duplicate
import risk_guard


def bar(o=0, h=0, l=0, c=0):
    return {"o": o, "h": h, "l": l, "c": c}


class TestRSI(unittest.TestCase):
    def test_all_gains_is_100(self):
        closes = [1, 2, 3, 4, 5]
        self.assertEqual(rsi(closes, 4, period=2), 100.0)

    def test_all_losses_is_0(self):
        closes = [5, 4, 3, 2, 1]
        self.assertAlmostEqual(rsi(closes, 4, period=2), 0.0)

    def test_balanced_is_50(self):
        # +1 then -1 over a 2-period window → avg gain == avg loss → RSI 50.
        closes = [10, 11, 10]
        self.assertAlmostEqual(rsi(closes, 2, period=2), 50.0)

    def test_insufficient_history_is_none(self):
        self.assertIsNone(rsi([1, 2], 1, period=2))


class TestCumulativeRSI2(unittest.TestCase):
    def test_flat_series_never_enters(self):
        bars = [{"c": 100.0} for _ in range(260)]
        rets = cumulative_rsi2(bars)
        self.assertTrue(all(r == 0.0 for r in rets))

    def test_length_matches_input(self):
        bars = [{"c": 100 + (i % 7)} for i in range(300)]
        self.assertEqual(len(cumulative_rsi2(bars)), 300)


class TestIBS(unittest.TestCase):
    def test_enters_on_close_near_low_and_exits_near_high(self):
        # Day 0: IBS = (10-10)/(20-10) = 0 < 0.2 → enter at close.
        # Day 1: IBS = (19.9-10)/(20-10) ≈ 0.99 > 0.8 → exit; return realized.
        bars = [bar(h=20, l=10, c=10), bar(h=20, l=10, c=19.9)]
        rets = ibs_series(bars)
        self.assertAlmostEqual(rets[1], 19.9 / 10 - 1 - 0.0005, places=6)

    def test_no_range_day_is_neutral(self):
        # h == l → IBS defaults to 0.5: neither entry nor exit.
        bars = [bar(h=10, l=10, c=10)] * 5
        self.assertTrue(all(r == 0.0 for r in ibs_series(bars)))


class TestRSI2Strategy(unittest.TestCase):
    def test_never_enters_below_200sma(self):
        # Monotonic decline: always below the 200-day SMA → never long.
        bars = [{"c": 1000 - i} for i in range(400)]
        self.assertTrue(all(r == 0.0 for r in rsi2_series(bars)))


class TestCapitalAllocator(unittest.TestCase):
    def T(self, *pnls):
        return [{"pnl": p} for p in pnls]

    def test_profit_factor_basic(self):
        self.assertAlmostEqual(_profit_factor(self.T(10, -5)), 2.0)
        self.assertAlmostEqual(_profit_factor(self.T(3, -6)), 0.5)

    def test_zero_loss_clamps_to_2(self):
        self.assertEqual(_profit_factor(self.T(10, 20)), 2.0)
        self.assertEqual(_profit_factor(self.T()), 1.0)

    def test_consec_losses_counts_from_end(self):
        self.assertEqual(_consec_losses(self.T(5, -1, -2)), 2)
        self.assertEqual(_consec_losses(self.T(-1, 5)), 0)

    def test_raw_weight_bands(self):
        self.assertEqual(_raw_weight(0.3, 0), 0.25)   # PF < 0.5 → near-pause
        self.assertAlmostEqual(_raw_weight(0.75, 0), 0.75)
        self.assertAlmostEqual(_raw_weight(1.5, 0), 1.5)
        self.assertEqual(_raw_weight(5.0, 0), 2.0)    # capped
        self.assertEqual(_raw_weight(2.0, 5), 0.25)   # loss streak overrides


class TestAgentLoopFmt(unittest.TestCase):
    def test_fmt(self):
        self.assertEqual(_fmt(1.234), "1.23")
        self.assertEqual(_fmt("?"), "?")
        self.assertEqual(_fmt(None), "?")
        self.assertEqual(_fmt(0.171, ".1%"), "17.1%")


class TestBacklogDedup(unittest.TestCase):
    def test_rephrased_idea_is_duplicate(self):
        a = "- [P1] Wrap get_congress_trades() in try/except within run() so a Quiver failure degrades gracefully"
        b = "- [P1] get_congress_trades() can raise but run() doesn't catch it — wrap the Quiver fetch in try/except"
        self.assertTrue(_is_duplicate(a, [b]))

    def test_distinct_idea_survives(self):
        a = "- [P2] Refactor tjr_strategy.py into indicator/signal/execution modules"
        b = "- [P1] Wrap get_congress_trades() in try/except in run()"
        self.assertFalse(_is_duplicate(a, [b]))


class TestRiskGuardPeak(unittest.TestCase):
    def test_peak_ratchets_up_never_down(self):
        with tempfile.TemporaryDirectory() as d:
            old = risk_guard.PEAK_FILE
            risk_guard.PEAK_FILE = os.path.join(d, "risk_state.json")
            try:
                self.assertEqual(risk_guard._peak_equity(100_000), 100_000)
                self.assertEqual(risk_guard._peak_equity(90_000), 100_000)
                self.assertEqual(risk_guard._peak_equity(120_000), 120_000)
                with open(risk_guard.PEAK_FILE) as f:
                    self.assertEqual(json.load(f)["peak_equity"], 120_000)
            finally:
                risk_guard.PEAK_FILE = old


if __name__ == "__main__":
    unittest.main(verbosity=2)
