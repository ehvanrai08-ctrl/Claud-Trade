"""
IBS (Internal Bar Strength) Bot — QQQ
=====================================
Mean-reversion on the Internal Bar Strength indicator, the single best
risk-adjusted candidate in our backtests (QQQ 2015-2026: 69% win rate,
profit factor 2.10, Sharpe 1.43 — backtest_candidates.py).

    IBS = (Close - Low) / (High - Low)      # where today closed in its range

Rule (decided once per day near the close, holds multi-day):
  - BUY  QQQ when IBS < 0.20  (closed near the LOW — oversold)
  - SELL QQQ when IBS > 0.80  (closed near the HIGH — bounce captured)
  No trend filter: the unfiltered version tested strictly better (PF 2.10 vs 1.89).

COLLISION SAFETY (multiple bots can touch QQQ — ORB, SIP-ORB):
  - Defers entry if a QQQ position already exists (hands-off; another bot owns it).
  - On exit, sells EXACTLY the quantity this bot bought — never close_position(),
    which would wipe out another bot's shares in the merged broker position.
  - Reconciles if its position vanishes (e.g., an intraday bot closed QQQ): marks
    flat and records the trade at the last price instead of crashing.

Runs once daily at ~3:50 PM ET (one decision per day). Holds overnight.
"""

import json
import logging
import os
import time
import requests
from datetime import datetime, timedelta, timezone
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
STATE_FILE = f"{BASE_DIR}/ibs_state.json"

SYMBOL    = "QQQ"
NOTIONAL  = 2000      # $ per position
IBS_BUY   = 0.20      # buy below this
IBS_SELL  = 0.80      # sell above this

logging.basicConfig(
    filename=f"{BASE_DIR}/ibs.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger()


# ── Data ──────────────────────────────────────────────────────────────────────

def market_is_open():
    r = requests.get(f"{BASE_URL}/clock", headers=HEADERS)
    return r.json().get("is_open", False)


def todays_bar():
    """Today's forming daily bar (o/h/l/c). Returns None if stale/unavailable."""
    start = (datetime.now(timezone.utc) - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = requests.get(
        f"https://data.alpaca.markets/v2/stocks/{SYMBOL}/bars",
        headers=DATA_HEADERS,
        params={"timeframe": "1Day", "start": start, "limit": 10, "sort": "asc"},
    )
    bars = r.json().get("bars") or [] if r.ok else []
    if not bars:
        return None
    last = bars[-1]
    bar_date = datetime.fromisoformat(last["t"].replace("Z", "+00:00")).astimezone(ET).date()
    if bar_date != datetime.now(ET).date():
        log.warning(f"Latest bar is {bar_date}, not today — skipping (stale/holiday).")
        return None
    return last


def ibs_value(bar):
    rng = bar["h"] - bar["l"]
    return (bar["c"] - bar["l"]) / rng if rng > 0 else 0.5


def latest_price():
    r = requests.get(
        f"https://data.alpaca.markets/v2/stocks/{SYMBOL}/trades/latest",
        headers=DATA_HEADERS,
    )
    return float(r.json()["trade"]["p"]) if r.ok else None


# ── Orders ────────────────────────────────────────────────────────────────────

def get_position(symbol):
    """Position dict, None if genuinely flat (404), or "ERROR" if the lookup
    failed. Distinguishing these matters: treating an API error as "flat" would
    fabricate a reconcile trade and orphan a real position."""
    try:
        r = requests.get(f"{BASE_URL}/positions/{symbol}", headers=HEADERS)
    except Exception:
        return "ERROR"
    if r.status_code == 404:
        return None
    if r.ok:
        return r.json()
    return "ERROR"


def submit_market(side, qty):
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json={
        "symbol":        SYMBOL,
        "qty":           str(qty),
        "side":          side,
        "type":          "market",
        "time_in_force": "day",
    })
    if r.ok:
        return r.json()
    log.error(f"{side} failed: {r.text[:200]}")
    return None


def fill_price(order_id, fallback):
    """Poll up to ~6s for the ACTUAL fill price. A market order is not yet
    'filled' on the first GET, so a single poll would return the fallback and
    record a wrong price into the ledger. Retry until filled or timeout."""
    for _ in range(6):
        try:
            r = requests.get(f"{BASE_URL}/orders/{order_id}", headers=HEADERS)
            if r.ok:
                o = r.json()
                if o.get("status") == "filled":
                    fp = float(o.get("filled_avg_price") or 0)
                    if fp:
                        return fp
        except Exception:
            pass
        time.sleep(1)
    log.warning(f"Order {order_id} not confirmed filled after poll — using fallback price.")
    return fallback


# ── State ─────────────────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            log.warning("State file unreadable (truncated?) — starting flat.")
    return {"holding": False, "entry_price": None, "entry_qty": 0, "entry_date": None}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Strategy ──────────────────────────────────────────────────────────────────

def run():
    if not market_is_open():
        log.info("Market closed — skipping.")
        print("[IBS] Market closed")
        return

    state = load_state()
    bar   = todays_bar()
    if not bar:
        print("[IBS] No usable bar today")
        return

    ibs   = ibs_value(bar)
    price = latest_price() or bar["c"]
    pos   = get_position(SYMBOL)
    if pos == "ERROR":
        log.warning("Position lookup failed — skipping run to avoid acting on bad data.")
        print("[IBS] Position lookup failed — skipping")
        return
    log.info(f"IBS={ibs:.2f} price=${price:.2f} holding={state['holding']} "
             f"position={'yes' if pos else 'no'}")

    flat_state = {"holding": False, "entry_price": None, "entry_qty": 0, "entry_date": None}

    if state["holding"]:
        # Reconcile: our shares were genuinely closed by something else (404, not
        # an API error — that was filtered above).
        if not pos:
            pnl = (price - state["entry_price"]) * state["entry_qty"]
            record_trade("ibs_qqq", SYMBOL, pnl, "reconciled (position vanished)")
            log.warning(f"Position gone — reconciled. P&L ${pnl:+.2f}")
            save_state(flat_state)
            return
        # Exit signal: sell our own qty, but never more than the broker actually
        # holds (protects another bot's shares in a merged position).
        if ibs > IBS_SELL:
            held = int(float(pos["qty"]))
            qty  = min(state["entry_qty"], held)
            if qty < 1:
                log.warning(f"Held qty {held} < expected {state['entry_qty']} — reconciling flat.")
                save_state(flat_state)
                return
            order = submit_market("sell", qty)
            if order:
                exit_p = fill_price(order["id"], price)
                pnl = (exit_p - state["entry_price"]) * qty
                record_trade("ibs_qqq", SYMBOL, pnl, f"IBS exit {ibs:.2f}")
                log.info(f"SELL {qty} {SYMBOL} @ ~${exit_p:.2f} | P&L ${pnl:+.2f}")
                print(f"[IBS] SELL {qty} {SYMBOL} | P&L ${pnl:+.2f}")
                save_state(flat_state)
        else:
            log.info(f"Holding {state['entry_qty']} {SYMBOL} — IBS {ibs:.2f} not > {IBS_SELL}")
        return

    # Flat: only enter if QQQ isn't already held by another bot (hands-off).
    if pos:
        log.info(f"{SYMBOL} already held by another strategy (qty {pos.get('qty')}) — deferring.")
        print(f"[IBS] {SYMBOL} held elsewhere — deferring")
        return

    if ibs < IBS_BUY:
        qty = int(NOTIONAL // price)
        if qty < 1:
            log.warning(f"Notional ${NOTIONAL} too small at ${price:.2f}")
            return
        order = submit_market("buy", qty)
        if order:
            entry_p = fill_price(order["id"], price)
            save_state({"holding": True, "entry_price": entry_p, "entry_qty": qty,
                        "entry_date": datetime.now(ET).strftime("%Y-%m-%d")})
            log.info(f"BUY {qty} {SYMBOL} @ ~${entry_p:.2f} | IBS {ibs:.2f}")
            print(f"[IBS] BUY {qty} {SYMBOL} @ ~${entry_p:.2f} | IBS {ibs:.2f}")
    else:
        log.info(f"No entry — IBS {ibs:.2f} not < {IBS_BUY}")
        print(f"[IBS] No entry — IBS {ibs:.2f}")


if __name__ == "__main__":
    run()
