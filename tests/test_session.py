"""Daily-loss rollover semantics: the counter resets at market open, not midnight."""
import datetime as dt
import unittest

from engine import session_date


class SessionDateTest(unittest.TestCase):
    def test_after_market_open_counts_as_today(self):
        # Monday 2026-09-07 10:00 → session is that day
        now = dt.datetime(2026, 9, 7, 10, 0)
        self.assertEqual(session_date(now), "2026-09-07")

    def test_before_open_still_counts_as_previous_session(self):
        # Monday 06:00 → market hasn't opened, still Sunday's session
        now = dt.datetime(2026, 9, 7, 6, 0)
        self.assertEqual(session_date(now), "2026-09-06")

    def test_exactly_at_open_starts_new_session(self):
        now = dt.datetime(2026, 9, 7, 9, 15)
        self.assertEqual(session_date(now), "2026-09-07")

    def test_just_before_open_is_previous_session(self):
        now = dt.datetime(2026, 9, 7, 9, 14, 59)
        self.assertEqual(session_date(now), "2026-09-06")


if __name__ == "__main__":
    unittest.main()
