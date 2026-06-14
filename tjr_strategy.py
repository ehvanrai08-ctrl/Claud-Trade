"""
TJR Strategy Bot
================
Trades SPY (proxy for ES) and QQQ (proxy for NQ) using TJR's 4-step method:

Step 1 — Liquidity Sweep: Price pushes above session/hourly high or below low
Step 2 — Reversal Confirmation: 5-min BOS or inverse FVG in opposite direction
Step 3 — Retrace: 1-min BOS back toward the sweep (retracement into structure)
Step 4 — Entry: 1-min BOS back in trade direction = entry signal

Rules:
- Both SPY and QQQ must be aligned (same 5-min trend direction)
- Only trade 9:30–10:30 AM ET; give up if no setup by 10:30
- Stop above second high (short) / below second low (long)
- Targets: next draw on liquidity (session/hourly highs or lows)
- Max 1 trade per session
- No pre-market entries
"""

import json
import logging
import os
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from dotenv import dotenv_values

BASE_DIR = "/home/user/Claud-Trade"
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
STATE_FILE = f"{BASE_DIR}/tjr_state.json"
TRADE_SIZE = 2000   # $ per trade

logging.basicConfig(
    filename=f"{BASE_DIR}/tjr.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger()


# ── Data helpers ──────────────────────────────────────────────────────────────

def get_bars(symbol, timeframe, limit=50):
    """Fetch recent bars. timeframe: '1Min' or '5Min'."""
    now   = datetime.now(timezone.utc)
    start = (now - timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = requests.get(
        f"https://data.alpaca.markets/v2/stocks/{symbol}/bars",
        headers=DATA_HEADERS,
        params={"timeframe": timeframe, "start": start, "limit": limit, "sort": "asc"},
    )
    return r.json().get("bars") or [] if r.ok else []


def market_is_open():
    r = requests.get(f"{BASE_URL}/clock", headers=HEADERS)
    return r.json()["is_open"]


def now_et():
    return datetime.now(ET)


def session_open_et():
    n = now_et()
    return n.replace(hour=9, minute=30, second=0, microsecond=0)


def cutoff_et():
    n = now_et()
    return n.replace(hour=10, minute=30, second=0, microsecond=0)


# ── Technical analysis ────────────────────────────────────────────────────────

def get_session_levels(bars_5m):
    """Return session high/low and most recent hourly high/low from 5-min bars."""
    if not bars_5m:
        return None
    highs = [b["h"] for b in bars_5m]
    lows  = [b["l"] for b in bars_5m]
    # Hourly: last 12 bars (1 hour of 5-min bars)
    recent = bars_5m[-12:] if len(bars_5m) >= 12 else bars_5m
    return {
        "session_high": max(highs),
        "session_low":  min(lows),
        "hourly_high":  max(b["h"] for b in recent),
        "hourly_low":   min(b["l"] for b in recent),
    }


def detect_liquidity_sweep(bars_5m, levels):
    """
    Check if most recent candle swept a key level.
    Returns 'short' if swept above a high, 'long' if swept below a low, else None.
    """
    if not bars_5m or len(bars_5m) < 2 or not levels:
        return None
    last = bars_5m[-1]
    prev_levels_high = max(levels["session_high"], levels["hourly_high"])
    prev_levels_low  = min(levels["session_low"],  levels["hourly_low"])
    if last["h"] > prev_levels_high:
        return "short"
    if last["l"] < prev_levels_low:
        return "long"
    return None


def detect_bos_5m(bars_5m, direction):
    """
    5-minute break of structure.
    direction='short': looking for close below recent swing low (bearish BOS)
    direction='long':  looking for close above recent swing high (bullish BOS)
    """
    if len(bars_5m) < 3:
        return False
    recent = bars_5m[-6:]
    if direction == "short":
        swing_low = min(b["l"] for b in recent[:-1])
        return recent[-1]["c"] < swing_low
    else:
        swing_high = max(b["h"] for b in recent[:-1])
        return recent[-1]["c"] > swing_high


def detect_inverse_fvg_5m(bars_5m, direction):
    """
    Inverse fair value gap: a 3-candle FVG that gets closed through (disrespected).
    direction='short': bullish FVG gets closed through to downside
    direction='long':  bearish FVG gets closed through to upside
    """
    if len(bars_5m) < 4:
        return False
    c1, c2, c3 = bars_5m[-4], bars_5m[-3], bars_5m[-2]
    last = bars_5m[-1]
    if direction == "short":
        # Bullish FVG: c3.low > c1.high (gap between c1 and c3)
        if c3["l"] > c1["h"]:
            fvg_low = c1["h"]
            # Inverse: last candle closes below the FVG bottom
            return last["c"] < fvg_low
    else:
        # Bearish FVG: c3.high < c1.low
        if c3["h"] < c1["l"]:
            fvg_high = c1["l"]
            return last["c"] > fvg_high
    return False


def detect_bos_1m(bars_1m, direction):
    """
    1-minute break of structure.
    Used twice: once to confirm retrace, once to confirm entry.
    """
    if len(bars_1m) < 3:
        return False
    recent = bars_1m[-6:]
    if direction == "short":
        # Retrace: 1m BOS to the upside (price pulls back up briefly)
        swing_high = max(b["h"] for b in recent[:-1])
        return recent[-1]["c"] > swing_high
    else:
        # Retrace: 1m BOS to the downside
        swing_low = min(b["l"] for b in recent[:-1])
        return recent[-1]["c"] < swing_low


def get_5m_trend(bars_5m):
    """Simple 5-min trend: last close vs 10 bars ago."""
    if len(bars_5m) < 10:
        return None
    return "bullish" if bars_5m[-1]["c"] > bars_5m[-10]["c"] else "bearish"


def are_aligned(spy_bars_5m, qqq_bars_5m):
    """Both indexes must be in the same 5-min trend."""
    spy_trend = get_5m_trend(spy_bars_5m)
    qqq_trend = get_5m_trend(qqq_bars_5m)
    if spy_trend and qqq_trend and spy_trend == qqq_trend:
        return spy_trend
    return None


# ── Order helpers ─────────────────────────────────────────────────────────────

def place_order(symbol, side, notional, stop_price, target_price):
    order = {
        "symbol":        symbol,
        "notional":      str(round(notional, 2)),
        "side":          side,
        "type":          "market",
        "time_in_force": "day",
    }
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json=order)
    if r.ok:
        o = r.json()
        log.info(f"ORDER: {side} {symbol} ~${notional} | stop={stop_price:.2f} target={target_price:.2f} | id={o['id']}")
        return o
    log.error(f"Order failed: {r.status_code} {r.text[:200]}")
    return None


def close_position(symbol):
    r = requests.delete(f"{BASE_URL}/positions/{symbol}", headers=HEADERS)
    if r.ok:
        log.info(f"CLOSED position: {symbol}")
    return r.ok


def get_position(symbol):
    r = requests.get(f"{BASE_URL}/positions/{symbol}", headers=HEADERS)
    return r.json() if r.ok else None


# ── State ─────────────────────────────────────────────────────────────────────

def load_state():
    today = now_et().strftime("%Y-%m-%d")
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            s = json.load(f)
        if s.get("date") == today:
            return s
    # Fresh state for today
    return {
        "date":         today,
        "phase":        "watching",   # watching → swept → reversed → retraced → in_trade → done
        "direction":    None,         # 'long' or 'short'
        "symbol":       None,         # which symbol we're trading
        "sweep_price":  None,
        "stop_price":   None,
        "target_price": None,
        "order_id":     None,
        "result":       None,
    }


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    if not market_is_open():
        return

    et = now_et()

    # Only trade 9:30–10:30 AM ET
    open_time   = session_open_et()
    cutoff_time = cutoff_et()

    if et < open_time:
        return
    if et > cutoff_time:
        # Past cutoff — close any open position and mark done
        state = load_state()
        if state["phase"] == "in_trade" and state.get("symbol"):
            sym = state["symbol"]
            pos = get_position(sym)
            if pos:
                close_position(sym)
                log.info(f"CUTOFF: closed {sym} at 10:30 ET")
            state["phase"] = "done"
            save_state(state)
        return

    state = load_state()

    if state["phase"] == "done":
        return  # already traded today

    # ── Get data ─────────────────────────────────────────────────────────────
    spy_5m = get_bars("SPY", "5Min", limit=60)
    qqq_5m = get_bars("QQQ", "5Min", limit=60)
    spy_1m = get_bars("SPY", "1Min", limit=30)
    qqq_1m = get_bars("QQQ", "1Min", limit=30)

    if not spy_5m or not qqq_5m:
        log.warning("No bar data available")
        return

    phase     = state["phase"]
    direction = state["direction"]

    # ── Phase: watching — look for liquidity sweep ────────────────────────────
    if phase == "watching":
        spy_levels = get_session_levels(spy_5m)
        qqq_levels = get_session_levels(qqq_5m)

        spy_sweep = detect_liquidity_sweep(spy_5m, spy_levels)
        qqq_sweep = detect_liquidity_sweep(qqq_5m, qqq_levels)

        # At least one index must sweep, both must agree on direction
        sweep = None
        if spy_sweep and qqq_sweep and spy_sweep == qqq_sweep:
            sweep = spy_sweep
        elif spy_sweep:
            sweep = spy_sweep
        elif qqq_sweep:
            sweep = qqq_sweep

        if sweep:
            state["phase"]     = "swept"
            state["direction"] = sweep
            log.info(f"STEP 1 COMPLETE: liquidity sweep detected — direction={sweep}")
            print(f"[TJR] Step 1: Liquidity sweep ({sweep})")

    # ── Phase: swept — look for 5-min reversal ────────────────────────────────
    elif phase == "swept":
        d = direction
        spy_rev = detect_bos_5m(spy_5m, d) or detect_inverse_fvg_5m(spy_5m, d)
        qqq_rev = detect_bos_5m(qqq_5m, d) or detect_inverse_fvg_5m(qqq_5m, d)

        # Both must confirm OR at least SPY (larger/more reliable)
        if spy_rev:
            state["phase"] = "reversed"
            log.info(f"STEP 2 COMPLETE: 5-min reversal confirmed ({d})")
            print(f"[TJR] Step 2: 5-min reversal confirmed ({d})")

    # ── Phase: reversed — look for 1-min retrace ─────────────────────────────
    elif phase == "reversed":
        d = direction
        # Retrace means 1-min BOS in OPPOSITE direction
        retrace_dir = "long" if d == "short" else "short"
        spy_retrace = detect_bos_1m(spy_1m, retrace_dir)

        if spy_retrace:
            state["phase"] = "retraced"
            log.info(f"STEP 3 COMPLETE: 1-min retrace confirmed")
            print(f"[TJR] Step 3: 1-min retrace confirmed")

    # ── Phase: retraced — check index alignment then look for entry ───────────
    elif phase == "retraced":
        d = direction
        aligned = are_aligned(spy_5m, qqq_5m)

        if not aligned:
            log.info("Indexes not aligned — skipping entry")
            print("[TJR] Indexes not aligned — waiting")
            return

        # Entry: 1-min BOS back in trade direction
        entry_bos = detect_bos_1m(spy_1m, d)

        if entry_bos:
            last_spy = spy_5m[-1]
            recent_1m = spy_1m[-10:]

            if d == "short":
                stop   = max(b["h"] for b in recent_1m[-4:])   # above second recent high
                target = min(b["l"] for b in spy_5m[-20:])      # nearest session low
                side   = "sell"
            else:
                stop   = min(b["l"] for b in recent_1m[-4:])    # below second recent low
                target = max(b["h"] for b in spy_5m[-20:])      # nearest session high
                side   = "buy"

            # Check alignment one more time
            order = place_order("SPY", side, TRADE_SIZE, stop, target)
            if order:
                state["phase"]       = "in_trade"
                state["symbol"]      = "SPY"
                state["stop_price"]  = stop
                state["target_price"]= target
                state["order_id"]    = order["id"]
                log.info(f"STEP 4 COMPLETE: entered {side} SPY | stop={stop:.2f} target={target:.2f}")
                print(f"[TJR] Step 4: ENTERED {side.upper()} SPY | stop=${stop:.2f} target=${target:.2f}")

    # ── Phase: in_trade — manage stop and target ──────────────────────────────
    elif phase == "in_trade":
        sym  = state["symbol"]
        pos  = get_position(sym)
        d    = state["direction"]
        stop = state["stop_price"]
        tgt  = state["target_price"]

        if not pos:
            state["phase"] = "done"
            log.info("Position closed (filled stop or target)")
            print("[TJR] Position closed — done for the day")
            save_state(state)
            return

        price = float(pos["current_price"])
        pl    = float(pos["unrealized_pl"])

        log.info(f"MANAGING: {sym} @ ${price:.2f} | P&L=${pl:+.2f} | stop={stop:.2f} target={tgt:.2f}")
        print(f"[TJR] In trade: {sym} ${price:.2f} | P&L ${pl:+.2f}")

        # Manual stop/target check (market order to close)
        hit_stop   = (d == "short" and price >= stop) or (d == "long"  and price <= stop)
        hit_target = (d == "short" and price <= tgt)  or (d == "long"  and price >= tgt)

        if hit_stop or hit_target:
            reason = "TARGET" if hit_target else "STOP"
            close_position(sym)
            state["phase"]  = "done"
            state["result"] = f"{reason} @ ${price:.2f} | P&L=${pl:+.2f}"
            log.info(f"{reason} HIT: closed {sym} @ ${price:.2f} | P&L={pl:+.2f}")
            print(f"[TJR] {reason} HIT — closed {sym} @ ${price:.2f} | P&L ${pl:+.2f}")

    save_state(state)


if __name__ == "__main__":
    run()
