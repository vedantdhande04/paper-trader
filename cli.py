"""Small CLI for the paper-trader, for when the dashboard is overkill.

    python cli.py --history        # last 20 trades
    python cli.py --history 5      # last 5
    python cli.py --account        # cash, equity and open positions

Read-only: this never places an order.
"""
import argparse

from engine import CURRENCY, PaperBroker, TradeError

HISTORY_DEFAULT = 20


def _out(line):
    """Print without dying on console codepages that lack the ₹ glyph."""
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode())


def build_parser():
    p = argparse.ArgumentParser(
        prog="cli.py", description="paper-trader CLI (read-only)")
    p.add_argument("--account", action="store_true",
                   help="show cash, equity, daily P&L and open positions")
    p.add_argument("--history", nargs="?", type=int, const=HISTORY_DEFAULT,
                   default=None, metavar="N",
                   help=f"show the last N trades (default {HISTORY_DEFAULT})")
    return p


def print_account(acc):
    _out(f"cash      {CURRENCY}{acc['cash']:,.2f}")
    _out(f"equity    {CURRENCY}{acc['equity']:,.2f}")
    _out(f"today     {CURRENCY}{acc['daily_pnl']:+,.2f}  "
         f"(limit -{CURRENCY}{acc['daily_loss_limit']:,.0f})")
    _out(f"total     {CURRENCY}{acc['total_pnl']:+,.2f}  "
         f"({acc['total_pnl_pct']:+.2f}%)")
    _out(f"killsig   {'ON' if acc['kill_switch'] else 'off'}")
    if not acc["positions"]:
        _out("no open positions")
        return
    _out("open positions:")
    for p in acc["positions"]:
        _out(f"  {p['symbol']:<12} qty {p['qty']:>10g}  avg {p['avg_price']:>10,.2f}  "
             f"last {p['last_price']:>10,.2f}  pnl {CURRENCY}{p['unrealized_pnl']:+,.2f}")


def print_history(acc, n):
    trades = acc["trades"][:max(0, n)]
    if not trades:
        _out("no trades yet")
        return
    _out(f"{'when':<20}{'side':<6}{'symbol':<12}{'qty':>10}  {'price':>10}  "
         f"{'value':>12}  note")
    for t in trades:
        _out(f"{t['ts']:<20}{t['side']:<6}{t['symbol']:<12}{t['qty']:>10g}  "
             f"{t['price']:>10,.2f}  {t['value']:>12,.2f}  {t['note'] or '-'}")
    _out(f"({len(trades)} shown; the dashboard keeps the last 50)")


def main(argv=None):
    args = build_parser().parse_args(argv)
    broker = PaperBroker()
    try:
        acc = broker.account()
    except TradeError as e:
        _out(f"error: {e}")
        return 1

    if args.account:
        print_account(acc)
    if args.history is not None:
        if args.account:
            _out("")
        print_history(acc, args.history)
    if not args.account and args.history is None:
        print_account(acc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
