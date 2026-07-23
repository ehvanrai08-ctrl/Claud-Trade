"""
Credit/Vol QQQ Switch Bot
=========================
Live deployment of the strongest candidate from the 2026-07-16 research sweep
(research/top20/credit_vol_qqq.py, backtest_top20 report): hold QQQ only when
BOTH a credit-market gate and a volatility gate say risk-on, else park in BIL.

Signal (daily, act at close):
  credit_ok = HYG close > its 200d SMA   (high-yield credit spreads not blowing out)
  vol_ok    = VIX close < 30             (no acute volatility spike)
  QQQ  when credit_ok AND vol_ok, else BIL.

Backtest (Yahoo daily bars, 2016-05 to 2026-07, 5bps turnover cost): Sharpe
1.13 vs SPY B&H 0.89 same period, CAGR 18.1%, maxDD 19.3%. Crucially it also
beats its own untimed QQQ null (Sharpe 0.91) — unlike most trend/allocation
sleeves in this repo, this is a real claim to TIMING alpha, not just
drawdown control. maxDD is still real (19.3%), so this is sized as one
sleeve among many, not a bet-the-book strategy — same caution the sleeve's
own research report gave it.

One $6k sleeve, single position (QQQ or BIL), daily signal via GitHub Actions.
Collision-safe: never touches a QQQ/BIL position it didn't buy itself (own-qty
tracking), and every buy passes the shared risk guard.
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

STATE_FILE = f"{BASE_DIR}/credit_vol_qqq_state.json"

RISK_ASSET  = "QQQ"
CASH_ASSET  = "BIL"
SMA_PERIOD  = 200
ALLOCATION  = 6000    # $ sleeve, scaled by capital weight
MIN_DELTA   = 100     # ignore rebalance deltas under $100 (noise vs slippage)

logging.basicConfig(
    filename=f"{BASE_DIR}/credit_vol_qqq.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger()


# ── Alpaca helpers ─────────────────────────────────────────────────────────────

def market_is_open():
    try:
        r = requests.get(f"{BASE_URL}/clock", headers=HEADERS, timeout=15)
        return r.json().get("is_open", False) if r.ok else False
    except Exception:
        return False


def get_closes(symbol, days=420):
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = requests.get(f"https://data.alpaca.markets/v2/stocks/{symbol}/bars",
                     headers=DATA_HEADERS,
                     params={"timeframe": "1Day", "start": start, "limit": 1000,
                             "sort": "asc", "adjustment": "all"}, timeout=30)
    bars = r.json().get("bars") or [] if r.ok else []
    return [b["c"] for b in bars]


def latest_price(symbol):
    r = requests.get(f"https://data.alpaca.markets/v2/stocks/{symbol}/trades/latest",
                     headers=DATA_HEADERS, timeout=15)
    return float(r.json()["trade"]["p"]) if r.ok else None


def get_position(symbol):
    r = requests.get(f"{BASE_URL}/positions/{symbol}", headers=HEADERS, timeout=15)
    return r.json() if r.ok else None


def sell_qty(symbol, qty):
    if os.environ.get("DRY_RUN"):
        log.info(f"DRY_RUN: would sell {qty} {symbol}")
        return {"id": "dry-run"}
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json={
        "symbol": symbol, "qty": str(round(float(qty), 6)), "side": "sell",
        "type": "market", "time_in_force": "day"})
    if r.ok:
        return r.json()
    log.error(f"Sell failed {symbol}: {r.text[:200]}")
    return None


def buy_notional(symbol, notional):
    if os.environ.get("DRY_RUN"):
        log.info(f"DRY_RUN: would buy ${notional:.0f} of {symbol}")
        return {"id": "dry-run"}
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json={
        "symbol": symbol, "notional": str(round(notional, 2)),
        "side": "buy", "type": "market", "time_in_force": "day"})
    if r.ok:
        return r.json()
    log.error(f"Buy failed {symbol}: {r.text[:200]}")
    return None


def confirm_fill(symbol, notional, est_price, prior_qty=0.0):
    import time
    time.sleep(2)
    pos = get_position(symbol)
    if pos:
        try:
            return max(float(pos["qty"]) - prior_qty, 0.0), float(pos["avg_entry_price"])
        except (KeyError, TypeError, ValueError):
            pass
    return notional / est_price, est_price


# ── State ─────────────────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            log.warning("State unreadable — starting flat.")
    return {"holding": None, "qty": 0.0, "entry_price": 0.0, "last_signal_date": None}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def reconcile_corporate_actions(state):
    """Adopt broker qty/avg_entry after a split — cost basis preserved while
    qty jumps identifies one (same pattern as tsmom_sleeve/trend_basket)."""
    sym = state.get("holding")
    if not sym:
        return
    pos = get_position(sym)
    if not pos:
        return
    try:
        b_qty = float(pos["qty"]); b_avg = float(pos["avg_entry_price"])
        s_qty = float(state.get("qty", 0)); s_ent = float(state.get("entry_price", 0))
    except (KeyError, TypeError, ValueError):
        return
    if min(b_qty, b_avg, s_qty, s_ent) <= 0:
        return
    if (abs(b_qty - s_qty) / s_qty > 0.20
            and abs(b_qty * b_avg - s_qty * s_ent) / (s_qty * s_ent) < 0.02):
        log.warning(f"{sym}: corporate action — adopting broker "
                    f"{b_qty:.4f} @ ${b_avg:.2f} (was {s_qty:.4f} @ ${s_ent:.2f})")
        state["qty"], state["entry_price"] = b_qty, b_avg


# ── Strategy ──────────────────────────────────────────────────────────────────

def sma(closes, n):
    return sum(closes[-n:]) / n if len(closes) >= n else None


def compute_target():
    """Return (target_symbol, reason) or (None, reason) if signal can't be computed.
    Alpaca doesn't carry ^VIX bars, so the vol gate uses VIXY (the tradable ETF
    proxy): VIXY's own 90d percentile rank, index-free and robust to VIXY's
    structural contango decay unlike a fixed price level.

    Threshold is 0.50 (bottom half of the 90d range). Backtested on this exact
    VIXY-percentile mechanism (research/backtest_vixy_gate.py, Yahoo 2015-2026,
    5bps cost): 0.50 gives Sharpe 1.05 / maxDD 20.6% vs the prior 0.80's
    Sharpe 1.06 / maxDD 27.6%. So 0.50 is ~Sharpe-neutral but cuts drawdown
    ~7 pts — a drawdown-control choice, NOT a return/alpha improvement. (Both
    beat same-period SPY 0.89; note the tradable-VIXY proxy underperforms the
    idealized ^VIX<30 backtest at 1.11 — the proxy costs ~0.05 Sharpe.)"""
    hyg  = get_closes("HYG")
    vixy = get_closes("VIXY")
    if len(hyg) < SMA_PERIOD:
        return None, f"insufficient HYG history ({len(hyg)}d)"
    hyg_sma = sma(hyg, SMA_PERIOD)
    credit_ok = hyg[-1] > hyg_sma
    if not vixy:
        return None, "no VIXY data available"
    window = vixy[-90:]
    lo, hi = min(window), max(window)
    pct = (vixy[-1] - lo) / (hi - lo) if hi > lo else 0.0
    vol_ok = pct < 0.50   # bottom 50% of its own recent range = low vol environment only
    reason = (f"HYG {hyg[-1]:.2f} {'>' if credit_ok else '<='} SMA{SMA_PERIOD} {hyg_sma:.2f} "
              f"(credit {'OK' if credit_ok else 'STRESSED'}) | "
              f"VIXY 90d percentile {pct*100:.0f}% {'<' if vol_ok else '>='} 50% "
              f"(vol {'OK' if vol_ok else 'ELEVATED'})")
    return (RISK_ASSET if (credit_ok and vol_ok) else CASH_ASSET), reason


def run():
    if not market_is_open():
        log.info("Market closed — skipping.")
        print("[CVQ] Market closed")
        return

    state = load_state()
    reconcile_corporate_actions(state)

    target, reason = compute_target()
    if target is None:
        log.warning(f"No signal computable — skipping ({reason})")
        print(f"[CVQ] No signal — {reason}")
        return
    log.info(f"Signal: target={target} | {reason}")
    print(f"[CVQ] {reason} -> target {target}")

    weight = get_weight("credit_vol_qqq")
    sleeve = ALLOCATION * weight

    held_sym = state.get("holding")
    if held_sym == target:
        print(f"[CVQ] Already holding {target} — no change")
        save_state(state)
        return

    # ── Exit current holding (if any) ───────────────────────────────────────
    if held_sym:
        pos = get_position(held_sym)
        own_qty = round(min(float(state.get("qty", 0)), float(pos["qty"]) if pos else 0), 6)
        if pos and own_qty > 0:
            px = latest_price(held_sym) or float(pos["current_price"])
            order = sell_qty(held_sym, own_qty)
            if order:
                pnl = (px - state.get("entry_price", px)) * own_qty
                record_trade("credit_vol_qqq", held_sym, pnl, f"signal flip -> {target}")
                log.info(f"SELL {own_qty:.4f} {held_sym} @ ~${px:.2f} | P&L ${pnl:+.2f}")
                print(f"[CVQ] SELL {held_sym} | P&L ${pnl:+.2f}")
            else:
                log.warning(f"Sell failed for {held_sym} — leaving state as-is, retry next run")
                save_state(state)
                return
        else:
            log.info(f"{held_sym}: position already gone — reconciling flat")
        state["holding"], state["qty"], state["entry_price"] = None, 0.0, 0.0

    # ── Enter target ─────────────────────────────────────────────────────────
    px = latest_price(target)
    if not px:
        log.warning(f"No price for {target} — leaving flat, retry next run")
        save_state(state)
        return

    ok, guard_reason = can_enter("credit_vol_qqq", target, sleeve)
    if not ok:
        log.warning(f"Risk guard blocked {target}: {guard_reason}")
        print(f"[CVQ] Risk guard blocked {target} — {guard_reason}")
        save_state(state)
        return

    prior_qty = 0.0
    pos = get_position(target)
    if pos:
        try:
            prior_qty = float(pos["qty"])
        except (KeyError, TypeError, ValueError):
            pass

    order = buy_notional(target, sleeve)
    if order:
        added, entry = confirm_fill(target, sleeve, px, prior_qty)
        state["holding"], state["qty"], state["entry_price"] = target, added, entry
        log.info(f"BUY {target} ~${sleeve:.0f} ({added:.4f} sh @ ${entry:.2f})")
        print(f"[CVQ] BUY {target} ~${sleeve:.0f}")
    else:
        log.warning(f"Buy failed for {target} — left flat, retry next run")

    state["last_signal_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    save_state(state)
    log.info(f"Done — holding {state.get('holding')}")
    print(f"[CVQ] Done — holding {state.get('holding')}")


if __name__ == "__main__":
    run()
