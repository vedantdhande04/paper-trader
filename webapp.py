"""PaperTrader webapp — dashboard + trade API on top of the Broker interface."""
from flask import Flask, jsonify, render_template, request

from autotrader import AutoTrader
from engine import DEFAULT_CASH, PaperBroker, TradeError

app = Flask(__name__)
broker = PaperBroker()   # swap for a LiveBroker(...) here when going live
auto = AutoTrader(broker)
auto.apply_config({})    # resume auto mode from saved config (starts loop if enabled)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/account")
def api_account():
    return jsonify(broker.account())


@app.get("/api/quote")
def api_quote():
    symbol = request.args.get("symbol", "").strip()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    try:
        return jsonify(broker.quote(symbol))
    except TradeError as e:
        return jsonify({"error": str(e)}), 404


@app.post("/api/trade")
def api_trade():
    data = request.get_json(force=True)
    side = (data.get("side") or "").upper()
    symbol = (data.get("symbol") or "").strip()
    try:
        qty = float(data.get("qty", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "qty must be a number"}), 400
    if side not in ("BUY", "SELL"):
        return jsonify({"error": "side must be BUY or SELL"}), 400
    try:
        result = (broker.buy if side == "BUY" else broker.sell)(symbol, qty)
        return jsonify(result)
    except TradeError as e:
        return jsonify({"error": str(e)}), 400


@app.post("/api/kill")
def api_kill():
    data = request.get_json(force=True)
    broker.set_kill_switch(bool(data.get("on")))
    return jsonify({"kill_switch": broker.account()["kill_switch"]})


@app.post("/api/refresh")
def api_refresh():
    return jsonify(broker.clear_price_cache())


@app.post("/api/reset")
def api_reset():
    """Re-allocate capital: close all positions, set balance to `capital`."""
    data = request.get_json(force=True)
    try:
        return jsonify(broker.reset(float(data.get("capital", DEFAULT_CASH))))
    except (TradeError, ValueError) as e:
        return jsonify({"error": str(e)}), 400


# ------------------------------------------------------------- auto-trader
@app.get("/api/auto")
def api_auto():
    return jsonify(auto.status())


@app.post("/api/auto")
def api_auto_update():
    data = request.get_json(force=True)
    try:
        auto.apply_config(data)
        return jsonify(auto.status())
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.post("/api/auto/run-now")
def api_auto_run():
    try:
        return jsonify(auto.tick_now())
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8051, debug=False)
