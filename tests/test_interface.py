"""Broker interface contract: every backend must implement the 4 methods."""
import unittest

from engine import Broker, PaperBroker


class BrokerInterfaceTest(unittest.TestCase):
    def test_bare_broker_raises_not_implemented(self):
        b = Broker()
        for call in (lambda: b.quote("AAPL"),
                     lambda: b.buy("AAPL", 1),
                     lambda: b.sell("AAPL", 1),
                     lambda: b.account()):
            with self.assertRaises(NotImplementedError):
                call()

    def test_paperbroker_implements_full_contract(self):
        for name in ("quote", "buy", "sell", "account"):
            self.assertTrue(callable(getattr(PaperBroker, name)),
                            f"PaperBroker missing {name}")


if __name__ == "__main__":
    unittest.main()
