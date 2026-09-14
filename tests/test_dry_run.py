"""Dry-run mode: signals are logged, no orders leave the auto-trader."""
import unittest
from unittest import mock

from autotrader import AutoTrader


class FakeBroker:
    """Minimal Broker stand-in — records orders instead of executing them."""

    def __init__(self, dry_run=True, pos=None):
        self.config = {"enabled": True, "strategy": "sma", "interval_min": 5,
                       "symbols": ["BTC-INR"], "position_pct": 20,
                       "dry_run": dry_run}
        self.pos = pos
        self.calls = []

    def get_auto_config(self):
        return dict(self.config)

    def set_auto_config(self, updates):
        self.config.update(updates or {})
        return dict(self.config)

    def quote(self, symbol):
        return {"symbol": symbol, "price": 100.0, "cached": False}

    def position(self, symbol):
        return self.pos

    def account(self):
        return {"equity": 100_000.0}

    def buy(self, symbol, qty, note=""):
        self.calls.append(("BUY", symbol, qty, note))
        return {"ok": True}

    def sell(self, symbol, qty, note=""):
        self.calls.append(("SELL", symbol, qty, note))
        return {"ok": True, "realized_pnl": 0.0}


class DryRunTest(unittest.TestCase):
    def _tick(self, broker, signal):
        trader = AutoTrader(broker)
        with mock.patch("autotrader.fetch_history", return_value=object()), \
             mock.patch("autotrader.compute_signal", return_value=(signal, "test")):
            trader._tick()
        return trader

    def test_dry_run_logs_a_buy_without_placing_it(self):
        broker = FakeBroker(dry_run=True)
        trader = self._tick(broker, "BUY")
        self.assertEqual(broker.calls, [])
        self.assertEqual(trader.last_status["BTC-INR"]["action"], "would buy 200")

    def test_dry_run_logs_a_sell_without_placing_it(self):
        broker = FakeBroker(dry_run=True, pos={"symbol": "BTC-INR", "qty": 3})
        trader = self._tick(broker, "SELL")
        self.assertEqual(broker.calls, [])
        self.assertEqual(trader.last_status["BTC-INR"]["action"], "would sell 3")

    def test_live_mode_still_trades(self):
        broker = FakeBroker(dry_run=False)
        self._tick(broker, "BUY")
        self.assertEqual(broker.calls[0][0], "BUY")
        self.assertEqual(broker.calls[0][3], "AUTO")   # tagged in the trade log

    def test_status_reports_the_flag(self):
        trader = AutoTrader(FakeBroker(dry_run=True))
        self.assertTrue(trader.status()["dry_run"])


if __name__ == "__main__":
    unittest.main()
