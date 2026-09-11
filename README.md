# paper-trader

A paper-trading engine with a Flask dashboard. yfinance for prices, SQLite for state, a 25% position cap and a daily ₹5k loss limit. The Broker interface is designed so a live broker (Zerodha/Alpaca) can be swapped in later.

## Run it

```bash
pip install -r requirements.txt
python webapp.py          # dashboard on http://127.0.0.1:8051
python cli.py --account   # or a quick read-only look from the terminal
python cli.py --history 10
```

Tests (no network needed, they patch the price lookup):

```bash
PYTHONPATH=. python -m unittest tests.test_broker tests.test_pricing tests.test_summary tests.test_sizing tests.test_session tests.test_interface
```

## Going live: the upgrade path

The webapp only ever calls the four `Broker` methods — `quote`, `buy`, `sell`,
`account` — so live trading is one new subclass plus a one-line swap in
`webapp.py`. Nothing in `templates/index.html` or the API layer changes.

1. **Write the broker.** Add e.g. `kite_broker.py` with
   `class KiteBroker(Broker)` (Alpaca/Binance are the same shape):
   - `quote(symbol)` → live last price, same dict keys as `PaperBroker.quote`
     (`symbol`, `price`, `cached`, `currency`, `ts`).
   - `buy(symbol, qty, note="")` / `sell(...)` → real orders; keep the `note`
     field and return `{"ok", "side", "symbol", "qty", "price", "value"}`
     (`sell` also returns `realized_pnl`).
   - `account()` → one snapshot dict with `cash`, `equity`, `total_pnl`,
     `daily_pnl`, `daily_loss_limit`, `kill_switch`, `positions`, `trades`,
     `market_value`, `as_of`. The dashboard reads those keys directly.
2. **Keep the errors readable.** Rejections must stay `TradeError` — the UI
   prints `str(err)` to the user verbatim, so "insufficient funds, need
   ₹12,300" beats a broker error code.
3. **Carry the guardrails over.** The 25% position cap, the daily loss limit,
   the no-shorting rule and the duplicate-order check live in the broker
   today. Either re-implement them in the new broker or lift them into a
   shared mixin — don't trust the broker's own risk engine to be your only
   net. Note that guardrails use `_safe_price()`, which falls back to the
   last known price when a quote call fails: keep that behaviour so a data
   outage can't silently disable a limit.
4. **Wire the kill switch first.** `set_kill_switch(True)` should come up
   before any order path is reachable, and the global `_check_duplicate`
   should stay enabled — it is the cheapest protection against a UI
   double-click turning into two real fills.
5. **Credentials stay out of git.** Read keys/tokens from the environment
   (`.env` is already gitignored, but export them for the webapp process) and
   validate every symbol with `validate_symbol()` before it reaches the
   exchange.
6. **Swap the line.** In `webapp.py`:
   `broker = KiteBroker()` instead of `PaperBroker()`. Then run against the
   broker's sandbox first (Kite Connect paper mode, Alpaca paper keys) with
   the kill switch on, before pointing it at funded money.

Rough order that has worked: paper → sandbox live with kill switch on →
sandbox live with the auto-trader on → small funded account → real size.
