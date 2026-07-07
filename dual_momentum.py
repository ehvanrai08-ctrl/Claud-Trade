"""
Dual Momentum Bot (GEM — Global Equities Momentum)
==================================================
Gary Antonacci's Global Equities Momentum, the canonical dual-momentum rule.
Combines RELATIVE momentum (pick the strongest equity market) with ABSOLUTE
momentum (only hold equities if they're beating cash; otherwise hide in bonds).
Holds exactly ONE asset at a time. Monthly rebalance, ~1.5 trades/year.

Documented edge (Antonacci, 1974–2013): CAGR 17.4%, Sharpe 0.87, max drawdown
−22.7% vs −51% for buy-and-hold the S&P 500. The absolute-momentum gate is what
sidesteps bear markets — it sat in bonds through 2008.

Monthly rule (run near month-start, acts once per calendar month):
  1. Momentum score = trailing total return, ENSEMBLED over 6–12 month lookbacks
     to avoid the well-documented single-lookback fragility (Newfound). Total
     return uses dividend/split-adjusted prices.
  2. Absolute gate: if score(SPY) > score(BIL, the cash proxy) → risk-on;
     else → hold AGG (bonds) entirely.
  3. Relative (only if risk-on): hold whichever of SPY vs EFA scored higher.
  4. Rebalance the sleeve into that single asset.

Never shorts; risk-off = 100% bonds. Long-only sidesteps the momentum-crash
tail (which lives on the short-losers leg).
"""

import json
import logging
import os
import requests
from datetime import datetime, timedelta, timezone
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

STATE_FILE = f"{BASE_DIR}/dual_momentum_state.json"

US_EQUITY = "SPY"   # US stocks
EX_US     = "EFA"   # developed ex-US stocks
BONDS     = "AGG"   # US aggregate bonds (the risk-off sleeve)
CASH      = "BIL"   # 1–3 month T-bill proxy (absolute-momentum benchmark)

# Ensemble of monthly lookbacks (~21 trading days each) — averaging the score
# across these hardens GEM against single-lookback "timing luck".
LOOKBACK_MONTHS = [6, 7, 8, 9, 10, 11, 12]
TRADING_DAYS_PER_MONTH = 21

ALLOCATION = 10000   # $ sleeve dedicated to this strategy

logging.basicConfig(
    filename=f"{BASE_DIR}/dual_momentum.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger()


# ── Data helpers ──────────────────────────────────────────────────────────────

def market_is_open():
    try:
        r = requests.get(f"{BASE_URL}/clock", headers=HEADERS, timeout=15)
        return r.json().get("is_open", False) if r.ok else False
    except Exception:
        return False


def get_adjusted_closes(symbol, days=420):
    """Dividend/split-adjusted daily closes (total-return proxy), oldest first."""
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = requests.get(
        f"https://data.alpaca.markets/v2/stocks/{symbol}/bars",
        headers=DATA_HEADERS,
        params={"timeframe": "1Day", "start": start, "limit": 500,
                "sort": "asc", "adjustment": "all"},
        timeout=30,
    )
    bars = r.json().get("bars") or [] if r.ok else []
    return [b["c"] for b in bars]


def momentum_score(closes):
    """Average trailing total return across the lookback ensemble.
    Returns None if we don't have enough history for the longest lookback."""
    longest = max(LOOKBACK_MONTHS) * TRADING_DAYS_PER_MONTH
    if len(closes) < longest + 1:
        return None
    now = closes[-1]
    rets = []
    for m in LOOKBACK_MONTHS:
        back = m * TRADING_DAYS_PER_MONTH
        past = closes[-1 - back]
        if past > 0:
            rets.append(now / past - 1)
    return sum(rets) / len(rets) if rets else None


# ── Order helpers ─────────────────────────────────────────────────────────────

def get_position(symbol):
    r = requests.get(f"{BASE_URL}/positions/{symbol}", headers=HEADERS, timeout=15)
    return r.json() if r.ok else None


def close_position(symbol):
    pos = get_position(symbol)
    pnl = float(pos["unrealized_pl"]) if pos else 0.0
    if os.environ.get("DRY_RUN"):
        log.info(f"DRY_RUN: would close {symbol} (unrealized ${pnl:+.2f})")
        return True
    r = requests.delete(f"{BASE_URL}/positions/{symbol}", headers=HEADERS, timeout=15)
    if r.ok:
        log.info(f"CLOSED {symbol} | realized ~${pnl:+.2f}")
        record_trade("dual_momentum", symbol, pnl, "rebalance out")
        return True
    log.error(f"Close failed {symbol}: {r.text[:200]}")
    return False


def buy_notional(symbol, notional):
    if os.environ.get("DRY_RUN"):
        log.info(f"DRY_RUN: would buy ${notional:.0f} of {symbol}")
        return {"id": "dry-run"}
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, timeout=15, json={
        "symbol":        symbol,
        "notional":      str(round(notional, 2)),
        "side":          "buy",
        "type":          "market",
        "time_in_force": "day",
    })
    if r.ok:
        log.info(f"BUY {symbol} ~${notional:.0f} | order {r.json()['id']}")
        return r.json()
    log.error(f"Buy failed {symbol}: {r.text[:200]}")
    return None


# ── State ─────────────────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"holding": None, "last_rebalance_month": None, "history": []}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Strategy ──────────────────────────────────────────────────────────────────

def decide_target():
    """Return (target_symbol, reason) per GEM, or (None, reason) if data is
    insufficient to act safely."""
    # One batched request instead of three serial round-trips (smaller
    # partial-failure window; broker.get_bars_multi paginates for us).
    # AGG is deliberately NOT scored: GEM only scores SPY/EFA/CASH — bonds are
    # the unconditional risk-off destination, not a momentum contestant.
    from broker import Broker
    start = (datetime.now(timezone.utc) - timedelta(days=420)).strftime("%Y-%m-%d")
    multi = Broker().get_bars_multi([US_EQUITY, EX_US, CASH], "1Day", start,
                                    limit=500, adjustment="all")
    scores = {sym: momentum_score([b["c"] for b in (multi.get(sym) or [])])
              for sym in (US_EQUITY, EX_US, CASH)}

    if any(scores[s] is None for s in (US_EQUITY, EX_US, CASH)):
        return None, f"insufficient history: {scores}"

    spy, exus, cash = scores[US_EQUITY], scores[EX_US], scores[CASH]
    log.info(f"Scores: {US_EQUITY}={spy:+.3f} {EX_US}={exus:+.3f} {CASH}(cash)={cash:+.3f}")

    # Absolute gate driven by US equity vs cash.
    if spy <= cash:
        return BONDS, f"risk-off (SPY {spy:+.2%} ≤ cash {cash:+.2%}) → bonds"
    # Relative momentum: strongest equity market.
    if spy >= exus:
        return US_EQUITY, f"risk-on, US leads (SPY {spy:+.2%} ≥ EFA {exus:+.2%})"
    return EX_US, f"risk-on, ex-US leads (EFA {exus:+.2%} > SPY {spy:+.2%})"


def run():
    if not market_is_open():
        log.info("Market closed — skipping.")
        return

    state = load_state()
    this_month = datetime.now(timezone.utc).strftime("%Y-%m")
    if state.get("last_rebalance_month") == this_month:
        log.info(f"Already rebalanced for {this_month} — standing down.")
        print(f"[DM] Already rebalanced this month ({this_month})")
        return

    target, reason = decide_target()
    if target is None:
        log.warning(f"No rebalance — {reason}")
        print(f"[DM] No action — {reason}")
        return

    current = state.get("holding")
    log.info(f"Target={target} ({reason}) | currently holding {current}")
    print(f"[DM] {reason} | target {target}, holding {current}")

    if current == target and get_position(target):
        log.info("Already in target asset — no trade, marking month done.")
        state["last_rebalance_month"] = this_month
        save_state(state)
        return

    # Rotate: close the old sleeve, buy the new one.
    if current and get_position(current):
        close_position(current)

    order = buy_notional(target, ALLOCATION)
    if order:
        state["holding"] = target
        state["last_rebalance_month"] = this_month
        state.setdefault("history", []).append({
            "month": this_month, "asset": target, "reason": reason,
        })
        log.info(f"REBALANCED into {target}")
        print(f"[DM] REBALANCED into {target} — {reason}")
        save_state(state)
    else:
        log.error("Buy failed — leaving state untouched to retry next run.")


if __name__ == "__main__":
    run()
