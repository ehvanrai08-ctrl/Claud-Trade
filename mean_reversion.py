"""
Mean Reversion Bot
==================
Buys oversold stocks that are snapping back, sells when they revert to overbought.
Profits in range-bound / choppy markets where trend-following bots sit out.

Signals (daily timeframe):
  ENTRY (buy):  RSI < 30 (oversold) AND price below lower Bollinger Band,
                with a confirmation up-day (today's close > yesterday's close)
  EXIT (sell):  RSI > 70 (overbought) OR price back above middle Bollinger Band,
                OR stop-loss hit (-8% from entry)

Universe: range-bound large caps (low-beta, mean-reverting names)
Position size: $2,000 per name, max 4 concurrent positions
Runs once daily, ~30 min after open, via GitHub Actions.
"""

import json
import logging
import os
import requests
from datetime import datetime, timezone, timedelta
from dotenv import dotenv_values
from perf import record_trade

BASE_DIR = "/home/user/Claud-Trade"
config   = dotenv_values(f"{BASE_DIR}/.env")

BASE_URL = config["ALPACA_BASE_URL"]
HEADERS  = {
    "APCA-API-KEY-ID":     config["ALPACA_API_KEY"],
    "APCA-API-SECRET-KEY": config["ALPACA_SECRET_KEY"],
    "Content-Type":        "application/json",
}
DATA_HEADERS = {
    "APCA-API-KEY-ID":     config["ALPACA_API_KEY"],
    "APCA-API-SECRET-KEY": config["ALPACA_SECRET_KEY"],
}

STATE_FILE = f"{BASE_DIR}/mean_reversion_state.json"

# Range-bound large caps that historically mean-revert well
UNIVERSE = ["KO", "PEP", "JNJ", "WMT", "PG", "MCD", "VZ", "MRK"]

RSI_PERIOD      = 14
RSI_OVERSOLD    = 30
RSI_OVERBOUGHT  = 70
BB_PERIOD       = 20
BB_STD          = 2.0
TRADE_SIZE      = 2000     # $ per position
MAX_POSITIONS   = 4        # max concurrent mean-reversion positions
STOP_LOSS_PCT   = 0.08     # hard stop at -8%

logging.basicConfig(
    filename=f"{BASE_DIR}/mean_reversion.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger()


# ── Data helpers ──────────────────────────────────────────────────────────────

def market_is_open():
    r = requests.get(f"{BASE_URL}/clock", headers=HEADERS)
    return r.json()["is_open"]


def get_daily_closes(symbol, days=60):
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = requests.get(
        f"https://data.alpaca.markets/v2/stocks/{symbol}/bars",
        headers=DATA_HEADERS,
        params={"timeframe": "1Day", "start": start, "limit": 60, "sort": "asc", "adjustment": "raw"},
    )
    bars = r.json().get("bars") or [] if r.ok else []
    return [b["c"] for b in bars]


def get_position(symbol):
    r = requests.get(f"{BASE_URL}/positions/{symbol}", headers=HEADERS)
    return r.json() if r.ok else None


def get_all_positions():
    r = requests.get(f"{BASE_URL}/positions", headers=HEADERS)
    return r.json() if r.ok else []


# ── Indicators ────────────────────────────────────────────────────────────────

def rsi(closes, period=RSI_PERIOD):
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(-period, 0):
        change = closes[i] - closes[i - 1]
        gains  += max(change, 0)
        losses += max(-change, 0)
    avg_gain, avg_loss = gains / period, losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def bollinger(closes, period=BB_PERIOD, num_std=BB_STD):
    if len(closes) < period:
        return None
    window = closes[-period:]
    mean   = sum(window) / period
    var    = sum((c - mean) ** 2 for c in window) / period
    std    = var ** 0.5
    return {
        "middle": mean,
        "upper":  mean + num_std * std,
        "lower":  mean - num_std * std,
    }


# ── Order helpers ─────────────────────────────────────────────────────────────

def buy(symbol, notional):
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json={
        "symbol":        symbol,
        "notional":      str(round(notional, 2)),
        "side":          "buy",
        "type":          "market",
        "time_in_force": "day",
    })
    if r.ok:
        o = r.json()
        log.info(f"BUY {symbol} ~${notional} | order {o['id']}")
        return o
    log.error(f"Buy failed {symbol}: {r.text[:200]}")
    return None


def sell_all(symbol):
    r = requests.delete(f"{BASE_URL}/positions/{symbol}", headers=HEADERS)
    if r.ok:
        log.info(f"SELL (close) {symbol}")
        return True
    return False


# ── State ─────────────────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"entries": {}, "total_trades": 0, "closed_pnl": 0.0}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    if not market_is_open():
        return

    state = load_state()
    entries = state.setdefault("entries", {})

    # ── Manage existing mean-reversion positions (exits) ─────────────────────
    mr_symbols = list(entries.keys())
    for symbol in mr_symbols:
        closes = get_daily_closes(symbol)
        if not closes:
            continue
        price  = closes[-1]
        r_val  = rsi(closes)
        bb     = bollinger(closes)
        entry  = entries[symbol]["entry_price"]

        exit_reason = None
        if r_val is not None and r_val > RSI_OVERBOUGHT:
            exit_reason = f"RSI overbought ({r_val:.1f})"
        elif bb and price > bb["middle"]:
            exit_reason = "reverted to mean (above middle BB)"
        elif price <= entry * (1 - STOP_LOSS_PCT):
            exit_reason = f"stop-loss hit (-{STOP_LOSS_PCT*100:.0f}%)"

        if exit_reason:
            pos = get_position(symbol)
            pnl = float(pos["unrealized_pl"]) if pos else 0.0
            if sell_all(symbol):
                state["closed_pnl"] = state.get("closed_pnl", 0.0) + pnl
                record_trade("mean_reversion", symbol, pnl, exit_reason)
                log.info(f"EXIT {symbol}: {exit_reason} | P&L ${pnl:+.2f}")
                print(f"[MR] EXIT {symbol}: {exit_reason} | P&L ${pnl:+.2f}")
                del entries[symbol]

    # ── Look for new entries (only if we have room) ──────────────────────────
    open_mr = len(entries)
    for symbol in UNIVERSE:
        if open_mr >= MAX_POSITIONS:
            break
        if symbol in entries:
            continue  # already holding
        if get_position(symbol):
            continue  # held by another strategy — don't double up

        closes = get_daily_closes(symbol)
        if len(closes) < BB_PERIOD + 1:
            continue

        price = closes[-1]
        prev  = closes[-2]
        r_val = rsi(closes)
        bb    = bollinger(closes)
        if r_val is None or bb is None:
            continue

        # Entry: oversold + below lower band + confirmation up-day
        oversold       = r_val < RSI_OVERSOLD
        below_band     = price < bb["lower"]
        confirming_day = price > prev

        if oversold and below_band and confirming_day:
            order = buy(symbol, TRADE_SIZE)
            if order:
                entries[symbol] = {
                    "entry_price": price,
                    "entry_date":  datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    "entry_rsi":   round(r_val, 1),
                }
                state["total_trades"] = state.get("total_trades", 0) + 1
                open_mr += 1
                log.info(f"ENTRY {symbol} @ ${price:.2f} | RSI={r_val:.1f} below_lower_BB confirmed")
                print(f"[MR] ENTRY {symbol} @ ${price:.2f} | RSI {r_val:.1f}")

    if not entries:
        print("[MR] No mean-reversion positions; no entry signals today")

    save_state(state)


if __name__ == "__main__":
    run()
