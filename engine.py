"""PaperTrader engine.

Broker-agnostic core: the webapp talks ONLY to the `Broker` interface.
`PaperBroker` is one implementation (virtual cash, live prices via yfinance).
A future live-money backend (Zerodha Kite / Alpaca / Binance) implements the
same interface and can be swapped in without touching the UI.

Guardrails baked in:
  - daily loss limit (new buys blocked once today's P&L breaches it)
  - max position size as % of current equity
  - kill switch (master read-only)
  - no shorting: you can only sell what you hold
"""
import datetime as dt
import json
import sqlite3
import threading
import time

import yfinance as yf

DB_PATH = "state.db"
DEFAULT_CASH = 100_000.0
CURRENCY = "₹"
DAILY_LOSS_LIMIT = 5_000.0   # rupees: block new buys if today's P&L <= -limit
MAX_POSITION_PCT = 0.25      # max 25% of equity per symbol
PRICE_CACHE_TTL = 60         # seconds; avoids hammering Yahoo

AUTO_CONFIG_DEFAULTS = {
    "enabled": False,
    "strategy": "sma",               # "sma" (crossover) or "rsi" (reversal)
    "interval_min": 5,               # signal check cadence
    "symbols": ["BTC-INR", "ETH-INR", "SOL-INR", "TCS.NS", "RELIANCE.NS"],
    "position_pct": 20,              # % of equity per auto-buy (max 25)
}


class TradeError(Exception):
    """A rejected order (insufficient cash, guardrail, unknown symbol...)."""


class Broker:
    """Interface every execution backend (paper or live) must implement."""

    def quote(self, symbol):
        raise NotImplementedError

    def buy(self, symbol, qty, note=""):
        raise NotImplementedError

    def sell(self, symbol, qty, note=""):
        raise NotImplementedError

    def account(self):
        raise NotImplementedError


class PaperBroker(Broker):
    def __init__(self, db=DB_PATH):
        self.db = db
        self.lock = threading.RLock()   # RLock so internal calls can nest
        self._price_cache = {}
        self._init_db()

    # ------------------------------------------------------------- db setup
    def _conn(self):
        c = sqlite3.connect(self.db)
        c.row_factory = sqlite3.Row
        return c

    def _init_db(self):
        c = self._conn()
        c.executescript("""
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS positions (
            symbol TEXT PRIMARY KEY, qty REAL, avg_price REAL, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, symbol TEXT, side TEXT, qty REAL, price REAL,
            value REAL, realized_pnl REAL, note TEXT);
        """)
        cash = self._get_meta(c, "cash")
        if cash is None:
            today = dt.date.today().isoformat()
            c.execute(
                "INSERT INTO meta (k,v) VALUES "
                "('cash',?),('start_cash',?),('day_start_equity',?),('day',?),"
                "('daily_loss_limit',?),('kill_switch',?)",
                (str(DEFAULT_CASH), str(DEFAULT_CASH), str(DEFAULT_CASH),
                 today, str(DAILY_LOSS_LIMIT), "0"))
        c.commit()
        c.close()

    @staticmethod
    def _get_meta(c, key):
        row = c.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return row["v"] if row else None

    def _set_meta(self, key, value):
        c = self._conn()
        c.execute("INSERT INTO meta (k,v) VALUES (?,?) "
                  "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, str(value)))
        c.commit()
        c.close()

    # ------------------------------------------------------------ price data
    def _resolve(self, symbol):
        """Try the symbol as-is, then NSE (.NS) / BSE (.BO) suffixes."""
        symbol = symbol.strip().upper()
        candidates = [symbol]
        if not symbol.endswith((".NS", ".BO")) and "-" not in symbol:
            candidates += [symbol + ".NS", symbol + ".BO"]
        for cand in candidates:
            try:
                t = yf.Ticker(cand)
                px = t.fast_info
                last = float(px.last_price)
                if last and last > 0:
                    return cand, last
            except Exception:
                continue
        raise TradeError(f"Unknown symbol '{symbol}' — try RELIANCE.NS, TCS.NS, AAPL, BTC-INR...")

    def quote(self, symbol):
        """Live price with a short cache. Returns dict for the UI."""
        symbol = symbol.strip().upper()
        now = time.time()
        cached = self._price_cache.get(symbol)
        if cached and now - cached[1] < PRICE_CACHE_TTL:
            return {"symbol": symbol, "price": cached[0], "cached": True,
                    "currency": CURRENCY, "ts": dt.datetime.now().isoformat(timespec="seconds")}
        resolved, price = self._resolve(symbol)
        self._price_cache[resolved] = (price, now)
        return {"symbol": resolved, "price": price, "cached": False,
                "currency": CURRENCY, "ts": dt.datetime.now().isoformat(timespec="seconds")}

    def clear_price_cache(self):
        self._price_cache.clear()
        return {"cleared": True}

    def position(self, symbol):
        """Current holding for a symbol, or None."""
        with self.lock:
            c = self._conn()
            try:
                resolved, _ = self._resolve(symbol)
                row = c.execute("SELECT * FROM positions WHERE symbol=?",
                                (resolved,)).fetchone()
                return dict(row) if row else None
            finally:
                c.close()

    # ------------------------------------------------------------- accounting
    def _equity_at(self, c, prices):
        """cash + market value of all positions using given {symbol: price}."""
        cash = float(self._get_meta(c, "cash"))
        mv = 0.0
        for p in c.execute("SELECT symbol, qty FROM positions"):
            mv += p["qty"] * prices.get(p["symbol"], 0.0)
        return cash + mv

    def _roll_day(self, c):
        """On a new day, snapshot starting equity for the daily-loss guardrail."""
        today = dt.date.today().isoformat()
        if self._get_meta(c, "day") != today:
            prices = {s: self.quote(s)["price"] for s in
                      [r["symbol"] for r in c.execute("SELECT symbol FROM positions")]}
            c.execute("UPDATE meta SET v=? WHERE k='day_start_equity'",
                      (str(self._equity_at(c, prices)),))
            c.execute("UPDATE meta SET v=? WHERE k='day'", (today,))
            c.commit()

    # ---------------------------------------------------------------- trades
    def buy(self, symbol, qty, note=""):
        qty = float(qty)
        if qty <= 0:
            raise TradeError("Quantity must be positive.")
        with self.lock:
            c = self._conn()
            try:
                if self._get_meta(c, "kill_switch") == "1":
                    raise TradeError("Kill switch is ON — trading is paused.")
                resolved, price = self._resolve(symbol)
                self._price_cache[resolved] = (price, time.time())
                self._roll_day(c)

                cash = float(self._get_meta(c, "cash"))
                value = price * qty
                if value > cash + 1e-6:
                    raise TradeError(f"Insufficient cash: need {CURRENCY}{value:,.2f}, "
                                     f"have {CURRENCY}{cash:,.2f}.")

                # daily loss guardrail
                prices = {r["symbol"]: self.quote(r["symbol"])["price"]
                          for r in c.execute("SELECT symbol FROM positions")}
                prices[resolved] = price
                equity = self._equity_at(c, prices)
                day_start = float(self._get_meta(c, "day_start_equity"))
                daily_pnl = equity - day_start
                limit = float(self._get_meta(c, "daily_loss_limit"))
                if daily_pnl <= -limit:
                    raise TradeError(
                        f"Daily loss limit hit (today {CURRENCY}{daily_pnl:,.2f} "
                        f"<= -{CURRENCY}{limit:,.0f}). New buys blocked — "
                        f"reset the limit or wait for tomorrow.")

                # position size guardrail
                if value > MAX_POSITION_PCT * equity:
                    raise TradeError(
                        f"Position would be {value/equity:.0%} of equity — "
                        f"max allowed is {MAX_POSITION_PCT:.0%}. Reduce qty.")

                # execute
                pos = c.execute("SELECT * FROM positions WHERE symbol=?",
                                (resolved,)).fetchone()
                if pos:
                    new_qty = pos["qty"] + qty
                    new_avg = (pos["qty"] * pos["avg_price"] + value) / new_qty
                    c.execute("UPDATE positions SET qty=?, avg_price=?, updated_at=? "
                              "WHERE symbol=?",
                              (new_qty, new_avg, dt.datetime.now().isoformat(), resolved))
                else:
                    c.execute("INSERT INTO positions (symbol, qty, avg_price, updated_at) "
                              "VALUES (?,?,?,?)",
                              (resolved, qty, price, dt.datetime.now().isoformat()))

                c.execute("UPDATE meta SET v=? WHERE k='cash'", (str(cash - value),))
                c.execute("INSERT INTO trades (ts, symbol, side, qty, price, value, realized_pnl, note) "
                          "VALUES (?,?,?,?,?,?,?,?)",
                          (dt.datetime.now().isoformat(timespec="seconds"), resolved, "BUY",
                           qty, price, value, None, note))
                c.commit()
                return {"ok": True, "side": "BUY", "symbol": resolved, "qty": qty,
                        "price": price, "value": value}
            except TradeError:
                c.rollback()
                raise
            finally:
                c.close()

    def sell(self, symbol, qty, note=""):
        qty = float(qty)
        if qty <= 0:
            raise TradeError("Quantity must be positive.")
        with self.lock:
            c = self._conn()
            try:
                if self._get_meta(c, "kill_switch") == "1":
                    raise TradeError("Kill switch is ON — trading is paused.")
                resolved, price = self._resolve(symbol)
                self._price_cache[resolved] = (price, time.time())

                pos = c.execute("SELECT * FROM positions WHERE symbol=?",
                                (resolved,)).fetchone()
                if not pos:
                    raise TradeError(f"You don't hold {resolved}.")
                if qty > pos["qty"] + 1e-6:
                    raise TradeError(f"You hold {pos['qty']:g} of {resolved}, "
                                     f"can't sell {qty:g}.")

                cash = float(self._get_meta(c, "cash"))
                value = price * qty
                realized = (price - pos["avg_price"]) * qty
                new_qty = pos["qty"] - qty
                if new_qty < 1e-9:
                    c.execute("DELETE FROM positions WHERE symbol=?", (resolved,))
                else:
                    c.execute("UPDATE positions SET qty=?, updated_at=? WHERE symbol=?",
                              (new_qty, dt.datetime.now().isoformat(), resolved))

                c.execute("UPDATE meta SET v=? WHERE k='cash'", (str(cash + value),))
                c.execute("INSERT INTO trades (ts, symbol, side, qty, price, value, realized_pnl, note) "
                          "VALUES (?,?,?,?,?,?,?,?)",
                          (dt.datetime.now().isoformat(timespec="seconds"), resolved, "SELL",
                           qty, price, value, realized, note))
                c.commit()
                return {"ok": True, "side": "SELL", "symbol": resolved, "qty": qty,
                        "price": price, "value": value, "realized_pnl": realized}
            except TradeError:
                c.rollback()
                raise
            finally:
                c.close()

    def set_kill_switch(self, on):
        self._set_meta("kill_switch", "1" if on else "0")
        return {"kill_switch": on}

    def reset(self, capital):
        """Re-allocate capital: close all positions, set balance to `capital`."""
        capital = float(capital)
        if capital < 1000:
            raise TradeError("Minimum capital is ₹1,000.")
        with self.lock:
            c = self._conn()
            try:
                c.execute("DELETE FROM positions")
                today = dt.date.today().isoformat()
                for k in ("cash", "start_cash", "day_start_equity"):
                    c.execute("UPDATE meta SET v=? WHERE k=?", (str(capital), k))
                c.execute("UPDATE meta SET v=? WHERE k='day'", (today,))
                c.commit()
                return self.account()
            finally:
                c.close()

    # --------------------------------------------------------- auto-trader cfg
    def get_auto_config(self):
        c = self._conn()
        raw = self._get_meta(c, "auto_config")
        c.close()
        cfg = json.loads(raw) if raw else {}
        merged = {**AUTO_CONFIG_DEFAULTS, **cfg}
        return merged

    def set_auto_config(self, updates):
        cur = self.get_auto_config()
        cur.update(updates or {})
        cur["enabled"] = bool(cur.get("enabled"))
        cur["interval_min"] = max(1, int(cur.get("interval_min", 5)))
        cur["position_pct"] = min(25.0, max(1.0, float(cur.get("position_pct", 20))))
        cur["strategy"] = cur.get("strategy") if cur.get("strategy") in ("sma", "rsi") else "sma"
        syms = cur.get("symbols", [])
        if isinstance(syms, str):
            syms = [s for s in syms.split(",") if s.strip()]
        cur["symbols"] = [s.strip().upper() for s in syms if s.strip()]
        self._set_meta("auto_config", json.dumps(cur))
        return cur

    # -------------------------------------------------------------- snapshot
    def account(self):
        with self.lock:
            c = self._conn()
            try:
                self._roll_day(c)
                symbols = [r["symbol"] for r in c.execute("SELECT symbol FROM positions")]
                prices = {}
                for s in symbols:
                    try:
                        prices[s] = self.quote(s)["price"]
                    except TradeError:
                        prices[s] = 0.0

                cash = float(self._get_meta(c, "cash"))
                start = float(self._get_meta(c, "start_cash"))
                day_start = float(self._get_meta(c, "day_start_equity"))
                limit = float(self._get_meta(c, "daily_loss_limit"))

                positions = []
                mv_total = 0.0
                for p in c.execute("SELECT * FROM positions ORDER BY symbol"):
                    last = prices.get(p["symbol"], 0.0)
                    mv = p["qty"] * last
                    mv_total += mv
                    positions.append({
                        "symbol": p["symbol"], "qty": p["qty"],
                        "avg_price": round(p["avg_price"], 4),
                        "last_price": round(last, 4),
                        "market_value": round(mv, 2),
                        "unrealized_pnl": round((last - p["avg_price"]) * p["qty"], 2),
                        "unrealized_pct": round((last / p["avg_price"] - 1) * 100, 2)
                        if p["avg_price"] else 0.0,
                    })

                equity = cash + mv_total
                realized = float(c.execute(
                    "SELECT COALESCE(SUM(realized_pnl),0) FROM trades "
                    "WHERE realized_pnl IS NOT NULL").fetchone()[0])
                trades = [dict(r) for r in c.execute(
                    "SELECT * FROM trades ORDER BY id DESC LIMIT 50")]
                for t in trades:
                    if t["realized_pnl"] is not None:
                        t["realized_pnl"] = round(t["realized_pnl"], 2)
                    t["value"] = round(t["value"], 2)
                    t["price"] = round(t["price"], 4)

                return {
                    "cash": round(cash, 2),
                    "equity": round(equity, 2),
                    "start_cash": start,
                    "total_pnl": round(equity - start, 2),
                    "total_pnl_pct": round((equity / start - 1) * 100, 2) if start else 0.0,
                    "day_start_equity": round(day_start, 2),
                    "daily_pnl": round(equity - day_start, 2),
                    "daily_loss_limit": limit,
                    "kill_switch": self._get_meta(c, "kill_switch") == "1",
                    "currency": CURRENCY,
                    "positions": positions,
                    "trades": trades,
                    "market_value": round(mv_total, 2),
                    "as_of": dt.datetime.now().isoformat(timespec="seconds"),
                }
            finally:
                c.close()
