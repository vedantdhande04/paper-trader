"""Backtest stats: pure summarize/per_symbol, plus a read-only db read."""
import os
import sqlite3
import tempfile
import unittest

import backtest


def trade(symbol, realized, side="SELL"):
    return {"ts": "2026-09-10T10:00:00", "symbol": symbol, "side": side, "qty": 1.0,
            "price": 100.0, "value": 100.0, "realized_pnl": realized, "note": ""}


class SummarizeTest(unittest.TestCase):
    def test_counts_wins_losses_and_win_rate(self):
        s = backtest.summarize([trade("A", 100.0), trade("A", -40.0), trade("A", 60.0)])
        self.assertEqual((s["wins"], s["losses"]), (2, 1))
        self.assertAlmostEqual(s["win_rate"], 66.7, places=1)
        self.assertEqual(s["realized"], 120.0)

    def test_averages_and_extremes(self):
        s = backtest.summarize([trade("A", 100.0), trade("A", 200.0), trade("A", -50.0)])
        self.assertEqual(s["avg_win"], 150.0)
        self.assertEqual(s["avg_loss"], 50.0)
        self.assertEqual(s["best"], 200.0)
        self.assertEqual(s["worst"], -50.0)

    def test_profit_factor(self):
        s = backtest.summarize([trade("A", 300.0), trade("A", -100.0)])
        self.assertEqual(s["profit_factor"], 3.0)

    def test_profit_factor_infinite_without_losses(self):
        self.assertEqual(backtest.summarize([trade("A", 10.0)])["profit_factor"],
                         float("inf"))

    def test_open_buys_are_ignored_by_the_stats(self):
        s = backtest.summarize([trade("A", 50.0), trade("A", None, side="BUY")])
        self.assertEqual(s["trades"], 2)
        self.assertEqual(s["closed"], 1)
        self.assertEqual(s["realized"], 50.0)

    def test_empty_input_is_all_zeroes(self):
        s = backtest.summarize([])
        self.assertEqual((s["closed"], s["realized"], s["profit_factor"]), (0, 0.0, 0.0))

    def test_per_symbol_sorts_by_realized_pnl(self):
        rows = backtest.per_symbol([trade("A", 10.0), trade("B", 90.0)])
        self.assertEqual(list(rows), ["B", "A"])
        self.assertEqual(rows["B"]["realized"], 90.0)


class LoadTradesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db = tmp.name
        c = sqlite3.connect(self.db)
        c.executescript("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, symbol TEXT, side TEXT,
            qty REAL, price REAL, value REAL, realized_pnl REAL, note TEXT);
        INSERT INTO trades (ts, symbol, side, qty, price, value, realized_pnl, note)
            VALUES ('2026-09-10T10:00:00','A.NS','BUY',1,100,100,NULL,''),
                   ('2026-09-11T10:00:00','B.NS','SELL',1,110,110,10,'');
        """)
        c.commit()
        c.close()

    def tearDown(self):
        os.unlink(self.db)

    def test_reads_everything_oldest_first(self):
        rows = backtest.load_trades(self.db)
        self.assertEqual([r["symbol"] for r in rows], ["A.NS", "B.NS"])

    def test_symbol_filter_is_case_insensitive(self):
        rows = backtest.load_trades(self.db, "a.ns")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["side"], "BUY")

    def test_missing_db_is_empty_not_an_error(self):
        self.assertEqual(backtest.load_trades(self.db + ".nope"), [])

    def test_read_only_open_does_not_create_tables(self):
        # a db with no trades table must fail loudly rather than silently write
        empty = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        empty.close()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                backtest.load_trades(empty.name)
        finally:
            os.unlink(empty.name)


if __name__ == "__main__":
    unittest.main()
