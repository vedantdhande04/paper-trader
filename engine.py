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
import csv
import datetime as dt
import io
import json
import re
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

MARKET_OPEN = dt.time(9, 15)   # IST — the daily loss counter rolls here


def session_date(now=None):
    """ISO date of the trading session `now` belongs to.

    Sessions roll at market open (09:15 IST) instead of midnight, so the
    overnight gap after 15:30 doesn't eat into the next day's loss allowance.
    """
    now = now or dt.datetime.now()
    day = now.date()
    if now.time() < MARKET_OPEN:
        day -= dt.timedelta(days=1)
    return day.isoformat()


SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,19}$")


def validate_symbol(symbol):
    """Normalize + syntax-check a symbol before it hits the network."""
    symbol = (symbol or "").strip().upper()
    if not SYMBOL_RE.match(symbol):
        raise TradeError(
            f"Invalid symbol '{symbol[:20] or '(empty)'}' — letters, digits, "
            f"dots and dashes only, e.g. TCS.NS or BTC-INR.")
    return symbol


def position_size(equity, price, pct=20.0):
    """Order qty for `pct`% of equity, capped by the max position guardrail.

    `pct` is a percentage of current equity; the result is clamped to
    MAX_POSITION_PCT so a fat-fingered pct can't trip the broker's own cap.
    """
    pct = min(100.0, max(0.0, float(pct)))
    if price <= 0:
        raise TradeError("Price must be positive to size a position.")
    cap = MAX_POSITION_PCT * 100.0
    budget = equity * min(pct, cap) / 100.0
    return budget / price


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
            today = session_date()
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

    # -------------------------------------------------------- order guards
    def _check_duplicate(self, symbol, side, qty):
        """Reject an exact re-submit of the last order within a minute.

        Catches double-clicks and retried HTTP posts. A different quantity
        or the opposite side (e.g. a quick round trip) is never blocked.
        """
        c = self._conn()
        try:
            row = c.execute(
                "SELECT side, qty, ts FROM trades WHERE symbol=? "
                "ORDER BY id DESC LIMIT 1", (symbol,)).fetchone()
        finally:
            c.close()
        if not row or row["side"] != side or abs(row["qty"] - qty) > 1e-9:
            return
        try:
            age = (dt.datetime.now()
                   - dt.datetime.fromisoformat(row["ts"])).total_seconds()
        except (TypeError, ValueError):
            return
        if 0 <= age < 60:
            raise TradeError(
                f"A {side} of {qty:g} {symbol} just filled — looks like a "
                f"duplicate, wait a minute before retrying.")

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
        """On a new session (market-open roll), snapshot starting equity."""
        today = session_date()
        if self._get_meta(c, "day") != today:
            prices = {s: self.quote(s)["price"] for s in
                      [r["symbol"] for r in c.execute("SELECT symbol FROM positions")]}
            c.execute("UPDATE meta SET v=? WHERE k='day_start_equity'",
                      (str(self._equity_at(c, prices)),))
            c.execute("UPDATE meta SET v=? WHERE k='day'", (today,))
            c.commit()

    # ---------------------------------------------------------------- trades
    def buy(self, symbol, qty, note=""):
        symbol = validate_symbol(symbol)
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

                # position size guardrail: the resulting holding in this
                # symbol (existing qty + this order) must stay under the cap
                held_row = c.execute("SELECT COALESCE(qty, 0) FROM positions "
                                     "WHERE symbol=?", (resolved,)).fetchone()
                held_qty = held_row[0] if held_row else 0.0
                if (held_qty + qty) * price > MAX_POSITION_PCT * equity:
                    raise TradeError(
                        f"Position in {resolved} would be "
                        f"{(held_qty + qty) * price / equity:.0%} of equity — "
                        f"max allowed is {MAX_POSITION_PCT:.0%}. Reduce qty.")

                self._check_duplicate(resolved, "BUY", qty)

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
        symbol = validate_symbol(symbol)
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

                self._check_duplicate(resolved, "SELL", qty)

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
                today = session_date()
                for k in ("cash", "start_cash", "day_start_equity"):
                    c.execute("UPDATE meta SET v=? WHERE k=?", (str(capital), k))
                c.execute("UPDATE meta SET v=? WHERE k='day'", (today,))
                c.commit()
                return self.account()
            finally:
                c.close()

    def top_up(self, amount):
        """Deposit paper cash; raises cost basis too so P&L stays honest.

        cash / start_cash / day_start_equity all move by `amount`, so
        neither total P&L nor today's P&L is distorted by the deposit.
        """
        amount = float(amount)
        if amount <= 0:
            raise TradeError("Top-up amount must be positive.")
        with self.lock:
            c = self._conn()
            try:
                self._roll_day(c)
                for k in ("cash", "start_cash", "day_start_equity"):
                    cur = float(self._get_meta(c, k))
                    c.execute("UPDATE meta SET v=? WHERE k=?",
                              (str(cur + amount), k))
                c.commit()
            finally:
                c.close()
        return self.account()

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

    # ---------------------------------------------------------------- export
    def export_trades(self, path=None):
        """Trade history as CSV text; also writes to `path` if given."""
        with self.lock:
            c = self._conn()
            try:
                rows = c.execute(
                    "SELECT ts, symbol, side, qty, price, value, realized_pnl, note "
                    "FROM trades ORDER BY id").fetchall()
            finally:
                c.close()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["ts", "symbol", "side", "qty", "price", "value",
                    "realized_pnl", "note"])
        for r in rows:
            w.writerow([r["ts"], r["symbol"], r["side"], r["qty"], r["price"],
                        r["value"], r["realized_pnl"], r["note"]])
        text = buf.getvalue()
        if path:
            with open(path, "w", newline="") as f:
                f.write(text)
        return text
