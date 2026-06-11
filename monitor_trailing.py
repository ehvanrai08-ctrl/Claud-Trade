"""
Trailing stop monitor — run continuously after tsla_strategy.py.
Checks price every 60s and:
  - Once up 10%: switches to trailing mode (stop = 5% below high water mark)
  - High water mark only moves up, never down
  - Cancels old stop order and places new one when floor rises
"""

import json
import time
import requests
from dotenv import dotenv_values

config = dotenv_values("/home/user/Claud-Trade/.env")
BASE_URL = config["ALPACA_BASE_URL"]
HEADERS = {
    "APCA-API-KEY-ID": config["ALPACA_API_KEY"],
    "APCA-API-SECRET-KEY": config["ALPACA_SECRET_KEY"],
    "Content-Type": "application/json",
}

STATE_FILE = "/home/user/Claud-Trade/strategy_state.json"
TRAIL_TRIGGER_PCT = 0.10
TRAIL_OFFSET_PCT = 0.05
CHECK_INTERVAL = 60  # seconds


def load_state():
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_latest_price(symbol):
    r = requests.get(
        f"https://data.alpaca.markets/v2/stocks/{symbol}/trades/latest",
        headers=HEADERS,
    )
    r.raise_for_status()
    return float(r.json()["trade"]["p"])


def cancel_order(order_id):
    requests.delete(f"{BASE_URL}/orders/{order_id}", headers=HEADERS)


def place_stop(symbol, qty, stop_price):
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json={
        "symbol": symbol,
        "qty": str(qty),
        "side": "sell",
        "type": "stop",
        "stop_price": str(round(stop_price, 2)),
        "time_in_force": "gtc",
    })
    r.raise_for_status()
    return r.json()


print("Trailing stop monitor running. Press Ctrl+C to stop.\n")

while True:
    state = load_state()
    symbol = state["symbol"]
    entry = state["entry_price"]
    hwm = state["high_water_mark"]
    current_stop = state["current_stop"]
    trailing_active = state["trailing_active"]
    stop_order_id = state["stop_order_id"]
    qty = state["entry_qty"]

    price = get_latest_price(symbol)
    print(f"[{time.strftime('%H:%M:%S')}] {symbol} ${price:.2f} | HWM ${hwm:.2f} | Stop ${current_stop:.2f} | Trailing: {trailing_active}")

    # Update high water mark
    if price > hwm:
        state["high_water_mark"] = price
        hwm = price

    # Activate trailing once up 10%
    if not trailing_active and price >= entry * (1 + TRAIL_TRIGGER_PCT):
        print(f"  → Trailing stop ACTIVATED at ${price:.2f} (+10% from entry)")
        state["trailing_active"] = True
        trailing_active = True

    if trailing_active:
        new_stop = round(hwm * (1 - TRAIL_OFFSET_PCT), 2)
        if new_stop > current_stop:
            print(f"  → Raising stop: ${current_stop:.2f} → ${new_stop:.2f}")
            cancel_order(stop_order_id)
            new_order = place_stop(symbol, qty, new_stop)
            state["stop_order_id"] = new_order["id"]
            state["current_stop"] = new_stop
            print(f"  → New stop order ID: {new_order['id']}")

    save_state(state)
    time.sleep(CHECK_INTERVAL)
