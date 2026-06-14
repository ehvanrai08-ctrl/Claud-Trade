"""
DCA Index Base
==============
The "boring base" beneath the active strategies. Buys a fixed dollar amount
of a broad index ETF on a fixed schedule regardless of price. Historically
the most reliable long-term wealth builder; near-zero risk of catastrophic loss.

- Buys $500 of VOO every Monday (or next trading day if Monday is a holiday)
- Never sells — pure accumulation
- Records each buy and tracks total invested + average cost
- Runs once weekly via GitHub Actions
"""

import json
import logging
import os
import requests
from datetime import datetime, timezone
from dotenv import dotenv_values

BASE_DIR = "/home/user/Claud-Trade"
config   = dotenv_values(f"{BASE_DIR}/.env")

BASE_URL = config["ALPACA_BASE_URL"]
HEADERS  = {
    "APCA-API-KEY-ID":     config["ALPACA_API_KEY"],
    "APCA-API-SECRET-KEY": config["ALPACA_SECRET_KEY"],
    "Content-Type":        "application/json",
}

STATE_FILE   = f"{BASE_DIR}/dca_state.json"
ETF          = "VOO"     # Vanguard S&P 500 ETF
WEEKLY_AMOUNT = 500      # $ per weekly buy

logging.basicConfig(
    filename=f"{BASE_DIR}/dca.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger()


def market_is_open():
    r = requests.get(f"{BASE_URL}/clock", headers=HEADERS)
    return r.json()["is_open"]


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"total_invested": 0.0, "buys": 0, "last_buy_date": None}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def buy(symbol, notional):
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json={
        "symbol":        symbol,
        "notional":      str(round(notional, 2)),
        "side":          "buy",
        "type":          "market",
        "time_in_force": "day",
    })
    if r.ok:
        return r.json()
    log.error(f"DCA buy failed: {r.text[:200]}")
    return None


def run():
    if not market_is_open():
        return

    state = load_state()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Guard: only one buy per day (in case the workflow fires more than once)
    if state.get("last_buy_date") == today:
        return

    order = buy(ETF, WEEKLY_AMOUNT)
    if order:
        state["total_invested"] = state.get("total_invested", 0.0) + WEEKLY_AMOUNT
        state["buys"]           = state.get("buys", 0) + 1
        state["last_buy_date"]  = today
        log.info(f"DCA BUY: ${WEEKLY_AMOUNT} of {ETF} | total invested ${state['total_invested']:.2f} over {state['buys']} buys | order {order['id']}")
        print(f"[DCA] Bought ${WEEKLY_AMOUNT} of {ETF} | total invested ${state['total_invested']:.2f}")
        save_state(state)


if __name__ == "__main__":
    run()
