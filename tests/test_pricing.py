"""Price-fetch robustness: bad ticker vs. Yahoo being unreachable."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine
from engine import PaperBroker, TradeError


class Boom(Exception):
    """Stands in for a socket/HTTP error coming out of yfinance."""


class NotFound(Exception):
    response = type("R", (), {"status_code": 404})()


class PricingFailureTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db_path = tmp.name
        self.broker = PaperBroker(self.db_path)
        self._orig_ticker = engine.yf.Ticker
        self._orig_sleep = engine.TRANSIENT_RETRY_SLEEP
        engine.TRANSIENT_RETRY_SLEEP = 0     # no real waiting in tests

    def tearDown(self):
        engine.yf.Ticker = self._orig_ticker
        engine.TRANSIENT_RETRY_SLEEP = self._orig_sleep
        os.unlink(self.db_path)

    def _ticker_raises(self, exc):
        def factory(symbol):
            raise exc
        engine.yf.Ticker = factory

    def _ticker_priced(self, price):
        class FastInfo:
            last_price = price

        class Ticker:
            fast_info = FastInfo()
        engine.yf.Ticker = lambda symbol: Ticker()

    def test_network_failure_blames_yahoo_not_the_symbol(self):
        self._ticker_raises(Boom("connection reset"))
        with self.assertRaises(TradeError) as ctx:
            self.broker.quote("TEST.NS")
        msg = str(ctx.exception)
        self.assertIn("Yahoo", msg)
        self.assertIn("Boom", msg)
        self.assertNotIn("Unknown symbol", msg)

    def test_404_is_reported_as_unknown_symbol(self):
        self._ticker_raises(NotFound())
        with self.assertRaises(TradeError) as ctx:
            self.broker.quote("NOPE.NS")
        self.assertIn("Unknown symbol", str(ctx.exception))

    def test_symbol_without_a_price_is_unknown(self):
        self._ticker_priced(None)
        with self.assertRaises(TradeError) as ctx:
            self.broker.quote("NOPE.NS")
        self.assertIn("Unknown symbol", str(ctx.exception))

    def test_dashboard_falls_back_to_last_known_price(self):
        self._ticker_priced(100.0)
        self.broker.buy("TEST.NS", 10)
        # yahoo goes away and the cache is dropped (e.g. someone hit refresh)
        self._ticker_raises(Boom("timed out"))
        self.broker._price_cache.clear()
        acc = self.broker.account()
        self.assertEqual(acc["positions"][0]["last_price"], 100.0)
        self.assertEqual(acc["equity"], 100_000.0)   # flat, not wiped out


if __name__ == "__main__":
    unittest.main()
