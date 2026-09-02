"""Auto-trader: signal strategies + a background loop that trades on its own.

Uses ONLY the Broker interface, so the same bot works unchanged on a live
backend later. Every auto trade is tagged note="AUTO" in the trade log.
"""
import datetime as dt
import threading

import yfinance as yf

from engine import position_size

STRATEGIES = {"sma": "SMA crossover (golden/death cross, 1h bars)",
              "rsi": "RSI reversal (buy <30, sell >70, 1h bars)"}


def fetch_history(symbol, interval="1h", period="30d"):
    df = yf.Ticker(symbol).history(interval=interval, period=period,
                                   auto_adjust=True)
    if df is None or df.empty:
        raise ValueError("no price history returned")
    return df


def sma_series(df, fast=8, slow=21):
    return (df["Close"].rolling(fast).mean(),
            df["Close"].rolling(slow).mean())


def rsi_series(df, period=14):
    delta = df["Close"].diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss
    return 100 - 100 / (1 + rs)


def compute_signal(strategy, df):
    """Return (signal, detail) where signal ∈ BUY | SELL | HOLD."""
    strategy = (strategy or "sma").lower()
    if strategy == "rsi":
        r = rsi_series(df, 14)
        last = float(r.iloc[-1])
        if last < 30:
            return "BUY", f"RSI {last:.0f} oversold"
        if last > 70:
            return "SELL", f"RSI {last:.0f} overbought"
        return "HOLD", f"RSI {last:.0f}"

    f, s = sma_series(df, 8, 21)
    if len(f) < 2 or f.iloc[-1] != f.iloc[-1] or f.iloc[-2] != f.iloc[-2]:
        return "HOLD", "warming up"
    cross_up = f.iloc[-2] <= s.iloc[-2] and f.iloc[-1] > s.iloc[-1]
    cross_dn = f.iloc[-2] >= s.iloc[-2] and f.iloc[-1] < s.iloc[-1]
    if cross_up:
        return "BUY", "golden cross"
    if cross_dn:
        return "SELL", "death cross"
    return ("HOLD", "uptrend") if f.iloc[-1] > s.iloc[-1] else ("HOLD", "downtrend")


class AutoTrader:
    def __init__(self, broker):
        self.broker = broker
        self._thread = None
        self._stop = threading.Event()
        self._tick_lock = threading.Lock()
        self.last_status = {}
        self.last_check = None

    # ------------------------------------------------------------- lifecycle
    def config(self):
        return self.broker.get_auto_config()

    def apply_config(self, updates):
        merged = self.broker.set_auto_config(updates)
        self._sync_thread(merged)
        return merged

    def _sync_thread(self, cfg=None):
        cfg = cfg or self.config()
        if cfg["enabled"] and (self._thread is None or not self._thread.is_alive()):
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        elif not cfg["enabled"] and self._thread and self._thread.is_alive():
            self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            with self._tick_lock:
                try:
                    self._tick()
                except Exception as e:  # never let the loop die
                    self.last_status = {"_error": str(e)[:200]}
                    self.last_check = dt.datetime.now().isoformat(timespec="seconds")
            self._stop.wait(max(30, self.config()["interval_min"] * 60))

    # ------------------------------------------------------------------ tick
    def _tick(self):
        cfg = self.config()
        status = {}
        for raw in cfg.get("symbols", []):
            raw = raw.strip()
            if not raw:
                continue
            try:
                q = self.broker.quote(raw)
                sym = q["symbol"]
                df = fetch_history(sym)
                sig, detail = compute_signal(cfg["strategy"], df)
                pos = self.broker.position(sym)
                action = "none"

                if sig == "BUY" and not pos:
                    equity = self.broker.account()["equity"]
                    qty = position_size(equity, q["price"], cfg["position_pct"])
                    if qty >= 1e-9:
                        self.broker.buy(sym, qty, note="AUTO")
                        action = f"bought {qty:.6g}"
                elif sig == "SELL" and pos:
                    self.broker.sell(sym, pos["qty"], note="AUTO")
                    action = f"sold {pos['qty']:.6g}"

                status[sym] = {"signal": sig, "detail": detail, "action": action,
                               "price": q["price"]}
            except Exception as e:
                status[raw] = {"signal": "ERR", "detail": str(e)[:100], "action": "none"}
        self.last_status = status
        self.last_check = dt.datetime.now().isoformat(timespec="seconds")

    def tick_now(self):
        """Run one signal pass immediately (used by 'Check now')."""
        if not self.config().get("enabled"):
            raise ValueError("Auto-trader is disabled — enable it first.")
        with self._tick_lock:
            self._tick()
        return self.status()

    def status(self):
        cfg = self.config()
        return {"config": cfg,
                "running": bool(self._thread and self._thread.is_alive()),
                "last_check": self.last_check,
                "status": self.last_status,
                "strategies": STRATEGIES}
