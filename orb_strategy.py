"""
Opening Range Breakout (ORB) Bot
================================
Single-asset 5-minute ORB on QQQ, adapted from Zarattini & Aziz,
"Can Day Trading Really Be Profitable?" (SSRN 4416622). The documented edge is a
LOW win rate (~17–43%) with asymmetric payoff: small stop, no profit target, let
the trend day run to the close.

Mechanics (faithful to the paper's structure, adapted for an unleveraged paper
account — we size by a fixed notional and use a resting exchange stop instead of
the paper's 0.05*ATR tiny stop + 4x leverage, which we can't replicate safely):

  1. Opening range = the first 5-min bar of the regular session (9:30–9:35 ET).
  2. Direction: that bar closes ABOVE its open  → go long  on the break.
               that bar closes BELOW its open  → go short on the break.
  3. Entry: market order at the first poll after 9:35 ET, in the signaled
     direction. One trade per session.
  4. Stop: resting stop_limit on Alpaca at the OPPOSITE edge of the opening
     range (long → OR low, short → OR high). Survives even if this job dies.
  5. Exit: no profit target. Close at 3:55 PM ET (EOD) if the stop never fired.

Self-looping job (same reliability pattern as the TSLA monitor): a flaky
every-2-min cron gets throttled/dropped by GitHub, so one job polls the whole
session with a 2 PM handoff relaunch. The resting stop means nothing critical is
lost on a handoff.
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
STATE_FILE = f"{BASE_DIR}/orb_state.json"

SYMBOL   = "QQQ"      # canonical instrument from the paper
NOTIONAL = 2000       # $ per trade (whole shares, so a resting stop can attach)

# A dead-flat open has no edge — skip if the opening range is a smaller fraction
# of price than this (range / price).
MIN_RANGE_FRAC = 0.003    # 0.3% — filters dead-flat days; backtest confirmed 0.08% too loose
MIN_RVOL       = 1.5      # only trade when first-5min volume is 1.5× normal (catalyst days)

# Self-looping job timing (mirrors market_monitor.py).
POLL_INTERVAL_SEC = 30
MAX_RUNTIME_MIN   = 330
ENTRY_AFTER       = (9, 35)    # enter on the first poll at/after 9:35 ET
EOD_CLOSE         = (15, 55)   # flatten at 3:55 PM ET

logging.basicConfig(
    filename=f"{BASE_DIR}/orb.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger()


# ── Data helpers ──────────────────────────────────────────────────────────────

def market_is_open():
    r = requests.get(f"{BASE_URL}/clock", headers=HEADERS)
    return r.json()["is_open"]


def now_et():
    return datetime.now(ET)


def at_or_after(hm):
    n = now_et()
    return (n.hour, n.minute) >= hm


def get_5m_bars(symbol, limit=120):
    start = (datetime.now(timezone.utc) - timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = requests.get(
        f"https://data.alpaca.markets/v2/stocks/{symbol}/bars",
        headers=DATA_HEADERS,
        params={"timeframe": "5Min", "start": start, "limit": limit, "sort": "asc"},
    )
    return r.json().get("bars") or [] if r.ok else []


def opening_range_bar(bars_5m):
    """Return today's 9:30–9:35 ET bar, or None if it hasn't printed yet."""
    today = now_et().date()
    for b in bars_5m:
        t_et = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(ET)
        if t_et.date() == today and t_et.hour == 9 and t_et.minute == 30:
            return b
    return None


def get_price(symbol):
    r = requests.get(
        f"https://data.alpaca.markets/v2/stocks/{symbol}/trades/latest",
        headers=DATA_HEADERS,
    )
    return float(r.json()["trade"]["p"]) if r.ok else None


# ── Order helpers ─────────────────────────────────────────────────────────────

def submit_market(symbol, side, qty):
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json={
        "symbol":        symbol,
        "qty":           str(qty),
        "side":          side,
        "type":          "market",
        "time_in_force": "day",
    })
    if r.ok:
        o = r.json()
        log.info(f"{side.upper()} {symbol} x{qty} | order {o['id']}")
        return o
    log.error(f"{side} failed {symbol}: {r.text[:200]}")
    return None


def place_protective_stop(symbol, side, qty, stop_price):
    """Resting stop_limit that exits the position if the OR boundary breaks.
    side is the EXIT side: 'sell' protects a long, 'buy' protects a short.
    Limit sits just past the stop (1% give) so a fast move still fills."""
    limit = stop_price * (0.99 if side == "sell" else 1.01)
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json={
        "symbol":        symbol,
        "qty":           str(qty),
        "side":          side,
        "type":          "stop_limit",
        "stop_price":    str(round(stop_price, 2)),
        "limit_price":   str(round(limit, 2)),
        "time_in_force": "day",
    })
    if r.ok:
        return r.json()
    log.error(f"Stop placement failed {symbol}: {r.text[:200]}")
    return None


def get_order(order_id):
    try:
        r = requests.get(f"{BASE_URL}/orders/{order_id}", headers=HEADERS)
        return r.json() if r.ok else None
    except Exception:
        return None


def cancel_order(order_id):
    if not order_id:
        return
    try:
        requests.delete(f"{BASE_URL}/orders/{order_id}", headers=HEADERS)
    except Exception:
        pass


def get_position(symbol):
    r = requests.get(f"{BASE_URL}/positions/{symbol}", headers=HEADERS)
    return r.json() if r.ok else None


def close_position(symbol):
    r = requests.delete(f"{BASE_URL}/positions/{symbol}", headers=HEADERS)
    if r.ok:
        log.info(f"CLOSED position: {symbol}")
    return r.ok


# ── State ─────────────────────────────────────────────────────────────────────

def load_state():
    today = now_et().strftime("%Y-%m-%d")
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            s = json.load(f)
        if s.get("date") == today:
            return s
    return {
        "date":          today,
        "phase":         "waiting",   # waiting → in_trade → done
        "direction":     None,
        "entry_price":   None,
        "qty":           0,
        "stop_order_id": None,
        "stop_price":    None,
        "result":        None,
    }


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Main ──────────────────────────────────────────────────────────────────────

def get_daily_bars(symbol, days=25):
    """Recent daily bars for RVol calculation."""
    start = (datetime.now(timezone.utc) - timedelta(days=days+5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = requests.get(
        f"https://data.alpaca.markets/v2/stocks/{symbol}/bars",
        headers=DATA_HEADERS,
        params={"timeframe": "1Day", "start": start, "limit": 30, "sort": "asc"},
    )
    return r.json().get("bars") or [] if r.ok else []


def try_enter(state):
    """Read the opening range; only enter on high-RVol catalyst days via breakout."""
    # Hands-off: if QQQ is already held (e.g., by the IBS swing bot), stand down.
    # ORB closes its position with close_position(), which would otherwise wipe
    # out another strategy's shares in the merged broker position.
    if get_position(SYMBOL):
        log.info(f"{SYMBOL} already held by another strategy — ORB standing down today.")
        state["phase"] = "done"
        return

    bars = get_5m_bars(SYMBOL)
    orb  = opening_range_bar(bars)
    if not orb:
        log.info("Opening-range bar not available yet — waiting")
        return

    o, c, hi, lo = orb["o"], orb["c"], orb["h"], orb["l"]
    rng = hi - lo
    if c == o or rng <= 0 or (rng / c) < MIN_RANGE_FRAC:
        log.info(f"No tradable opening range (o={o} c={c} range={rng:.2f}) — standing down")
        state["phase"] = "done"
        return

    # RVol filter: only trade on days with genuine order flow (catalysts).
    # Backtest showed unconditional entry = 23% win rate regardless of other filters.
    daily = get_daily_bars(SYMBOL, days=25)
    if len(daily) >= 10:
        avg_vol = sum(b["v"] for b in daily[-20:]) / min(len(daily), 20)
        exp_5m  = avg_vol * (5 / 390)
        rvol    = orb["v"] / exp_5m if exp_5m > 0 else 0
        if rvol < MIN_RVOL:
            log.info(f"Low RVol {rvol:.1f}x (need {MIN_RVOL}x) — skipping today")
            state["phase"] = "done"
            return
        log.info(f"RVol {rvol:.1f}x — qualifying")

    direction  = "long" if c > o else "short"
    entry_side = "buy"  if direction == "long" else "sell"
    exit_side  = "sell" if direction == "long" else "buy"
    stop_price = lo if direction == "long" else hi
    # Breakout trigger: enter only if price actually clears the OR boundary.
    # Stop-limit entry replaces the old unconditional market order.
    trigger    = hi if direction == "long" else lo

    price = get_price(SYMBOL) or c
    qty   = int(NOTIONAL // price)
    if qty < 1:
        log.warning(f"Notional ${NOTIONAL} too small for {SYMBOL} @ ${price:.2f}")
        state["phase"] = "done"
        return

    # Place a resting stop-limit entry — only fills if price breaks the OR boundary.
    # The live bot now behaves identically to SIP-ORB: no fill on quiet days.
    limit_entry = round(trigger * (1.003 if direction == "long" else 0.997), 2)
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json={
        "symbol":        SYMBOL,
        "qty":           str(qty),
        "side":          entry_side,
        "type":          "stop_limit",
        "stop_price":    str(round(trigger, 2)),
        "limit_price":   str(limit_entry),
        "time_in_force": "day",
    })
    if not r.ok:
        log.error(f"Entry order failed: {r.text[:200]}")
        return

    entry_order = r.json()
    stop = place_protective_stop(SYMBOL, exit_side, qty, stop_price)
    state.update({
        "phase":           "pending_entry",   # new phase: waiting for breakout fill
        "direction":       direction,
        "entry_order_id":  entry_order["id"],
        "entry_price":     None,
        "qty":             qty,
        "stop_order_id":   stop["id"] if stop else None,
        "stop_price":      round(stop_price, 2),
        "or_high":         round(hi, 2),
        "or_low":          round(lo, 2),
    })
    note = f"stop ${stop_price:.2f}" if stop else "STOP FAILED"
    log.info(f"BREAKOUT ORDER {direction} {SYMBOL} x{qty} trigger=${trigger:.2f} | "
             f"OR[{lo:.2f}-{hi:.2f}] | {note}")
    print(f"[ORB] BREAKOUT ORDER {direction} {SYMBOL} x{qty} trigger=${trigger:.2f} | {note}")


def manage(state):
    """Watch the open trade: detect a stop fill, or flatten at EOD."""
    sym = SYMBOL
    pos = get_position(sym)

    # Position gone — the resting stop fired intraday (or it was closed elsewhere).
    if not pos:
        sid   = state.get("stop_order_id")
        order = get_order(sid) if sid else None
        entry = state.get("entry_price") or 0
        qty   = state.get("qty", 0)
        if order and order.get("status") == "filled":
            fill = float(order.get("filled_avg_price") or 0) or entry
            pnl  = (fill - entry) * qty if state["direction"] == "long" else (entry - fill) * qty
            record_trade("orb", sym, pnl, "stop filled intraday")
            log.info(f"STOP FILLED {sym} @ ${fill:.2f} | P&L ${pnl:+.2f}")
            print(f"[ORB] STOP FILLED {sym} @ ${fill:.2f} | P&L ${pnl:+.2f}")
        else:
            cancel_order(sid)
            log.info(f"{sym} position gone (not via our stop) — cleaned up")
        state["phase"]  = "done"
        state["result"] = "stopped"
        return

    price = float(pos["current_price"])
    pl    = float(pos["unrealized_pl"])
    log.info(f"MANAGING {sym} @ ${price:.2f} | P&L ${pl:+.2f} | stop {state.get('stop_price')}")

    # EOD: no profit target — flatten at the close so the trend day's gain is
    # captured (faithful to the paper's hold-to-close rule).
    if at_or_after(EOD_CLOSE):
        cancel_order(state.get("stop_order_id"))
        if close_position(sym):
            record_trade("orb", sym, pl, "EOD close")
            log.info(f"EOD CLOSE {sym} @ ${price:.2f} | P&L ${pl:+.2f}")
            print(f"[ORB] EOD CLOSE {sym} @ ${price:.2f} | P&L ${pl:+.2f}")
            state["phase"]  = "done"
            state["result"] = "eod"


def run_once():
    if not market_is_open():
        log.info("Market closed — no action.")
        return "closed"

    state = load_state()
    if state["phase"] == "done":
        return "done"

    if state["phase"] == "waiting":
        if at_or_after(EOD_CLOSE):
            log.info("Reached EOD without ever entering — done.")
            state["phase"] = "done"
        elif at_or_after(ENTRY_AFTER):
            try_enter(state)
        else:
            log.info(f"Before entry time ({now_et():%H:%M} ET) — waiting for 9:35.")
    elif state["phase"] == "pending_entry":
        # Check if breakout entry order filled; if EOD arrives, cancel it.
        if at_or_after(EOD_CLOSE):
            cancel_order(state.get("entry_order_id"))
            cancel_order(state.get("stop_order_id"))
            log.info("EOD reached with no breakout fill — cancelled entry, done.")
            state["phase"] = "done"
        else:
            order = get_order(state.get("entry_order_id"))
            if order and order.get("status") == "filled":
                fill = float(order.get("filled_avg_price") or 0)
                state["entry_price"] = fill
                state["phase"]       = "in_trade"
                log.info(f"BREAKOUT FILLED {SYMBOL} @ ${fill:.2f} | "
                         f"stop ${state['stop_price']:.2f}")
                print(f"[ORB] BREAKOUT FILLED @ ${fill:.2f}")
            elif order and order.get("status") in ("cancelled", "expired", "rejected"):
                cancel_order(state.get("stop_order_id"))
                state["phase"] = "done"
                log.info(f"Entry order {order.get('status')} — done.")
    elif state["phase"] == "in_trade":
        manage(state)

    save_state(state)
    return "done" if state["phase"] == "done" else "active"


def run():
    start = time.monotonic()
    log.info("ORB session loop started")
    print("ORB session loop started")
    while (time.monotonic() - start) / 60 < MAX_RUNTIME_MIN:
        try:
            status = run_once()
        except Exception as e:
            log.exception(f"tick error (continuing): {e}")
            status = "active"
        if status in ("closed", "done"):
            log.info(f"ORB loop exiting — {status}")
            break
        time.sleep(POLL_INTERVAL_SEC)
    log.info("ORB session loop ended")


if __name__ == "__main__":
    run()
