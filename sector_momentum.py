"""
Sector Momentum Rotation Bot
============================
Cross-sectional momentum across the 11 US sector SPDRs — the one strategy that
survived `backtest_research.py` against a brutal 2016-2026 SPY buy-and-hold bar.

Backtest (Alpaca daily, 2016-2026, in `backtest_research.py`):
  - Top 3 of 11 sectors by 12-month momentum, monthly rebalance.
  - Full-period Sharpe 1.03 vs SPY 0.88; max drawdown 18% vs SPY's 34%.
  - ROBUST across the lookback×top_n grid (Sharpe 0.78-1.16 every cell — not a
    single-cell fluke, unlike TJR/gold which we tested and shelved).
  - Honest caveat: it beat SPY in-sample but SPY's mega-cap run beat it
    out-of-sample on RAW return (14.95% vs 21.79%). This is a risk-adjusted /
    drawdown / diversification leg — SPY-like returns with ~half the drawdown —
    NOT a guaranteed index-beater in tech-led melt-ups. Sized modestly for that
    reason. Complements (does not duplicate) dual_momentum's 2-asset GEM.

Monthly rule (runs near month-start, acts once per calendar month):
  1. Momentum score = trailing total return, ENSEMBLED over 9-12 month lookbacks
     (single-lookback fragility is well documented — Newfound "timing luck").
     Uses dividend/split-adjusted closes.
  2. Rank all 11 sectors; the target basket is the top 3, equal-weighted.
  3. Rebalance: sell held sectors that dropped out of the top 3, buy new entrants.

Collision-safe: no other bot trades the sector SPDRs, but the bot still tracks
its own per-symbol qty in state and only ever sells what it holds. Position sizing
scales by the dynamic capital weight; every buy passes the portfolio risk guard.
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

STATE_FILE = f"{BASE_DIR}/sector_momentum_state.json"

# The 11 US sector SPDRs (the cross-sectional universe).
SECTORS = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLP", "XLY", "XLU", "XLB", "XLRE", "XLC"]
TOP_N   = 3

# Ensemble of monthly lookbacks (~21 trading days each). 9-12 months brackets the
# strongest, most robust region of the backtest grid (231-252d) while averaging
# out single-lookback timing luck.
LOOKBACK_MONTHS = [9, 10, 11, 12]
TRADING_DAYS_PER_MONTH = 21

ALLOCATION = 9000   # $ total sleeve → $3000 per sector at top-3 equal weight

logging.basicConfig(
    filename=f"{BASE_DIR}/sector_momentum.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger()


# ── Data helpers ──────────────────────────────────────────────────────────────

def market_is_open():
    r = requests.get(f"{BASE_URL}/clock", headers=HEADERS)
    return r.json().get("is_open", False)


def get_adjusted_closes(symbol, days=420):
    """Dividend/split-adjusted daily closes (total-return proxy), oldest first."""
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = requests.get(
        f"https://data.alpaca.markets/v2/stocks/{symbol}/bars",
        headers=DATA_HEADERS,
        params={"timeframe": "1Day", "start": start, "limit": 500,
                "sort": "asc", "adjustment": "all"},
    )
    bars = r.json().get("bars") or [] if r.ok else []
    return [b["c"] for b in bars]


def momentum_score(closes):
    """Average trailing total return across the lookback ensemble, or None if we
    lack enough history for the longest lookback."""
    longest = max(LOOKBACK_MONTHS) * TRADING_DAYS_PER_MONTH
    if len(closes) < longest + 1:
        return None
    now = closes[-1]
    rets = []
    for m in LOOKBACK_MONTHS:
        past = closes[-1 - m * TRADING_DAYS_PER_MONTH]
        if past > 0:
            rets.append(now / past - 1)
    return sum(rets) / len(rets) if rets else None


def latest_price(symbol):
    r = requests.get(f"https://data.alpaca.markets/v2/stocks/{symbol}/trades/latest",
                     headers=DATA_HEADERS)
    return float(r.json()["trade"]["p"]) if r.ok else None


# ── Order helpers ─────────────────────────────────────────────────────────────

def get_position(symbol):
    r = requests.get(f"{BASE_URL}/positions/{symbol}", headers=HEADERS)
    return r.json() if r.ok else None


def sell_qty(symbol, qty):
    """Sell exactly `qty` shares (never close_position — protects against any
    shared position, and keeps us selling only what this bot bought)."""
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json={
        "symbol": symbol, "qty": str(qty), "side": "sell",
        "type": "market", "time_in_force": "day",
    })
    if r.ok:
        return r.json()
    log.error(f"Sell failed {symbol}: {r.text[:200]}")
    return None


def buy_notional(symbol, notional):
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json={
        "symbol": symbol, "notional": str(round(notional, 2)),
        "side": "buy", "type": "market", "time_in_force": "day",
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
            log.warning("State file unreadable — starting flat.")
    return {"holdings": {}, "last_rebalance_month": None, "history": []}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Strategy ──────────────────────────────────────────────────────────────────

def rank_sectors():
    """Return (ranked_list, scores) where ranked_list is sectors sorted by
    momentum desc, excluding any with insufficient history."""
    scores = {}
    for sym in SECTORS:
        s = momentum_score(get_adjusted_closes(sym))
        if s is not None:
            scores[sym] = s
    ranked = sorted(scores, key=lambda s: scores[s], reverse=True)
    return ranked, scores


def run():
    if not market_is_open():
        log.info("Market closed — skipping.")
        print("[SECMOM] Market closed")
        return

    state = load_state()
    this_month = datetime.now(timezone.utc).strftime("%Y-%m")
    if state.get("last_rebalance_month") == this_month:
        log.info(f"Already rebalanced for {this_month} — standing down.")
        print(f"[SECMOM] Already rebalanced this month ({this_month})")
        return

    ranked, scores = rank_sectors()
    if len(ranked) < TOP_N:
        log.warning(f"Insufficient history — only {len(ranked)} sectors scored.")
        print("[SECMOM] Insufficient history — skipping")
        return

    target = set(ranked[:TOP_N])
    held   = dict(state.get("holdings", {}))   # sym -> {qty, entry_price}
    log.info("Scores: " + " ".join(f"{s}={scores[s]:+.2%}" for s in ranked))
    log.info(f"Target top{TOP_N}: {sorted(target)} | currently held: {sorted(held)}")
    print(f"[SECMOM] target {sorted(target)} | held {sorted(held)}")

    # ── Sell sectors that dropped out of the top N (our own qty only) ──────────
    for sym in list(held):
        if sym in target:
            continue
        pos = get_position(sym)
        if not pos:
            # Position vanished — reconcile out of state without a phantom trade.
            log.warning(f"{sym} in state but no live position — reconciling out.")
            held.pop(sym, None)
            continue
        owned = int(float(pos["qty"]))
        qty   = min(int(held[sym].get("qty", 0)), owned)
        if qty < 1:
            held.pop(sym, None)
            continue
        order = sell_qty(sym, qty)
        if order:
            px = latest_price(sym) or float(pos["current_price"])
            pnl = (px - held[sym].get("entry_price", px)) * qty
            record_trade("sector_momentum", sym, pnl, "rotate out")
            log.info(f"SELL {qty} {sym} @ ~${px:.2f} | P&L ${pnl:+.2f}")
            print(f"[SECMOM] SELL {qty} {sym} | P&L ${pnl:+.2f}")
            held.pop(sym, None)

    # ── Buy new entrants up to the target basket ───────────────────────────────
    weight   = get_weight("sector_momentum")
    per_name = (ALLOCATION * weight) / TOP_N
    for sym in target:
        if sym in held and get_position(sym):
            continue   # already hold it — let it ride
        px = latest_price(sym)
        if not px:
            log.warning(f"No price for {sym} — skipping buy.")
            continue
        ok, reason = can_enter("sector_momentum", sym, per_name)
        if not ok:
            log.warning(f"Risk guard blocked {sym}: {reason}")
            print(f"[SECMOM] Risk guard blocked {sym} — {reason}")
            continue
        order = buy_notional(sym, per_name)
        if order:
            qty = per_name / px
            held[sym] = {"qty": qty, "entry_price": px}
            log.info(f"BUY {sym} ~${per_name:.0f} (~{qty:.2f} sh @ ${px:.2f})")
            print(f"[SECMOM] BUY {sym} ~${per_name:.0f}")

    state["holdings"] = held
    state["last_rebalance_month"] = this_month
    state.setdefault("history", []).append({
        "month": this_month, "target": sorted(target),
        "scores": {s: round(scores[s], 4) for s in ranked[:TOP_N]},
    })
    save_state(state)
    log.info(f"Rebalanced for {this_month} — now holding {sorted(held)}")
    print(f"[SECMOM] Rebalanced — holding {sorted(held)}")


if __name__ == "__main__":
    run()
