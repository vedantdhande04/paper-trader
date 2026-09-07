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
        PRICES["TEST.NS"] = 100.0   # deterministic baseline per test
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

    def test_daily_loss_limit_blocks_new_buys(self):
        self.broker.buy("TEST.NS", 200)   # 20k @ 100, under the 25% cap
        PRICES["TEST.NS"] = 75.0          # position now worth 15k
        self.broker._price_cache.clear()  # make the drop visible
        # equity 95k vs 100k day-start -> today at -5k, right on the limit
        with self.assertRaises(TradeError):
            self.broker.buy("TEST.NS", 1)

    def test_buys_allowed_below_daily_loss_limit(self):
        self.broker.buy("TEST.NS", 200)
        PRICES["TEST.NS"] = 98.0          # small dip, ~ -400 today
        self.broker._price_cache.clear()
        res = self.broker.buy("TEST.NS", 10)
        self.assertTrue(res["ok"])
        self.assertEqual(self.broker.position("TEST.NS")["qty"], 210.0)

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

    def test_position_cap_blocks_stacking_past_25pct(self):
        self.broker.buy("TEST.NS", 200)          # 20k @ 100 — under the cap
        # adding 100 more would make the holding 30k = 30% of 100k equity
        with self.assertRaises(TradeError):
            self.broker.buy("TEST.NS", 100)

    def test_position_cap_allows_adds_under_25pct(self):
        self.broker.buy("TEST.NS", 200)
        self.broker.buy("TEST.NS", 40)           # 240 @ 100 = 24k — still fine
        self.assertEqual(self.broker.position("TEST.NS")["qty"], 240.0)

    def test_identical_resubmit_blocked_within_a_minute(self):
        self.broker.buy("TEST.NS", 10)
        with self.assertRaises(TradeError):      # same symbol/side/qty again
            self.broker.buy("TEST.NS", 10)

    def test_quick_add_with_different_qty_not_blocked(self):
        self.broker.buy("TEST.NS", 10)
        self.broker.buy("TEST.NS", 5)            # scaling in is not a duplicate
        self.assertEqual(self.broker.position("TEST.NS")["qty"], 15.0)


if __name__ == "__main__":
    unittest.main()
