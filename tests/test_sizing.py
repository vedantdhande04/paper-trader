"""Unit tests for the position-sizing helper (pure math, no broker/network)."""
import unittest

from engine import MAX_POSITION_PCT, TradeError, position_size


class PositionSizeTest(unittest.TestCase):
    def test_qty_for_given_pct_of_equity(self):
        # 20% of 100k = 20k budget; at 100/share that's 200 shares
        self.assertEqual(position_size(100_000.0, 100.0, 20.0), 200.0)

    def test_capped_at_max_position_pct(self):
        # asking for 50% gets clamped to MAX_POSITION_PCT (25%)
        self.assertEqual(position_size(100_000.0, 100.0, 50.0),
                         MAX_POSITION_PCT * 100_000.0 / 100.0)

    def test_defaults_to_20_pct(self):
        self.assertEqual(position_size(50_000.0, 50.0), 200.0)

    def test_zero_equity_gives_zero(self):
        self.assertEqual(position_size(0.0, 100.0), 0.0)

    def test_non_positive_price_rejected(self):
        with self.assertRaises(TradeError):
            position_size(100_000.0, 0.0)


if __name__ == "__main__":
    unittest.main()
