"""Unit tests for PaperBroker buy/sell against a temp DB, no network."""
import os
import tempfile
import unittest

from engine import PaperBroker, TradeError

PRICES = {"TEST.NS": 100.0}


def fake_resolve(self, symbol):
    """Stand-in for live pricing so tests never hit Yahoo."""
    return "TEST.NS", PRICES["TEST.NS"]


class PaperBrokerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db_path = tmp.name
        self.broker = PaperBroker(self.db_path)
        self._orig_resolve = PaperBroker._resolve
        PaperBroker._resolve = fake_resolve

    def tearDown(self):
        PaperBroker._resolve = self._orig_resolve
        os.unlink(self.db_path)

    def test_buy_opens_position_and_debits_cash(self):
        res = self.broker.buy("TEST.NS", 10, note="test")
        self.assertTrue(res["ok"])
        pos = self.broker.position("TEST.NS")
        self.assertIsNotNone(pos)
        self.assertEqual(pos["qty"], 10)
        self.assertEqual(pos["avg_price"], 100.0)
        acc = self.broker.account()
        self.assertEqual(acc["cash"], 100000.0 - 1000.0)
        last = acc["trades"][0]
        self.assertEqual(last["side"], "BUY")
        self.assertEqual(last["note"], "test")

    def test_buy_rejects_insufficient_cash(self):
        with self.assertRaises(TradeError):
            self.broker.buy("TEST.NS", 1e9)

    def test_sell_closes_position_and_credits_cash(self):
        self.broker.buy("TEST.NS", 10)
        PRICES["TEST.NS"] = 120.0
        res = self.broker.sell("TEST.NS", 10, note="take profit")
        self.assertTrue(res["ok"])
        self.assertEqual(res["realized_pnl"], 200.0)
        self.assertIsNone(self.broker.position("TEST.NS"))
        acc = self.broker.account()
        self.assertEqual(acc["cash"], 100000.0 + 200.0)
        self.assertEqual(acc["trades"][0]["side"], "SELL")

    def test_cannot_sell_more_than_held(self):
        self.broker.buy("TEST.NS", 5)
        with self.assertRaises(TradeError):
            self.broker.sell("TEST.NS", 6)

    def test_kill_switch_blocks_trades(self):
        self.broker.set_kill_switch(True)
        with self.assertRaises(TradeError):
            self.broker.buy("TEST.NS", 1)
        self.broker.set_kill_switch(False)
        self.broker.buy("TEST.NS", 1)  # works again

    def test_top_up_credits_cash_without_skewing_pnl(self):
        self.broker.buy("TEST.NS", 10)
        before = self.broker.account()
        res = self.broker.top_up(5_000.0)
        self.assertEqual(res["cash"], before["cash"] + 5_000.0)
        self.assertEqual(res["start_cash"], before["start_cash"] + 5_000.0)
        self.assertEqual(res["total_pnl"], before["total_pnl"])
        self.assertEqual(res["daily_pnl"], before["daily_pnl"])
        with self.assertRaises(TradeError):
            self.broker.top_up(-100.0)


if __name__ == "__main__":
    unittest.main()
