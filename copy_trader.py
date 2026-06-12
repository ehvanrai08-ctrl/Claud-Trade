"""
Copy Trading Bot — tracks US congressional trades via Quiver Quant
and mirrors them on Alpaca paper trading.
- Fetches latest congressional trades
- Picks the most profitable active politician
- Copies their recent trades if not already placed
- Runs hourly during market hours via GitHub Actions
"""

import json
import logging
import os
import requests
from datetime import datetime, timedelta
from dotenv import dotenv_values

BASE_DIR = "/home/user/Claud-Trade"
config = dotenv_values(f"{BASE_DIR}/.env")

BASE_URL = config["ALPACA_BASE_URL"]
HEADERS  = {
    "APCA-API-KEY-ID":     config["ALPACA_API_KEY"],
    "APCA-API-SECRET-KEY": config["ALPACA_SECRET_KEY"],
    "Content-Type":        "application/json",
}

QUIVER_URL   = "https://api.quiverquant.com/beta/live/congresstrading"
STATE_FILE   = f"{BASE_DIR}/copy_trader_state.json"
MAX_TRADE_VALUE = 5000   # max $ per copied trade
LOOKBACK_DAYS   = 14     # only copy trades filed in last 14 days

logging.basicConfig(
    filename=f"{BASE_DIR}/copy_trader.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger()


# ── Helpers ───────────────────────────────────────────────────────────────────

def market_is_open():
    r = requests.get(f"{BASE_URL}/clock", headers=HEADERS)
    return r.json()["is_open"]

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"copied_trades": [], "tracked_politician": None, "total_trades": 0}
    with open(STATE_FILE) as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def get_congress_trades():
    r = requests.get(QUIVER_URL, headers={"Accept": "application/json"}, timeout=15)
    r.raise_for_status()
    return r.json()

def get_price(symbol):
    r = requests.get(
        f"https://data.alpaca.markets/v2/stocks/{symbol}/trades/latest",
        headers=HEADERS,
    )
    if r.ok:
        return float(r.json()["trade"]["p"])
    return None

def is_tradeable(symbol):
    """Check if symbol is a tradeable US equity on Alpaca."""
    try:
        r = requests.get(f"{BASE_URL}/assets/{symbol}", headers=HEADERS)
        asset = r.json()
        return asset.get("tradable") and asset.get("status") == "active" and asset.get("asset_class") == "us_equity"
    except Exception:
        return False

def place_order(symbol, side, notional):
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json={
        "symbol":        symbol,
        "notional":      str(round(notional, 2)),
        "side":          side,
        "type":          "market",
        "time_in_force": "day",
    })
    if r.ok:
        return r.json()
    log.error(f"Order failed {symbol} {side}: {r.text}")
    return None

def get_position(symbol):
    try:
        r = requests.get(f"{BASE_URL}/positions/{symbol}", headers=HEADERS)
        if r.ok:
            return r.json()
    except Exception:
        pass
    return None


# ── Politician scoring ────────────────────────────────────────────────────────

def pick_best_politician(trades):
    """
    Score politicians by:
    - Number of recent trades (activity)
    - Average ExcessReturn (beats market)
    Only consider trades filed in the last 90 days.
    """
    cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    recent = [t for t in trades if t.get("ReportDate", "") >= cutoff]

    scores = {}
    for t in recent:
        name = t["Representative"]
        excess = float(t.get("ExcessReturn") or 0)
        if name not in scores:
            scores[name] = {"count": 0, "excess_total": 0.0}
        scores[name]["count"] += 1
        scores[name]["excess_total"] += excess

    # Rank by avg excess return * trade frequency
    ranked = sorted(
        scores.items(),
        key=lambda x: (x[1]["excess_total"] / max(x[1]["count"], 1)) * min(x[1]["count"], 10),
        reverse=True,
    )
    if not ranked:
        return None
    best = ranked[0]
    log.info(f"Top politician: {best[0]} trades={best[1]['count']} avg_excess={best[1]['excess_total']/best[1]['count']:.2f}%")
    return best[0]


# ── Copy logic ────────────────────────────────────────────────────────────────

def run():
    if not market_is_open():
        return

    state = load_state()
    trades = get_congress_trades()

    # Pick or stick with tracked politician
    politician = state.get("tracked_politician") or pick_best_politician(trades)
    if not politician:
        log.warning("No politician found to track")
        return

    state["tracked_politician"] = politician
    print(f"[COPY] Tracking: {politician}")
    log.info(f"Tracking: {politician}")

    # Get their recent trades within lookback window
    cutoff = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    their_trades = [
        t for t in trades
        if t["Representative"] == politician
        and t.get("ReportDate", "") >= cutoff
        and t.get("Ticker")
        and t.get("Transaction") in ("Purchase", "Sale (Full)", "Sale (Partial)")
    ]

    if not their_trades:
        log.info(f"No recent trades from {politician} in last {LOOKBACK_DAYS} days")
        print(f"[COPY] No new trades from {politician} recently")
        save_state(state)
        return

    copied = state.get("copied_trades", [])
    new_copies = 0

    for trade in their_trades:
        ticker      = trade["Ticker"].strip().upper()
        transaction = trade["Transaction"]
        report_date = trade["ReportDate"]
        trade_id    = f"{politician}|{ticker}|{report_date}|{transaction}"

        if trade_id in copied:
            continue  # already copied

        # Determine side
        if "Purchase" in transaction:
            side = "buy"
        elif "Sale" in transaction:
            side = "sell"
        else:
            continue

        # Skip if selling something we don't own
        if side == "sell" and not get_position(ticker):
            log.info(f"SKIP sell {ticker} — no position")
            continue

        # Check asset is tradeable
        if not is_tradeable(ticker):
            log.info(f"SKIP {ticker} — not tradeable on Alpaca")
            copied.append(trade_id)
            continue

        # Size the trade
        price = get_price(ticker)
        if not price:
            log.warning(f"SKIP {ticker} — couldn't get price")
            continue

        notional = min(MAX_TRADE_VALUE, price * 1)  # at least 1 share equivalent

        order = place_order(ticker, side, notional)
        if order:
            log.info(f"COPIED: {politician} | {side} {ticker} ~${notional:.0f} | report {report_date} | order {order['id']}")
            print(f"[COPY] {side.upper()} ${notional:.0f} of {ticker} (copied from {politician}, filed {report_date})")
            state["total_trades"] += 1
            new_copies += 1

        copied.append(trade_id)

    state["copied_trades"] = copied[-500:]  # keep last 500 to avoid unbounded growth

    if new_copies == 0:
        print(f"[COPY] No new trades to copy from {politician}")
        log.info("No new trades to copy this run")

    save_state(state)


if __name__ == "__main__":
    run()
