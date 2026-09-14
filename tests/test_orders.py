"""Stop-loss orders: arming, firing, cancelling — temp DB, no network."""
import os
import tempfile
import unittest

from engine import PaperBroker, TradeError

PRICES = {"TEST.NS": 100.0}


def fake_resolve(self, symbol):
    return "TEST.NS", PRICES["TEST.NS"]


class StopLossTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db_path = tmp.name
        self.broker = PaperBroker(self.db_path)
        PRICES["TEST.NS"] = 100.0
        self._orig_resolve = PaperBroker._resolve
        PaperBroker._resolve = fake_resolve
        self.broker.buy("TEST.NS", 10)

    def tearDown(self):
        PaperBroker._resolve = self._orig_resolve
        os.unlink(self.db_path)

    def _drop_to(self, price):
        PRICES["TEST.NS"] = price
        self.broker._price_cache.clear()

    def test_arms_an_order_on_a_held_position(self):
        res = self.broker.place_stop_loss("TEST.NS", 10, 90.0)
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], "OPEN")
        orders = self.broker.open_orders()
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["trigger"], 90.0)
        self.assertEqual(orders[0]["kind"], "STOP_LOSS")

    def test_stop_must_be_below_the_last_price(self):
        with self.assertRaises(TradeError):
            self.broker.place_stop_loss("TEST.NS", 10, 100.0)

    def test_cannot_protect_more_than_held(self):
        with self.assertRaises(TradeError):
            self.broker.place_stop_loss("TEST.NS", 25, 90.0)

    def test_cannot_protect_a_symbol_you_dont_hold(self):
        self.broker.sell("TEST.NS", 10)
        with self.assertRaises(TradeError):
            self.broker.place_stop_loss("TEST.NS", 10, 90.0)

    def test_rejects_non_positive_stop(self):
        with self.assertRaises(TradeError):
            self.broker.place_stop_loss("TEST.NS", 10, 0)

    def test_rejects_non_positive_qty(self):
        with self.assertRaises(TradeError):
            self.broker.place_stop_loss("TEST.NS", 0, 90.0)

    def test_holds_fire_when_price_crosses_the_trigger(self):
        self.broker.place_stop_loss("TEST.NS", 10, 90.0)
        self._drop_to(88.0)
        fired = self.broker.check_open_orders()
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["status"], "FILLED")
        self.assertEqual(fired[0]["fill_price"], 88.0)
        self.assertIsNone(self.broker.position("TEST.NS"))
        self.assertEqual(self.broker.account()["trades"][0]["note"], "STOP LOSS")
        self.assertEqual(self.broker.open_orders(), [])

    def test_stays_armed_while_price_is_above_the_trigger(self):
        self.broker.place_stop_loss("TEST.NS", 10, 90.0)
        self._drop_to(95.0)
        self.assertEqual(self.broker.check_open_orders(), [])
        self.assertEqual(len(self.broker.open_orders()), 1)
        self.assertIsNotNone(self.broker.position("TEST.NS"))

    def test_exact_trigger_price_fires(self):
        self.broker.place_stop_loss("TEST.NS", 10, 90.0)
        self._drop_to(90.0)
        self.assertEqual(len(self.broker.check_open_orders()), 1)
        self.assertIsNone(self.broker.position("TEST.NS"))

    def test_cancel_stops_it_from_firing(self):
        order_id = self.broker.place_stop_loss("TEST.NS", 10, 90.0)["id"]
        self.assertTrue(self.broker.cancel_order(order_id)["ok"])
        self._drop_to(80.0)
        self.assertEqual(self.broker.check_open_orders(), [])
        self.assertIsNotNone(self.broker.position("TEST.NS"))
        with self.assertRaises(TradeError):
            self.broker.cancel_order(order_id)   # already cancelled

    def test_cancel_unknown_order_errors(self):
        with self.assertRaises(TradeError):
            self.broker.cancel_order(999)

    def test_kill_switch_leaves_stops_armed(self):
        self.broker.place_stop_loss("TEST.NS", 10, 90.0)
        self.broker.set_kill_switch(True)
        self._drop_to(80.0)
        self.assertEqual(self.broker.check_open_orders(), [])
        self.assertEqual(len(self.broker.open_orders()), 1)

    def test_account_snapshot_lists_open_orders(self):
        self.broker.place_stop_loss("TEST.NS", 10, 90.0)
        self.assertEqual(len(self.broker.account()["orders"]), 1)


if __name__ == "__main__":
    unittest.main()
