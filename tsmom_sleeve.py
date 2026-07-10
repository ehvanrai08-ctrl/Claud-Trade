"""
TSMOM Sleeve Bot — multi-asset absolute trend-following (defensive diversifier)
==============================================================================
Each of 5 asset-class ETFs (SPY/TLT/GLD/DBC/UUP) gets its OWN long-flat trend
signal — ensembled 6/9/12-month total return > 0 — independent of the others
(Moskowitz-Ooi-Pedersen time-series momentum, long-only). A slot whose signal
is OFF parks in BIL. Equal slot weights, monthly rebalance.

⚠ HONEST STATUS (backtest_tsmom.py, 24-cell grid, 2016-2026): the trend TIMING
adds ~zero Sharpe over just equal-weight-holding the same basket untimed (null
Sharpe 1.19 vs best cell 1.19, ensemble cell 1.05-1.08). This is NOT alpha.
What IS real and grid-stable: the timing halves max drawdown (13.3% → ~7.8% in
every cell) and the out-of-sample half IMPROVED (Sharpe 0.86 IS → 1.32 OOS,
side-stepping 2022). Deployed small as the fleet's defensive leg — sub-8% DD,
low equity beta, SPY-like Sharpe — with the config chosen from the MIDDLE of
the grid (ensemble, no gate), not the best cell, to avoid selection bias.

Collision safety: SPY is also traded by rsi2/dual_momentum — this bot tracks
its own per-symbol qty and only ever sells what it bought (same pattern as
sector_momentum/superinvestor/emerging_growth), plus corporate-action
reconciliation and the shared risk guard on every buy.
"""

import json
import logging
import os
import time
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

STATE_FILE = f"{BASE_DIR}/tsmom_state.json"

ASSETS    = ["SPY", "TLT", "GLD", "DBC", "UUP"]   # one slot each
CASH      = "BIL"                                  # where an OFF slot parks
LOOKBACKS = [6, 9, 12]                             # ensembled months (~21 td each)
TDM       = 21
ALLOCATION = 6000    # $ sleeve → $1200/slot; scaled by capital weight
MIN_DELTA  = 100     # ignore rebalance deltas under $100 (noise vs slippage)

logging.basicConfig(
    filename=f"{BASE_DIR}/tsmom.log",
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


def get_adjusted_closes(symbol, days=460):
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = requests.get(f"https://data.alpaca.markets/v2/stocks/{symbol}/bars",
                     headers=DATA_HEADERS,
                     params={"timeframe": "1Day", "start": start, "limit": 1000,
                             "sort": "asc", "adjustment": "all"}, timeout=30)
    bars = r.json().get("bars") or [] if r.ok else []
    return [b["c"] for b in bars]


def signal_on(closes):
    """Ensembled 6/9/12-month total return > 0, or None if history is short."""
    need = max(LOOKBACKS) * TDM + 1
    if len(closes) < need:
        return None
    sigs = [closes[-1] / closes[-1 - m * TDM] - 1 for m in LOOKBACKS
            if closes[-1 - m * TDM] > 0]
    return (sum(sigs) / len(sigs)) > 0 if sigs else None


def latest_price(symbol):
    r = requests.get(f"https://data.alpaca.markets/v2/stocks/{symbol}/trades/latest",
                     headers=DATA_HEADERS, timeout=15)
    return float(r.json()["trade"]["p"]) if r.ok else None


def get_position(symbol):
    r = requests.get(f"{BASE_URL}/positions/{symbol}", headers=HEADERS, timeout=15)
    return r.json() if r.ok else None


def sell_qty(symbol, qty):
    """Sell exactly `qty` shares — fractional OK; never close_position."""
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
    """Actual (added_qty, avg_entry) read back from the broker after a buy;
    falls back to the estimate if the fill isn't visible yet."""
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
    return {"holdings": {}, "last_rebalance_month": None, "history": []}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def reconcile_corporate_actions(held):
    """Adopt broker qty/avg_entry after a split: cost basis is preserved while
    qty jumps, so 'same cost, different qty' identifies one (see the CRWD 4:1
    incident). Same logic as the other basket bots."""
    for sym, h in held.items():
        pos = get_position(sym)
        if not pos:
            continue
        try:
            b_qty = float(pos["qty"]); b_avg = float(pos["avg_entry_price"])
            s_qty = float(h.get("qty", 0)); s_ent = float(h.get("entry_price", 0))
        except (KeyError, TypeError, ValueError):
            continue
        if min(b_qty, b_avg, s_qty, s_ent) <= 0:
            continue
        if (abs(b_qty - s_qty) / s_qty > 0.20
                and abs(b_qty * b_avg - s_qty * s_ent) / (s_qty * s_ent) < 0.02):
            log.warning(f"{sym}: corporate action — adopting broker "
                        f"{b_qty:.4f} @ ${b_avg:.2f} (was {s_qty:.4f} @ ${s_ent:.2f})")
            h["qty"], h["entry_price"] = b_qty, b_avg


# ── Strategy ──────────────────────────────────────────────────────────────────

def target_notionals(per_slot):
    """{symbol: target $} from each asset's own trend signal. OFF/short-history
    slots park in BIL. Returns None if no signal could be computed at all."""
    targets, computed = {}, 0
    for sym in ASSETS:
        on = signal_on(get_adjusted_closes(sym))
        if on is None:
            log.warning(f"{sym}: insufficient history — slot parks in cash.")
            on = False
        else:
            computed += 1
        slot_sym = sym if on else CASH
        targets[slot_sym] = targets.get(slot_sym, 0.0) + per_slot
        log.info(f"slot {sym}: {'ON' if on else 'OFF -> ' + CASH}")
    return targets if computed else None


def run():
    if not market_is_open():
        log.info("Market closed — skipping.")
        print("[TSMOM] Market closed")
        return

    state = load_state()
    this_month = datetime.now(timezone.utc).strftime("%Y-%m")
    if state.get("last_rebalance_month") == this_month:
        log.info(f"Already rebalanced for {this_month} — standing down.")
        print(f"[TSMOM] Already rebalanced this month ({this_month})")
        return

    weight   = get_weight("tsmom")
    per_slot = (ALLOCATION * weight) / len(ASSETS)
    targets  = target_notionals(per_slot)
    if targets is None:
        log.warning("No signals computable — skipping (month left unmarked).")
        print("[TSMOM] No signals computable — skipping")
        return

    held = dict(state.get("holdings", {}))
    reconcile_corporate_actions(held)
    log.info(f"Targets: {{{', '.join(f'{s}: ${n:.0f}' for s, n in sorted(targets.items()))}}} "
             f"| held: {sorted(held)}")
    print(f"[TSMOM] targets {sorted(targets)} | held {sorted(held)}")

    complete = True   # any failed leg leaves the month unmarked → retried next run

    # ── Trim/exit: sell where held value exceeds target (own qty only) ─────────
    for sym in list(held):
        pos = get_position(sym)
        if not pos:
            qty   = float(held[sym].get("qty", 0))
            entry = held[sym].get("entry_price", 0)
            px    = latest_price(sym) or entry
            if qty > 0 and entry:
                record_trade("tsmom", sym, (px - entry) * qty,
                             "reconciled out (position vanished)")
            held.pop(sym, None)
            continue
        px = latest_price(sym) or float(pos["current_price"])
        own_qty  = round(min(float(held[sym].get("qty", 0)), float(pos["qty"])), 6)
        own_val  = own_qty * px
        target   = targets.get(sym, 0.0)
        excess   = own_val - target
        if excess <= MIN_DELTA or own_qty <= 0:
            continue
        qty = round(min(own_qty, excess / px), 6)
        order = sell_qty(sym, qty)
        if order:
            pnl = (px - held[sym].get("entry_price", px)) * qty
            record_trade("tsmom", sym, pnl, "trend off / rebalance trim")
            log.info(f"SELL {qty:.4f} {sym} @ ~${px:.2f} | P&L ${pnl:+.2f}")
            print(f"[TSMOM] SELL {qty:.4f} {sym} | P&L ${pnl:+.2f}")
            remaining = round(own_qty - qty, 6)
            if remaining > 0:
                held[sym]["qty"] = remaining
            else:
                held.pop(sym, None)
        else:
            complete = False

    # ── Top-up/enter: buy where target exceeds held value ──────────────────────
    for sym, target in sorted(targets.items()):
        px = latest_price(sym)
        if not px:
            log.warning(f"No price for {sym} — skipping buy.")
            complete = False
            continue
        own_qty = float(held.get(sym, {}).get("qty", 0))
        deficit = target - own_qty * px
        if deficit <= MIN_DELTA:
            continue
        ok, reason = can_enter("tsmom", sym, deficit)
        if not ok:
            log.warning(f"Risk guard blocked {sym}: {reason}")
            print(f"[TSMOM] Risk guard blocked {sym} — {reason}")
            complete = False
            continue
        prior_broker_qty = 0.0
        pos = get_position(sym)
        if pos:
            try:
                prior_broker_qty = float(pos["qty"])
            except (KeyError, TypeError, ValueError):
                pass
        order = buy_notional(sym, deficit)
        if order:
            added, entry = confirm_fill(sym, deficit, px, prior_broker_qty)
            if sym in held:
                old_q = float(held[sym].get("qty", 0))
                old_e = float(held[sym].get("entry_price", entry))
                new_q = old_q + added
                held[sym] = {"qty": new_q,
                             "entry_price": (old_q * old_e + added * entry) / new_q
                                            if new_q > 0 else entry}
            else:
                held[sym] = {"qty": added, "entry_price": entry}
            log.info(f"BUY {sym} ~${deficit:.0f} ({added:.4f} sh @ ${entry:.2f})")
            print(f"[TSMOM] BUY {sym} ~${deficit:.0f}")
        else:
            complete = False

    state["holdings"] = held
    if complete:
        state["last_rebalance_month"] = this_month
        state.setdefault("history", []).append({
            "month": this_month,
            "targets": {s: round(n, 2) for s, n in targets.items()},
        })
    else:
        log.warning("Rebalance incomplete — month unmarked, retrying next run.")
        print("[TSMOM] Rebalance incomplete — will retry next run")
    save_state(state)
    log.info(f"Done for {this_month} — holding {sorted(held)}")
    print(f"[TSMOM] Done — holding {sorted(held)}")


if __name__ == "__main__":
    run()
