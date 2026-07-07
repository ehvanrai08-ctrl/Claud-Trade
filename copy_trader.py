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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
config = dotenv_values(f"{BASE_DIR}/.env")

BASE_URL = config["ALPACA_BASE_URL"]
HEADERS  = {
    "APCA-API-KEY-ID":     config["ALPACA_API_KEY"],
    "APCA-API-SECRET-KEY": config["ALPACA_SECRET_KEY"],
    "Content-Type":        "application/json",
}

QUIVER_URL      = "https://api.quiverquant.com/beta/live/congresstrading"
STATE_FILE      = f"{BASE_DIR}/copy_trader_state.json"
MAX_TRADE_VALUE = 5000   # max $ per copied trade
LOOKBACK_DAYS   = 30     # only copy trades filed in last 30 days
MIN_CONVICTION  = 2      # require at least 2 politicians buying same ticker to copy

logging.basicConfig(
    filename=f"{BASE_DIR}/copy_trader.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger()


# ── Helpers ───────────────────────────────────────────────────────────────────

def market_is_open():
    try:
        r = requests.get(f"{BASE_URL}/clock", headers=HEADERS, timeout=15)
        return r.json().get("is_open", False) if r.ok else False
    except Exception:
        return False

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
        timeout=15,
    )
    if r.ok:
        trade = r.json().get("trade") or {}
        p = trade.get("p")
        return float(p) if p else None
    return None

def is_tradeable(symbol):
    """Check if symbol is a tradeable US equity on Alpaca."""
    try:
        r = requests.get(f"{BASE_URL}/assets/{symbol}", headers=HEADERS, timeout=15)
        asset = r.json()
        # Alpaca's asset object uses the field "class" (not "asset_class").
        asset_cls = asset.get("class") or asset.get("asset_class")
        return asset.get("tradable") and asset.get("status") == "active" and asset_cls == "us_equity"
    except Exception:
        return False

def place_order(symbol, side, notional):
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, timeout=15, json={
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
        r = requests.get(f"{BASE_URL}/positions/{symbol}", headers=HEADERS, timeout=15)
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

    # Re-evaluate best politician weekly to avoid locking onto a stale pick.
    # Default last_eval to today so a missing key doesn't force a re-eval every run.
    today_str = datetime.now().strftime("%Y-%m-%d")
    last_eval = state.get("last_politician_eval") or today_str
    week_ago  = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    if not state.get("tracked_politician") or last_eval < week_ago:
        politician = pick_best_politician(trades)
        state["last_politician_eval"] = today_str
    else:
        politician = state.get("tracked_politician")
        state["last_politician_eval"] = state.get("last_politician_eval") or today_str
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

    # Build conviction map: tickers bought by 2+ politicians recently
    all_recent_buys = {}
    for t in trades:
        if t.get("ReportDate","") >= cutoff and t.get("Transaction") == "Purchase" and t.get("Ticker"):
            ticker = t["Ticker"].strip().upper()
            buyers = all_recent_buys.setdefault(ticker, set())
            buyers.add(t["Representative"])
    high_conviction = {t for t, buyers in all_recent_buys.items() if len(buyers) >= MIN_CONVICTION}

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
        pos = get_position(ticker) if side == "sell" else None
        if side == "sell" and not pos:
            log.info(f"SKIP sell {ticker} — no position")
            continue

        # Check asset is tradeable
        if not is_tradeable(ticker):
            log.info(f"SKIP {ticker} — not tradeable on Alpaca")
            copied.append(trade_id)
            continue

        # Size by conviction: full size if 2+ politicians agree, half if solo
        price = get_price(ticker)
        if not price:
            log.warning(f"SKIP {ticker} — couldn't get price")
            continue

        conviction_mult = 1.0 if ticker in high_conviction else 0.5
        notional = MAX_TRADE_VALUE * conviction_mult

        # Never sell more than we actually hold — and mirror a partial sale as
        # selling half the position, not a fixed $ amount that may exceed it.
        if side == "sell":
            held_value = abs(float(pos.get("market_value", 0) or 0))
            frac = 0.5 if transaction == "Sale (Partial)" else 1.0
            notional = min(notional, round(held_value * frac, 2))
            if notional < 1:
                log.info(f"SKIP sell {ticker} — position too small (${held_value:.2f})")
                copied.append(trade_id)
                continue

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
