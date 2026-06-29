"""
Emerging-Growth Basket Bot  ⚠ EXPERIMENTAL / HIGH-VARIANCE
==========================================================
The live, tradeable leg of the "find companies early that become huge" idea —
deliberately built as a DIVERSIFIED, RISK-CONTROLLED basket, never a single
concentrated bet, because picking one multibagger is mostly luck.

⚠ HONEST STATUS (see backtest_emerging.py): on 2016-2026 data the momentum
SELECTION overlay added NO risk-adjusted value over equal-weight-holding the
same universe (selection alpha ~0). The apparent SPY-beating return is
survivorship bias (the universe is today's known winners). The ONE real benefit
of the rules below is the 200-day trend gate, which cut max drawdown ~68%→57%.
So this is sized SMALL and labeled experimental: a high-variance growth sleeve
with drawdown control, NOT a proven-alpha strategy. Do not scale it up on the
backtest's headline return.

Rule (monthly): from the scout's tradeable watchlist (emerging_watchlist.json,
with a built-in fallback universe), rank by ensembled 6–12mo momentum, hold the
top N that are ALSO above their 200-day SMA (trend gate), equal-weight. Names
failing the gate are left in cash.

Collision-safe like sector_momentum/superinvestor: tracks its own per-symbol qty,
sells only what it bought, reconciles vanished positions, and every buy passes
the portfolio risk guard. Capital-weighted by get_weight("emerging_growth").
"""

import json
import logging
import os
import requests
from datetime import datetime, timedelta, timezone
from dotenv import dotenv_values
from perf import record_trade
from capital_allocator import get_weight
from risk_guard import can_enter

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

STATE_FILE     = f"{BASE_DIR}/emerging_growth_state.json"
WATCHLIST_JSON = f"{BASE_DIR}/emerging_watchlist.json"

# Fallback universe if the scout hasn't written a watchlist yet (same names as
# backtest_emerging.py). Excludes TSLA/NVDA/AMD etc. that other bots own.
FALLBACK_UNIVERSE = [
    "SHOP", "SQ", "MELI", "NOW", "TEAM", "NFLX", "ISRG", "WDAY", "VEEV",
    "OKTA", "ZS", "TTD", "ROKU", "DDOG", "CRWD", "NET", "SNOW", "ABNB",
    "DASH", "PLTR", "U", "RBLX", "COIN", "HOOD", "MDB", "ZM", "DOCU",
    "PINS", "SNAP", "TWLO",
]

TOP_N           = 6
LOOKBACK_MONTHS = [6, 9, 12]
TD_PER_MONTH    = 21
SMA_TREND       = 200
ALLOCATION      = 4000   # SMALL experimental sleeve → ALLOCATION/TOP_N per name

logging.basicConfig(
    filename=f"{BASE_DIR}/emerging_growth.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger()


# ── Alpaca helpers ──────────────────────────────────────────────────────────--

def market_is_open():
    r = requests.get(f"{BASE_URL}/clock", headers=HEADERS)
    return r.json().get("is_open", False)


def get_adjusted_closes(symbol, days=520):
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = requests.get(f"https://data.alpaca.markets/v2/stocks/{symbol}/bars",
                     headers=DATA_HEADERS,
                     params={"timeframe": "1Day", "start": start, "limit": 1000,
                             "sort": "asc", "adjustment": "all"})
    bars = r.json().get("bars") or [] if r.ok else []
    return [b["c"] for b in bars]


def momentum_score(closes):
    longest = max(LOOKBACK_MONTHS) * TD_PER_MONTH
    if len(closes) < longest + 1:
        return None
    now = closes[-1]
    rs = []
    for m in LOOKBACK_MONTHS:
        past = closes[-1 - m * TD_PER_MONTH]
        if past > 0:
            rs.append(now / past - 1)
    return sum(rs) / len(rs) if rs else None


def above_trend(closes):
    if len(closes) < SMA_TREND:
        return False
    return closes[-1] > sum(closes[-SMA_TREND:]) / SMA_TREND


def latest_price(symbol):
    r = requests.get(f"https://data.alpaca.markets/v2/stocks/{symbol}/trades/latest",
                     headers=DATA_HEADERS)
    return float(r.json()["trade"]["p"]) if r.ok else None


def get_position(symbol):
    r = requests.get(f"{BASE_URL}/positions/{symbol}", headers=HEADERS)
    return r.json() if r.ok else None


def sell_qty(symbol, qty):
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json={
        "symbol": symbol, "qty": str(qty), "side": "sell",
        "type": "market", "time_in_force": "day"})
    if r.ok:
        return r.json()
    log.error(f"Sell failed {symbol}: {r.text[:200]}")
    return None


def buy_notional(symbol, notional):
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json={
        "symbol": symbol, "notional": str(round(notional, 2)),
        "side": "buy", "type": "market", "time_in_force": "day"})
    if r.ok:
        return r.json()
    log.error(f"Buy failed {symbol}: {r.text[:200]}")
    return None


# ── Universe / state ──────────────────────────────────────────────────────────

def load_universe():
    """Tradeable names from the scout's watchlist, else the fallback."""
    if os.path.exists(WATCHLIST_JSON):
        try:
            with open(WATCHLIST_JSON) as f:
                names = json.load(f).get("tradeable") or []
            if names:
                return names
        except Exception:
            log.warning("Watchlist unreadable — using fallback universe.")
    return FALLBACK_UNIVERSE


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            log.warning("State unreadable — starting flat.")
    return {"holdings": {}, "last_rebalance_month": None, "history": []}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Strategy ──────────────────────────────────────────────────────────────────

def rank(universe):
    """Top-N names by momentum that also pass the 200d trend gate."""
    scored = []
    for sym in universe:
        closes = get_adjusted_closes(sym)
        mom = momentum_score(closes)
        if mom is None or not above_trend(closes):
            continue
        scored.append((sym, mom))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:TOP_N]


def run():
    if not market_is_open():
        log.info("Market closed — skipping.")
        print("[EMERGING] Market closed")
        return

    state = load_state()
    this_month = datetime.now(timezone.utc).strftime("%Y-%m")
    if state.get("last_rebalance_month") == this_month:
        log.info(f"Already rebalanced for {this_month} — standing down.")
        print(f"[EMERGING] Already rebalanced this month ({this_month})")
        return

    universe = load_universe()
    top = rank(universe)
    if not top:
        log.warning("No names passed momentum+trend gate — staying in cash this month.")
        print("[EMERGING] No names passed the gate — cash")
        # still mark the month so we don't re-scan all day
        state["last_rebalance_month"] = this_month
        save_state(state)
        return

    target = {s for s, _ in top}
    held   = dict(state.get("holdings", {}))
    log.info("Ranked: " + " ".join(f"{s}={m:+.2%}" for s, m in top))
    log.info(f"Target {sorted(target)} | held {sorted(held)}")
    print(f"[EMERGING] target {sorted(target)} | held {sorted(held)}")

    # ── Sell names that dropped out (own qty only) ─────────────────────────────
    for sym in list(held):
        if sym in target:
            continue
        pos = get_position(sym)
        if not pos:
            held.pop(sym, None)
            continue
        owned = int(float(pos["qty"]))
        qty   = min(int(held[sym].get("qty", 0)), owned)
        if qty < 1:
            held.pop(sym, None)
            continue
        order = sell_qty(sym, qty)
        if order:
            px  = latest_price(sym) or float(pos["current_price"])
            pnl = (px - held[sym].get("entry_price", px)) * qty
            record_trade("emerging_growth", sym, pnl, "rotate out")
            log.info(f"SELL {qty} {sym} @ ~${px:.2f} | P&L ${pnl:+.2f}")
            print(f"[EMERGING] SELL {qty} {sym} | P&L ${pnl:+.2f}")
            held.pop(sym, None)

    # ── Buy new entrants ───────────────────────────────────────────────────────
    weight   = get_weight("emerging_growth")
    per_name = (ALLOCATION * weight) / TOP_N
    for sym in sorted(target):
        if sym in held and get_position(sym):
            continue
        px = latest_price(sym)
        if not px:
            log.warning(f"No price for {sym} — skipping.")
            continue
        ok, reason = can_enter("emerging_growth", sym, per_name)
        if not ok:
            log.warning(f"Risk guard blocked {sym}: {reason}")
            print(f"[EMERGING] Risk guard blocked {sym} — {reason}")
            continue
        order = buy_notional(sym, per_name)
        if order:
            qty = per_name / px
            held[sym] = {"qty": qty, "entry_price": px}
            log.info(f"BUY {sym} ~${per_name:.0f} (~{qty:.2f} sh @ ${px:.2f})")
            print(f"[EMERGING] BUY {sym} ~${per_name:.0f}")

    state["holdings"] = held
    state["last_rebalance_month"] = this_month
    state.setdefault("history", []).append({
        "month": this_month, "target": sorted(target),
        "scores": {s: round(m, 4) for s, m in top},
    })
    save_state(state)
    log.info(f"Rebalanced for {this_month} — holding {sorted(held)}")
    print(f"[EMERGING] Rebalanced — holding {sorted(held)}")


if __name__ == "__main__":
    run()
