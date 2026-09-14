"""Backtest over the trades already stored in state.db.

Not a market simulator — it replays the paper broker's own trade log and
reports how the runs actually went: win rate, average win/loss, profit
factor and a per-symbol breakdown.

    python backtest.py                    # everything so far
    python backtest.py --symbol TCS.NS    # one symbol
    python backtest.py --csv              # machine readable

The db is opened read-only, so this can never corrupt live state.
"""
import argparse
import csv
import io
import os
import sqlite3

from engine import CURRENCY, DB_PATH

COLS = ("ts", "symbol", "side", "qty", "price", "value", "realized_pnl", "note")


def _out(line):
    """Print without dying on console codepages that lack the ₹ glyph."""
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode())


def load_trades(db=DB_PATH, symbol=None):
    """All trades from the paper broker's db (read-only), oldest first."""
    if not os.path.exists(db):
        return []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        sql = f"SELECT {', '.join(COLS)} FROM trades"
        params = ()
        if symbol:
            sql += " WHERE symbol=?"
            params = ((symbol or "").strip().upper(),)
        sql += " ORDER BY id"
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def summarize(trades):
    """Stats over the closed (sold) trades in `trades`."""
    closed = [t for t in trades if t.get("realized_pnl") is not None]
    pnls = [float(t["realized_pnl"]) for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    return {
        "trades": len(trades),                 # every row, buys included
        "closed": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "flat": len(pnls) - len(wins) - len(losses),
        "win_rate": (len(wins) / len(closed) * 100.0) if closed else 0.0,
        "realized": sum(pnls),
        "avg_win": (gross_win / len(wins)) if wins else 0.0,
        "avg_loss": (gross_loss / len(losses)) if losses else 0.0,
        "best": max(pnls) if pnls else 0.0,
        "worst": min(pnls) if pnls else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss
                         else (float("inf") if gross_win else 0.0),
    }


def per_symbol(trades):
    """{symbol: stats}, biggest realized P&L first."""
    symbols = sorted({t["symbol"] for t in trades})
    rows = {s: summarize([t for t in trades if t["symbol"] == s]) for s in symbols}
    return dict(sorted(rows.items(), key=lambda kv: kv[1]["realized"], reverse=True))


def _money(x):
    return f"{CURRENCY}{x:+,.2f}"


def _factor(x):
    return "inf" if x == float("inf") else f"{x:.2f}"


def print_stats(label, s):
    _out(f"{label}")
    _out(f"  closed trades {s['closed']:<6} wins {s['wins']:<6} losses {s['losses']:<6} "
         f"win rate {s['win_rate']:.1f}%")
    _out(f"  realized {_money(s['realized']):<14} avg win {_money(s['avg_win']):<14} "
         f"avg loss {_money(s['avg_loss'])}")
    _out(f"  best {_money(s['best']):<14} worst {_money(s['worst']):<14} "
         f"profit factor {_factor(s['profit_factor'])}")


def print_csv(trades, stats, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["scope", "closed", "wins", "losses", "win_rate_pct", "realized",
                "avg_win", "avg_loss", "best", "worst", "profit_factor"])
    for label, s in [("ALL", stats)] + list(rows.items()):
        w.writerow([label, s["closed"], s["wins"], s["losses"], f"{s['win_rate']:.1f}",
                    f"{s['realized']:.2f}", f"{s['avg_win']:.2f}", f"{s['avg_loss']:.2f}",
                    f"{s['best']:.2f}", f"{s['worst']:.2f}",
                    "inf" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"])
    _out(buf.getvalue().rstrip())


def build_parser():
    p = argparse.ArgumentParser(prog="backtest.py",
                                description="stats over the paper-trader's stored trade log")
    p.add_argument("--symbol", default=None, help="only this symbol (e.g. TCS.NS)")
    p.add_argument("--csv", action="store_true", help="print CSV instead of a report")
    p.add_argument("--db", default=DB_PATH, help="path to the state db")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    trades = load_trades(args.db, args.symbol)
    if not trades:
        _out(f"no trades stored yet{' for ' + args.symbol.upper() if args.symbol else ''} "
             f"— place a few paper trades first")
        return 0
    stats = summarize(trades)
    rows = per_symbol(trades) if not args.symbol else {}
    if args.csv:
        print_csv(trades, stats, rows)
        return 0
    label = f"{args.symbol.upper()} — {stats['trades']} trade rows" if args.symbol \
        else f"all symbols — {stats['trades']} trade rows"
    print_stats(label, stats)
    if rows:
        _out("")
        for sym, s in rows.items():
            _out(f"  {sym:<12} closed {s['closed']:<4} win rate {s['win_rate']:>5.1f}%  "
                 f"realized {_money(s['realized'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
