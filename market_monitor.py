"""
Daily market-hours monitor for TSLA strategy.
- Runs every minute via cron (Mon-Fri 9:30-16:00 ET)
- Checks if market is open; exits silently if closed
- Updates trailing stop floor (only moves up, never down)
- Re-enters position if stop was hit and price recovers
- Logs all actions to monitor.log
"""

import json
import logging
import os
import requests
from datetime import datetime, timezone
from dotenv import dotenv_values
from zoneinfo import ZoneInfo

BASE_DIR = "/home/user/Claud-Trade"
config = dotenv_values(f"{BASE_DIR}/.env")

BASE_URL = config["ALPACA_BASE_URL"]
HEADERS = {
    "APCA-API-KEY-ID": config["ALPACA_API_KEY"],
    "APCA-API-SECRET-KEY": config["ALPACA_SECRET_KEY"],
    "Content-Type": "application/json",
}

TRAIL_TRIGGER_PCT = 0.10   # activate trailing once up 10% from entry
TRAIL_OFFSET_PCT  = 0.05   # trail stop sits 5% below running high
REENTRY_PCT       = 0.02   # re-enter if price recovers 2% above stop after a fill

logging.basicConfig(
    filename=f"{BASE_DIR}/monitor.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger()


# ── Helpers ───────────────────────────────────────────────────────────────────

def api_get(path):
    r = requests.get(f"{BASE_URL}{path}", headers=HEADERS)
    r.raise_for_status()
    return r.json()


def api_post(path, payload):
    r = requests.post(f"{BASE_URL}{path}", headers=HEADERS, json=payload)
    r.raise_for_status()
    return r.json()


def api_delete(path):
    requests.delete(f"{BASE_URL}{path}", headers=HEADERS)


def get_price(symbol):
    r = requests.get(
        f"https://data.alpaca.markets/v2/stocks/{symbol}/trades/latest",
        headers=HEADERS,
    )
    r.raise_for_status()
    return float(r.json()["trade"]["p"])


def load_state():
    with open(f"{BASE_DIR}/strategy_state.json") as f:
        return json.load(f)


def save_state(state):
    with open(f"{BASE_DIR}/strategy_state.json", "w") as f:
        json.dump(state, f, indent=2)


def market_is_open():
    data = api_get("/clock")
    return data["is_open"]


def get_order(order_id):
    try:
        return api_get(f"/orders/{order_id}")
    except Exception:
        return None


def get_position(symbol):
    try:
        return api_get(f"/positions/{symbol}")
    except Exception:
        return None


def place_stop(symbol, qty, stop_price):
    return api_post("/orders", {
        "symbol": symbol,
        "qty": str(qty),
        "side": "sell",
        "type": "stop",
        "stop_price": str(round(stop_price, 2)),
        "time_in_force": "gtc",
    })


def place_bracket_buy(symbol, qty, stop_price):
    return api_post("/orders", {
        "symbol": symbol,
        "qty": str(qty),
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "order_class": "bracket",
        "stop_loss": {"stop_price": str(round(stop_price, 2))},
        "take_profit": {"limit_price": "9999.00"},
    })


# ── Main logic ────────────────────────────────────────────────────────────────

def run():
    if not market_is_open():
        return  # silent exit outside market hours

    state = load_state()
    symbol        = state["symbol"]
    entry_price   = state["entry_price"]
    entry_qty     = state["entry_qty"]
    stop_order_id = state["stop_order_id"]
    current_stop  = state["current_stop"]
    trailing      = state["trailing_active"]
    hwm           = state["high_water_mark"]

    price = get_price(symbol)

    # Update high water mark
    if price > hwm:
        state["high_water_mark"] = price
        hwm = price

    # Check if we still have a position
    position = get_position(symbol)

    # ── Stop was hit: no position left ───────────────────────────────────────
    if position is None:
        stop_order = get_order(stop_order_id)
        if stop_order and stop_order["status"] == "filled":
            fill = float(stop_order.get("filled_avg_price") or current_stop)
            log.info(f"STOP HIT: sold {entry_qty} {symbol} @ ${fill:.2f}")
            print(f"[STOP HIT] Sold {entry_qty} {symbol} @ ${fill:.2f}")

            # Re-enter if price has recovered 2% above the stop
            if price >= fill * (1 + REENTRY_PCT):
                new_stop = round(price * (1 - TRAIL_OFFSET_PCT), 2)
                order = place_bracket_buy(symbol, entry_qty, new_stop)
                log.info(f"RE-ENTRY: bought {entry_qty} {symbol} @ market, new stop ${new_stop:.2f} | order {order['id']}")
                print(f"[RE-ENTRY] Bought {entry_qty} {symbol} @ market | stop ${new_stop:.2f}")
                state["entry_price"]   = price
                state["stop_order_id"] = order["legs"][1]["id"]  # stop leg
                state["current_stop"]  = new_stop
                state["high_water_mark"] = price
                state["trailing_active"] = True
        save_state(state)
        return

    # ── Activate trailing once up 10% ────────────────────────────────────────
    if not trailing and price >= entry_price * (1 + TRAIL_TRIGGER_PCT):
        log.info(f"TRAILING ACTIVATED: {symbol} ${price:.2f} (+{((price/entry_price)-1)*100:.1f}%)")
        print(f"[TRAILING ON] {symbol} ${price:.2f}")
        state["trailing_active"] = True
        trailing = True

    # ── Raise the floor if trailing is active ────────────────────────────────
    if trailing:
        new_stop = round(hwm * (1 - TRAIL_OFFSET_PCT), 2)
        if new_stop > current_stop:
            api_delete(f"/orders/{stop_order_id}")
            new_order = place_stop(symbol, entry_qty, new_stop)
            log.info(f"STOP RAISED: ${current_stop:.2f} → ${new_stop:.2f} | new order {new_order['id']}")
            print(f"[STOP RAISED] ${current_stop:.2f} → ${new_stop:.2f}")
            state["stop_order_id"] = new_order["id"]
            state["current_stop"]  = new_stop

    log.info(f"TICK: {symbol} ${price:.2f} | HWM ${hwm:.2f} | Stop ${state['current_stop']:.2f} | Trailing: {trailing}")
    save_state(state)


if __name__ == "__main__":
    run()
