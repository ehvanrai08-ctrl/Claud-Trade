"""
Copy Trading Bot — tracks US congressional trades and mirrors them on Alpaca
paper trading.
- Fetches latest congressional trades from the OFFICIAL disclosure systems
  (House Clerk + Senate eFD, via congress_disclosures.py). Quiver Quant was
  the original source until it went 401/paywalled on 2026-07-08.
- Picks the most profitable active politician — ranked by the realized
  performance of their disclosed buys (Alpaca price data), since Quiver's
  proprietary ExcessReturn field is no longer available.
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
    from congress_disclosures import fetch_recent_trades
    return fetch_recent_trades(days=90)

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

def place_order(symbol, side, notional, qty=None):
    """Market order by notional (default) or by qty (fallback for sells on
    fractional positions where Alpaca rejects the notional form). DRY_RUN=1
    logs the intent without placing anything (CI smoke tests)."""
    payload = {"symbol": symbol, "side": side, "type": "market", "time_in_force": "day"}
    if qty is not None:
        payload["qty"] = str(round(float(qty), 6))
    else:
        payload["notional"] = str(round(notional, 2))
    if os.environ.get("DRY_RUN"):
        log.info(f"DRY_RUN: would {side} {symbol} {payload.get('qty') or '$' + payload['notional']}")
        return {"id": "dry-run"}
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, timeout=15, json=payload)
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

MAX_SCORE_TICKERS = 50   # bound Alpaca bar fetches per scoring pass


def _daily_closes(symbol, days=130):
    """[(iso_date, close), ...] ascending, dividend/split-adjusted."""
    start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
    try:
        r = requests.get(
            f"https://data.alpaca.markets/v2/stocks/{symbol}/bars",
            headers=HEADERS,
            params={"timeframe": "1Day", "start": start, "limit": 200,
                    "adjustment": "all", "sort": "asc"},
            timeout=15,
        )
        bars = r.json().get("bars") or [] if r.ok else []
        return [(b["t"][:10], b["c"]) for b in bars]
    except Exception:
        return []


def pick_best_politician(trades):
    """
    Rank politicians by how their disclosed BUYS actually performed from
    transaction date to now (Alpaca daily bars) — a primary-data replacement
    for Quiver's proprietary ExcessReturn field. Score = avg buy return ×
    min(buy_count, 10); at least 2 scoreable buys required. Falls back to
    plain activity ranking if no prices could be fetched (e.g. data outage).
    Only considers trades filed in the last 90 days.
    """
    cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    buys = [t for t in trades
            if t.get("ReportDate", "") >= cutoff
            and t.get("Transaction") == "Purchase" and t.get("Ticker")]
    if not buys:
        return None

    # One bar fetch per unique ticker, most recently traded first, capped.
    tickers = []
    for t in sorted(buys, key=lambda x: x["ReportDate"], reverse=True):
        tk = t["Ticker"].strip().upper()
        if tk not in tickers:
            tickers.append(tk)
    closes = {tk: _daily_closes(tk) for tk in tickers[:MAX_SCORE_TICKERS]}

    scores = {}
    for t in buys:
        name   = t["Representative"]
        series = closes.get(t["Ticker"].strip().upper())
        entry_date = t.get("TransactionDate") or t["ReportDate"]
        s = scores.setdefault(name, {"count": 0, "ret_total": 0.0, "scored": 0})
        s["count"] += 1
        if not series:
            continue
        entry = next((c for d, c in series if d >= entry_date), None)
        if not entry or entry <= 0:
            continue
        s["ret_total"] += series[-1][1] / entry - 1
        s["scored"]    += 1

    ranked = sorted(
        ((n, s) for n, s in scores.items() if s["scored"] >= 2),
        key=lambda x: (x[1]["ret_total"] / x[1]["scored"]) * min(x[1]["scored"], 10),
        reverse=True,
    )
    if not ranked:
        # No prices at all (data outage / all-new tickers) — most active buyer.
        ranked = sorted(scores.items(), key=lambda x: x[1]["count"], reverse=True)
        best = ranked[0]
        log.info(f"Top politician (activity fallback): {best[0]} buys={best[1]['count']}")
        return best[0]
    best = ranked[0]
    log.info(f"Top politician: {best[0]} buys={best[1]['scored']} "
             f"avg_buy_return={best[1]['ret_total']/best[1]['scored']*100:+.2f}%")
    return best[0]


# ── Copy logic ────────────────────────────────────────────────────────────────

def run():
    if not market_is_open():
        return

    state = load_state()
    try:
        trades = get_congress_trades()
        # Reset failure counter on success so future alerts reflect fresh outages.
        state["fetch_fail_count"] = 0
        state.pop("quiver_fail_count", None)   # legacy key from the Quiver era
    except Exception as e:
        # Distinguish auth/blocking failures (401/403 → source changed, needs a
        # human) from transient errors (5xx, timeout → retry is fine).
        err_str = str(e)
        is_auth_failure = "401" in err_str or "403" in err_str
        log.warning(f"Disclosure fetch failed, skipping this run: {e}")
        consecutive = state.get("fetch_fail_count",
                                state.get("quiver_fail_count", 0)) + 1
        state["fetch_fail_count"] = consecutive
        save_state(state)
        if is_auth_failure:
            print(f"[COPY] ALERT: disclosure source is blocking us ({e}) — House Clerk / Senate eFD access needs a human look.")
        elif consecutive >= 3:
            print(f"[COPY] ALERT: disclosure fetch has failed {consecutive} consecutive runs ({e})")
        else:
            print(f"[COPY] Disclosure fetch failed — skipping ({e})")
        return

    # Re-evaluate best politician weekly to avoid locking onto a stale pick.
    # Default last_eval to today so a missing key doesn't force a re-eval every run.
    today_str = datetime.now().strftime("%Y-%m-%d")
    # Default to a date old enough to force scoring on first run (missing key = never evaluated).
    last_eval = state.get("last_politician_eval") or "2000-01-01"
    week_ago  = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    if not state.get("tracked_politician") or last_eval < week_ago:
        politician = pick_best_politician(trades)
        state["last_politician_eval"] = today_str
    else:
        politician = state.get("tracked_politician")
        # Do NOT overwrite last_politician_eval here — preserving the original
        # eval date is what allows the weekly re-evaluation to fire correctly.
        # Overwriting it on every non-eval run would silently push the timer
        # forward and prevent re-evaluation from ever triggering after week 1.
    if not politician:
        log.warning("No politician found to track")
        return

    state["tracked_politician"] = politician
    print(f"[COPY] Tracking: {politician}")
    log.info(f"Tracking: {politician}")

    # Get their recent trades within lookback window
    copy_cutoff = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    their_trades = [
        t for t in trades
        if t["Representative"] == politician
        and t.get("ReportDate", "") >= copy_cutoff
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
        if t.get("ReportDate","") >= copy_cutoff and t.get("Transaction") == "Purchase" and t.get("Ticker"):
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
        if not order and side == "sell":
            # Notional sells on fractional positions get rejected — fall back
            # to selling the exact share qty instead of leaving the signal
            # permanently stuck.
            frac = 0.5 if transaction == "Sale (Partial)" else 1.0
            fallback_qty = abs(float(pos.get("qty", 0) or 0)) * frac
            if fallback_qty > 0:
                order = place_order(ticker, side, notional, qty=fallback_qty)
        if order:
            log.info(f"COPIED: {politician} | {side} {ticker} ~${notional:.0f} | report {report_date} | order {order['id']}")
            print(f"[COPY] {side.upper()} ${notional:.0f} of {ticker} (copied from {politician}, filed {report_date})")
            state["total_trades"] += 1
            new_copies += 1
            copied.append(trade_id)
        else:
            # Do NOT mark a failed order as copied — a transient broker error
            # would otherwise permanently skip this trade; the hourly cron
            # retries it instead.
            log.warning(f"Order failed for {ticker} — leaving uncopied to retry next run")

    state["copied_trades"] = copied[-500:]  # keep last 500 to avoid unbounded growth

    if new_copies == 0:
        already_seen = sum(
            1 for trade in their_trades
            if f"{politician}|{trade['Ticker'].strip().upper()}|{trade['ReportDate']}|{trade['Transaction']}"
               in copied
        )
        print(f"[COPY] No new trades to copy from {politician} ({len(their_trades)} examined, {already_seen} already copied)")
        log.info(f"No new trades to copy this run ({len(their_trades)} examined, {already_seen} already copied)")

    save_state(state)


if __name__ == "__main__":
    run()
