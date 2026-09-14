"""Market-hours guard: Mon-Fri 09:15-15:30 IST, no holidays yet."""
import datetime as dt
import unittest

from engine import market_hours


def at(y, m, d, hh, mm):
    return dt.datetime(y, m, d, hh, mm)


class MarketHoursTest(unittest.TestCase):
    def test_mid_session_is_open(self):
        # Wednesday 2026-09-09 11:30
        self.assertTrue(market_hours(at(2026, 9, 9, 11, 30))["open"])

    def test_exactly_at_open_is_open(self):
        self.assertTrue(market_hours(at(2026, 9, 9, 9, 15))["open"])

    def test_just_before_open_is_pre_open(self):
        h = market_hours(at(2026, 9, 9, 9, 14))
        self.assertFalse(h["open"])
        self.assertEqual(h["reason"], "pre-open")

    def test_at_close_is_shut(self):
        h = market_hours(at(2026, 9, 9, 15, 30))
        self.assertFalse(h["open"])
        self.assertEqual(h["reason"], "after close")

    def test_evening_is_after_close(self):
        self.assertEqual(market_hours(at(2026, 9, 9, 21, 0))["reason"], "after close")

    def test_saturday_is_weekend(self):
        h = market_hours(at(2026, 9, 12, 11, 0))
        self.assertFalse(h["open"])
        self.assertEqual(h["reason"], "weekend")

    def test_sunday_is_weekend(self):
        self.assertEqual(market_hours(at(2026, 9, 13, 11, 0))["reason"], "weekend")


if __name__ == "__main__":
    unittest.main()
