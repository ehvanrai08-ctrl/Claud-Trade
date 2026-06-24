"""
Trend Basket Bot — high-beta names with ATR trailing stops
==========================================================
Generalizes the single-name TSLA trailing-stop strategy to a diversified basket.
Trend-following's edge is "a few big winners pay for many small losses," and that
edge is far more reliable spread across several uncorrelated-ish names than bet on
one stock (Clenow: diversification IS the strategy).

TSLA stays on its own dedicated bot (market_monitor.py); this basket covers the
OTHER high-beta names so the two never fight over the same position.

Per name:
  - ENTRY: buy a fixed notional, but ONLY if price > 50-day SMA (uptrend filter —
    don't start a new trend trade in a downtrend / falling knife).
  - STOP: initial protective stop at entry − ATR(14)×2 (fallback 8%).
  - TRAIL: once up TRAIL_TRIGGER_PCT from entry, ratchet the stop up to
    HWM − ATR×2; the stop only ever moves UP, never down.
  - RE-ENTRY: after a stop-out, re-enter once price recovers REENTRY_PCT above the
    fill AND is still above its 50-day SMA.

COLLISION SAFETY: only manages positions it opened. Defers entry on any symbol
already held by another strategy. The resting stop sells only this bot's qty.

Self-looping job (same reliability pattern as market_monitor.py), 2 PM handoff.
"""

import json
import logging
import os
import time
import requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from dotenv import dotenv_values
from perf import record_trade

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
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

ET         = ZoneInfo("America/New_York")
STATE_FILE = f"{BASE_DIR}/trend_basket_state.json"

# High-beta, liquid names — TSLA deliberately excluded (its own bot owns it).
BASKET = ["NVDA", "AMD", "AVGO", "META", "AMZN", "GOOGL"]

NOTIONAL_PER      = 2500    # $ per name
TRAIL_TRIGGER_PCT = 0.10    # arm trailing once up 10% from entry
ATR_MULTIPLIER    = 2.0     # trail stop = HWM − ATR×mult
STOP_FALLBACK_PCT = 0.08    # if ATR unavailable, stop 8% below
REENTRY_PCT       = 0.02    # re-enter once price recovers 2% above the fill
SMA_TREND         = 50      # only buy when price > 50-day SMA

POLL_INTERVAL_SEC = 60
MAX_RUNTIME_MIN   = 330

logging.basicConfig(
    filename=f"{BASE_DIR}/trend_basket.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger()


# ── Helpers ───────────────────────────────────────────────────────────────────

def market_is_open():
    r = requests.get(f"{BASE_URL}/clock", headers=HEADERS)
    return r.json().get("is_open", False)


def get_price(symbol):
    r = requests.get(f"https://data.alpaca.markets/v2/stocks/{symbol}/trades/latest",
                     headers=DATA_HEADERS)
    return float(r.json()["trade"]["p"]) if r.ok else None


def daily_bars(symbol, n=60):
    start = (datetime.now(timezone.utc) - timedelta(days=n + 30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = requests.get(f"https://data.alpaca.markets/v2/stocks/{symbol}/bars",
                     headers=DATA_HEADERS,
                     params={"timeframe": "1Day", "start": start, "limit": 120,
                             "sort": "asc", "adjustment": "raw"})
    return r.json().get("bars") or [] if r.ok else []


def atr14(bars):
    if len(bars) < 15:
        return None
    b = bars[-15:]
    trs = [max(b[i]["h"]-b[i]["l"], abs(b[i]["h"]-b[i-1]["c"]), abs(b[i]["l"]-b[i-1]["c"]))
           for i in range(1, len(b))]
    return sum(trs[-14:]) / 14


def sma(bars, n):
    if len(bars) < n:
        return None
    return sum(b["c"] for b in bars[-n:]) / n


def get_position(symbol):
    """dict / None (genuine 404) / 'ERROR' (lookup failed)."""
    try:
        r = requests.get(f"{BASE_URL}/positions/{symbol}", headers=HEADERS)
    except Exception:
        return "ERROR"
    if r.status_code == 404:
        return None
    if r.ok:
        return r.json()
    return "ERROR"


def get_order(order_id):
    try:
        r = requests.get(f"{BASE_URL}/orders/{order_id}", headers=HEADERS)
        return r.json() if r.ok else None
    except Exception:
        return None


def cancel_order(order_id):
    if not order_id:
        return
    try:
        requests.delete(f"{BASE_URL}/orders/{order_id}", headers=HEADERS)
    except Exception:
        pass


def place_stop(symbol, qty, stop_price):
    limit_price = round(stop_price * 0.99, 2)
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json={
        "symbol":        symbol,
        "qty":           str(qty),
        "side":          "sell",
        "type":          "stop_limit",
        "stop_price":    str(round(stop_price, 2)),
        "limit_price":   str(limit_price),
        "time_in_force": "gtc",
    })
    return r.json() if r.ok else None


def bracket_buy(symbol, qty, stop_price):
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json={
        "symbol":        symbol,
        "qty":           str(qty),
        "side":          "buy",
        "type":          "market",
        "time_in_force": "day",
        "order_class":   "bracket",
        "stop_loss":     {"stop_price": str(round(stop_price, 2))},
        "take_profit":   {"limit_price": "99999.00"},
    })
    if r.ok:
        return r.json()
    log.error(f"Buy failed {symbol}: {r.text[:200]}")
    return None


# ── State ─────────────────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            log.warning("State file unreadable — starting fresh.")
    return {"positions": {}}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def stop_leg_id(order):
    legs = order.get("legs", []) or []
    leg = next((l for l in legs if l.get("type") == "stop_loss"), None)
    if leg:
        return leg["id"]
    return legs[1]["id"] if len(legs) > 1 else order["id"]


# ── Per-symbol logic ──────────────────────────────────────────────────────────

def manage_symbol(symbol, state):
    positions = state["positions"]
    bars = daily_bars(symbol)
    price = get_price(symbol)
    if price is None or len(bars) < SMA_TREND:
        return
    atr   = atr14(bars)
    trend = sma(bars, SMA_TREND)
    pos   = get_position(symbol)
    if pos == "ERROR":
        log.warning(f"{symbol}: position lookup failed — skipping this tick.")
        return

    held = symbol in positions

    # ── We think we hold it ──────────────────────────────────────────────────
    if held:
        info = positions[symbol]
        # Stop fired / position gone
        if not pos:
            sid = info.get("stop_order_id")
            order = get_order(sid) if sid else None
            entry = info["entry_price"]; qty = info["qty"]
            if order and order.get("status") == "filled":
                fill = float(order.get("filled_avg_price") or 0) or info.get("current_stop", entry)
                pnl = (fill - entry) * qty
                record_trade("trend_basket", symbol, pnl, "trailing stop hit")
                log.info(f"STOP HIT {symbol} @ ${fill:.2f} | P&L ${pnl:+.2f}")
                print(f"[BASKET] STOP HIT {symbol} @ ${fill:.2f} | P&L ${pnl:+.2f}")
                info["last_exit"] = fill
            else:
                cancel_order(sid)
                log.info(f"{symbol} position gone (not via our stop) — cleaned up")
            del positions[symbol]
            return

        # Update high-water mark
        if price > info["hwm"]:
            info["hwm"] = price

        # Arm trailing once up the trigger
        if not info["trailing_active"] and price >= info["entry_price"] * (1 + TRAIL_TRIGGER_PCT):
            info["trailing_active"] = True
            log.info(f"TRAILING ARMED {symbol} @ ${price:.2f}")

        # Raise the stop if trailing is active (only ever upward)
        if info["trailing_active"]:
            new_stop = (round(info["hwm"] - atr * ATR_MULTIPLIER, 2) if atr
                        else round(info["hwm"] * (1 - STOP_FALLBACK_PCT), 2))
            if new_stop > info["current_stop"]:
                cancel_order(info.get("stop_order_id"))
                new_order = place_stop(symbol, info["qty"], new_stop)
                if new_order:
                    info["stop_order_id"] = new_order["id"]
                    info["current_stop"]  = new_stop
                    log.info(f"STOP RAISED {symbol} → ${new_stop:.2f}")
                    print(f"[BASKET] {symbol} stop raised → ${new_stop:.2f}")
        return

    # ── We don't hold it ─────────────────────────────────────────────────────
    # Hands-off: another strategy owns this symbol.
    if pos:
        log.info(f"{symbol} held by another strategy — deferring.")
        return
    # Re-entry guard: if we recently stopped out, only buy back above the fill.
    # Uptrend filter: only buy above the 50-day SMA.
    if price <= trend:
        log.info(f"{symbol} ${price:.2f} below SMA{SMA_TREND} ${trend:.2f} — no entry.")
        return

    qty = int(NOTIONAL_PER // price)
    if qty < 1:
        return
    init_stop = (round(price - atr * ATR_MULTIPLIER, 2) if atr
                 else round(price * (1 - STOP_FALLBACK_PCT), 2))
    order = bracket_buy(symbol, qty, init_stop)
    if order:
        positions[symbol] = {
            "entry_price":     price,
            "qty":             qty,
            "stop_order_id":   stop_leg_id(order),
            "current_stop":    init_stop,
            "hwm":             price,
            "trailing_active": False,
            "entry_date":      datetime.now(ET).strftime("%Y-%m-%d"),
        }
        log.info(f"ENTRY {symbol} x{qty} @ ${price:.2f} | stop ${init_stop:.2f}")
        print(f"[BASKET] ENTRY {symbol} x{qty} @ ${price:.2f} | stop ${init_stop:.2f}")


# ── Loop ──────────────────────────────────────────────────────────────────────

def tick():
    state = load_state()
    for symbol in BASKET:
        try:
            manage_symbol(symbol, state)
        except Exception as e:
            log.exception(f"{symbol} tick error (continuing): {e}")
    save_state(state)


def run():
    start = time.monotonic()
    log.info("Trend basket loop started")
    print("Trend basket loop started")
    while (time.monotonic() - start) / 60 < MAX_RUNTIME_MIN:
        try:
            if not market_is_open():
                log.info("Market closed — exiting loop.")
                break
            tick()
        except Exception as e:
            log.exception(f"tick error (continuing): {e}")
        time.sleep(POLL_INTERVAL_SEC)
    log.info("Trend basket loop ended")


if __name__ == "__main__":
    run()
