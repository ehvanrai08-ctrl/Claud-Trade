"""
Connors RSI(2) Bot — SPY
========================
Larry Connors' 2-period RSI mean reversion, a documented high-win-rate edge.
Backtest (SPY 2015-2026, backtest_candidates.py): 72% win rate, PF 1.38.

Rule (decided once per day near the close, holds multi-day):
  - BUY  SPY when RSI(2) < 10  AND  Close > 200-day SMA  (oversold in an uptrend)
  - SELL SPY when Close > 5-day SMA                       (snap-back captured)
The 200-day regime filter is essential — it keeps the bot out of bear markets,
where buying oversold dips is catching falling knives.

COLLISION SAFETY (Dual Momentum & SIP-ORB can also touch SPY):
  - Defers entry if a SPY position already exists (another bot owns it).
  - On exit, sells EXACTLY the quantity this bot bought — never close_position().
  - Reconciles if its position vanishes: marks flat and records at last price.

Runs once daily at ~3:50 PM ET. Holds overnight.
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
from capital_allocator import get_weight
from premarket import read_signals

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
STATE_FILE = f"{BASE_DIR}/rsi2_state.json"

SYMBOL    = "SPY"
NOTIONAL  = 2000
RSI_BUY   = 10        # RSI(2) below this = oversold
SMA_TREND = 200       # only buy above this
SMA_EXIT  = 5         # sell when close clears this

logging.basicConfig(
    filename=f"{BASE_DIR}/rsi2.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger()


# ── Data ──────────────────────────────────────────────────────────────────────

def market_is_open():
    r = requests.get(f"{BASE_URL}/clock", headers=HEADERS)
    return r.json().get("is_open", False)


def daily_closes(days=320):
    """Daily bars (oldest first); the last is today's forming bar.
    Returns the full bar list, or [] if stale/unavailable."""
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = requests.get(
        f"https://data.alpaca.markets/v2/stocks/{SYMBOL}/bars",
        headers=DATA_HEADERS,
        # Raw (not adjusted) so the 200-day SMA is consistent with the live raw
        # price we splice in as today's close — mixing adjusted history with a raw
        # live price would bias the close>SMA200 regime gate.
        params={"timeframe": "1Day", "start": start, "limit": 400,
                "sort": "asc", "adjustment": "raw"},
    )
    bars = r.json().get("bars") or [] if r.ok else []
    if not bars:
        return []
    last_date = datetime.fromisoformat(bars[-1]["t"].replace("Z", "+00:00")).astimezone(ET).date()
    if last_date != datetime.now(ET).date():
        log.warning(f"Latest bar is {last_date}, not today — skipping (stale/holiday).")
        return []
    return bars


def rsi(closes, period=2):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(-period, 0):
        ch = closes[i] - closes[i-1]
        gains.append(max(ch, 0)); losses.append(max(-ch, 0))
    avg_g = sum(gains) / period
    avg_l = sum(losses) / period
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def sma(closes, n):
    return sum(closes[-n:]) / n if len(closes) >= n else None


def latest_price():
    r = requests.get(
        f"https://data.alpaca.markets/v2/stocks/{SYMBOL}/trades/latest",
        headers=DATA_HEADERS,
    )
    return float(r.json()["trade"]["p"]) if r.ok else None


# ── Orders ────────────────────────────────────────────────────────────────────

def get_position(symbol):
    """Position dict, None if genuinely flat (404), or "ERROR" if the lookup
    failed. Treating an API error as "flat" would fabricate a reconcile trade
    and orphan a real position."""
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
        print("[RSI2] Market closed")
        return

    state = load_state()
    bars  = daily_closes()
    if len(bars) < SMA_TREND + 1:
        log.info(f"Insufficient history ({len(bars)} bars) — skipping.")
        print("[RSI2] Insufficient history")
        return

    closes = [b["c"] for b in bars]
    price  = latest_price() or closes[-1]
    closes[-1] = price                       # use the live price as today's close
    r2     = rsi(closes, 2)
    s200   = sma(closes, SMA_TREND)
    s5     = sma(closes, SMA_EXIT)
    pos    = get_position(SYMBOL)
    if pos == "ERROR":
        log.warning("Position lookup failed — skipping run to avoid acting on bad data.")
        print("[RSI2] Position lookup failed — skipping")
        return
    log.info(f"RSI2={r2:.1f} price=${price:.2f} SMA200=${s200:.2f} SMA5=${s5:.2f} "
             f"holding={state['holding']} position={'yes' if pos else 'no'}")

    flat_state = {"holding": False, "entry_price": None, "entry_qty": 0, "entry_date": None}

    if state["holding"]:
        if not pos:
            pnl = (price - state["entry_price"]) * state["entry_qty"]
            record_trade("rsi2_spy", SYMBOL, pnl, "reconciled (position vanished)")
            log.warning(f"Position gone — reconciled. P&L ${pnl:+.2f}")
            save_state(flat_state)
            return
        if price > s5:                       # exit: close above 5-day SMA
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
                record_trade("rsi2_spy", SYMBOL, pnl, "exit close>SMA5")
                log.info(f"SELL {qty} {SYMBOL} @ ~${exit_p:.2f} | P&L ${pnl:+.2f}")
                print(f"[RSI2] SELL {qty} {SYMBOL} | P&L ${pnl:+.2f}")
                save_state(flat_state)
        else:
            log.info(f"Holding {state['entry_qty']} {SYMBOL} — ${price:.2f} not > SMA5 ${s5:.2f}")
        return

    # Flat: defer if SPY already held by another bot (Dual Momentum, SIP-ORB).
    if pos:
        log.info(f"{SYMBOL} already held by another strategy (qty {pos.get('qty')}) — deferring.")
        print(f"[RSI2] {SYMBOL} held elsewhere — deferring")
        return

    if r2 < RSI_BUY and price > s200:
        # Scale notional by market regime (pre-market screener) and capital weight.
        # RSI2 already has a hard 200d SMA bear-market block; regime adds size scaling.
        # bull 1.25×: SPY oversold in a strong uptrend — highest-conviction setup.
        # neutral 1.0×: standard size.
        # bear 0.5×: oversold but trend broken; size down, the 200d gate may still pass.
        signals = read_signals()
        regime  = signals.get("market_regime", "neutral")
        regime_mult = {"bull": 1.25, "neutral": 1.0, "bear": 0.5}.get(regime, 1.0)
        alloc_mult  = get_weight("rsi2")
        notional = NOTIONAL * regime_mult * alloc_mult
        qty = int(notional // price)
        if qty < 1:
            log.warning(f"Notional ${notional:.0f} too small at ${price:.2f} (regime={regime})")
            return
        order = submit_market("buy", qty)
        if order:
            entry_p = fill_price(order["id"], price)
            save_state({"holding": True, "entry_price": entry_p, "entry_qty": qty,
                        "entry_date": datetime.now(ET).strftime("%Y-%m-%d")})
            log.info(f"BUY {qty} {SYMBOL} @ ~${entry_p:.2f} | RSI2 {r2:.1f} > SMA200 "
                     f"regime={regime} ({regime_mult}×) alloc={alloc_mult}×")
            print(f"[RSI2] BUY {qty} {SYMBOL} @ ~${entry_p:.2f} | RSI2 {r2:.1f} | "
                  f"regime={regime} notional=${notional:.0f}")
    else:
        reason = (f"RSI2 {r2:.1f} not < {RSI_BUY}" if r2 >= RSI_BUY
                  else f"price ${price:.2f} below SMA200 ${s200:.2f} (bear regime)")
        log.info(f"No entry — {reason}")
        print(f"[RSI2] No entry — {reason}")


if __name__ == "__main__":
    run()
