"""
TSLA Trailing Stop Strategy
- Entry: Buy 10 shares at market
- Stop loss: -10% from entry price
- Trailing floor: once up 10%, trail stop at 5% below current high (floor only moves up)
- Ladder buys: -20% → buy 20 more shares | -30% → buy 10 more shares
"""

import os
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

SYMBOL = "TSLA"
ENTRY_QTY = 10
STOP_LOSS_PCT = 0.10       # -10% hard floor
TRAIL_TRIGGER_PCT = 0.10   # activate trailing once up 10%
TRAIL_OFFSET_PCT = 0.05    # trail stop sits 5% below running high
LADDER_20_QTY = 20         # buy 20 shares if down 20%
LADDER_30_QTY = 10         # buy 10 shares if down 30%


def place_order(payload):
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json=payload)
    r.raise_for_status()
    return r.json()


def get_order(order_id):
    r = requests.get(f"{BASE_URL}/orders/{order_id}", headers=HEADERS)
    r.raise_for_status()
    return r.json()


def cancel_order(order_id):
    requests.delete(f"{BASE_URL}/orders/{order_id}", headers=HEADERS)


def get_latest_price(symbol):
    r = requests.get(
        f"https://data.alpaca.markets/v2/stocks/{symbol}/trades/latest",
        headers=HEADERS,
    )
    r.raise_for_status()
    return float(r.json()["trade"]["p"])


# ── Step 1: Market buy ────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print("STEP 1 — Placing market buy: 10 shares of {SYMBOL}".format(SYMBOL=SYMBOL))
buy_order = place_order({
    "symbol": SYMBOL,
    "qty": str(ENTRY_QTY),
    "side": "buy",
    "type": "market",
    "time_in_force": "day",
})
print(f"  Order ID : {buy_order['id']}")
print(f"  Status   : {buy_order['status']}")
print(f"  Qty      : {buy_order['qty']}")
print(f"  Side     : {buy_order['side']}")
print(f"  Type     : {buy_order['type']}")

# ── Wait for fill ─────────────────────────────────────────────────────────────
print("\nWaiting for fill...")
for _ in range(30):
    o = get_order(buy_order["id"])
    if o["status"] == "filled":
        break
    time.sleep(1)

entry_price = float(o.get("filled_avg_price") or get_latest_price(SYMBOL))
print(f"  Fill price: ${entry_price:.2f}")

# ── Step 2: Stop loss order ───────────────────────────────────────────────────
stop_price = round(entry_price * (1 - STOP_LOSS_PCT), 2)
print(f"\n{'='*60}")
print(f"STEP 2 — Placing initial stop-loss sell @ ${stop_price:.2f} (-10%)")
stop_order = place_order({
    "symbol": SYMBOL,
    "qty": str(ENTRY_QTY),
    "side": "sell",
    "type": "stop",
    "stop_price": str(stop_price),
    "time_in_force": "gtc",
})
print(f"  Order ID  : {stop_order['id']}")
print(f"  Status    : {stop_order['status']}")
print(f"  Stop price: ${stop_price:.2f}")
print(f"  Qty       : {stop_order['qty']}")
print(f"  Side      : {stop_order['side']}")

# ── Step 3: Ladder buy orders ─────────────────────────────────────────────────
ladder_20_price = round(entry_price * 0.80, 2)
ladder_30_price = round(entry_price * 0.70, 2)

print(f"\n{'='*60}")
print(f"STEP 3 — Ladder buy: 20 shares if TSLA hits ${ladder_20_price:.2f} (-20%)")
ladder_20 = place_order({
    "symbol": SYMBOL,
    "qty": "20",
    "side": "buy",
    "type": "limit",
    "limit_price": str(ladder_20_price),
    "time_in_force": "gtc",
})
print(f"  Order ID   : {ladder_20['id']}")
print(f"  Status     : {ladder_20['status']}")
print(f"  Limit price: ${ladder_20_price:.2f}")
print(f"  Qty        : {ladder_20['qty']}")

print(f"\n{'='*60}")
print(f"STEP 4 — Ladder buy: 10 shares if TSLA hits ${ladder_30_price:.2f} (-30%)")
ladder_30 = place_order({
    "symbol": SYMBOL,
    "qty": "10",
    "side": "buy",
    "type": "limit",
    "limit_price": str(ladder_30_price),
    "time_in_force": "gtc",
})
print(f"  Order ID   : {ladder_30['id']}")
print(f"  Status     : {ladder_30['status']}")
print(f"  Limit price: ${ladder_30_price:.2f}")
print(f"  Qty        : {ladder_30['qty']}")

# ── Strategy summary ──────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print("STRATEGY SUMMARY")
print(f"{'='*60}")
print(f"  Symbol        : {SYMBOL}")
print(f"  Entry price   : ${entry_price:.2f}")
print(f"  Entry qty     : {ENTRY_QTY} shares")
print(f"  Entry value   : ${entry_price * ENTRY_QTY:,.2f}")
print(f"")
print(f"  Stop loss     : ${stop_price:.2f}  (-10%)  → sell all 10 shares")
print(f"  Max loss      : ${(entry_price - stop_price) * ENTRY_QTY:,.2f}")
print(f"")
print(f"  Trailing stop : activates at ${entry_price * (1 + TRAIL_TRIGGER_PCT):.2f} (+10%)")
print(f"                  then trails 5% below running high (floor never drops)")
print(f"")
print(f"  Ladder buy 1  : ${ladder_20_price:.2f} (-20%) → buy 20 shares")
print(f"  Ladder buy 2  : ${ladder_30_price:.2f} (-30%) → buy 10 shares")
print(f"{'='*60}")
print(f"\nAll orders placed. Stop-loss order ID to update for trailing: {stop_order['id']}")
print(f"\nRun monitor_trailing.py to activate live trailing stop management.\n")

# Save state for the trailing monitor
import json
state = {
    "symbol": SYMBOL,
    "entry_price": entry_price,
    "entry_qty": ENTRY_QTY,
    "stop_order_id": stop_order["id"],
    "current_stop": stop_price,
    "trailing_active": False,
    "high_water_mark": entry_price,
}
with open("/home/user/Claud-Trade/strategy_state.json", "w") as f:
    json.dump(state, f, indent=2)
print("Strategy state saved to strategy_state.json")
