"""The daily summary line: right numbers, logged once per session."""
import logging
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import PaperBroker, summary_line


class _Collector(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class SummaryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db_path = tmp.name
        self.broker = PaperBroker(self.db_path)
        self.logger = logging.getLogger("papertrader")

    def tearDown(self):
        os.unlink(self.db_path)

    def test_line_carries_the_numbers(self):
        acc = {"as_of": "2026-09-11T12:00:00", "cash": 99_000.0,
               "equity": 100_500.0, "daily_pnl": 500.0, "positions": [{}]}
        line = summary_line(acc)
        self.assertIn("summary:", line)
        self.assertIn("100,500.00", line)
        self.assertIn("+500.00", line)
        self.assertIn("open 1", line)

    def test_logged_once_per_session_not_every_refresh(self):
        collector = _Collector()
        self.logger.addHandler(collector)
        try:
            self.broker.account()
            self.broker.account()
            self.broker.account()
        finally:
            self.logger.removeHandler(collector)
        summaries = [m for m in collector.messages if m.startswith("summary:")]
        self.assertEqual(len(summaries), 1)

    def test_tomorrow_logs_again(self):
        self.broker._set_meta("last_summary_day", "1999-01-01")
        collector = _Collector()
        self.logger.addHandler(collector)
        try:
            self.broker.account()
        finally:
            self.logger.removeHandler(collector)
        self.assertEqual(len([m for m in collector.messages
                              if m.startswith("summary:")]), 1)


if __name__ == "__main__":
    unittest.main()
