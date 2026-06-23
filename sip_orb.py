"""
Stocks-in-Play Opening Range Breakout (SIP-ORB)
================================================
Multi-stock 5-min ORB on the top relative-volume names each morning.
Based on Zarattini, Barbon & Aziz (SSRN 4729284): Sharpe 2.81, >1,600% total
return 2016-2023, vs S&P 500 ~198% with near-zero market beta.

Key insight: the edge comes from isolating "Stocks in Play" — names with
unusually high volume in the first 5 minutes, a proxy for news-driven activity
that creates the trend days ORB is designed to capture.

Mechanics:
  Opening range  : first 5-min bar of the session (9:30–9:35 ET)
  RVol           : today's first-5-min volume / 14-day avg first-5-min volume
                   (proxy: today's OR volume / (20d avg daily vol × 5/390))
  Selection      : top MAX_POSITIONS stocks by RVol with RVol > MIN_RVOL
  Filters        : price > $5, ATR(14) > $0.50, avg daily vol > 500k
  Direction      : close > open → long  (stop-limit entry above OR high)
                   close < open → short (stop-limit entry below OR low)
  Entry          : resting stop-limit order at OR boundary (only enters if
                   price actually breaks through — no fill on quiet days)
  Stop           : 0.10 × ATR(14) from fill price (exchange-side stop_limit)
  Exit           : stop hit  OR  EOD close at 3:55 PM ET
  No profit target — trend days run to the close (paper's documented edge)

Self-looping job (same reliability pattern as the TSLA monitor).
TSLA excluded from universe — held permanently by the trailing-stop strategy.
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
STATE_FILE = f"{BASE_DIR}/sip_orb_state.json"

# ── Universe ──────────────────────────────────────────────────────────────────
# ~80 consistently-liquid US equities + key ETFs. TSLA excluded (held by the
# TSLA trailing-stop bot). Anything already held by another strategy is skipped
# at runtime via the existing-position check.
UNIVERSE = [
    # Mega-cap tech
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "AMD", "AVGO",
    "QCOM", "TXN", "MU", "INTC", "AMAT", "LRCX", "CRM", "ORCL", "NOW",
    "ADBE", "INTU", "PANW", "CRWD", "PLTR", "MRVL",
    # Financials
    "JPM", "BAC", "WFC", "GS", "MS", "C", "BLK", "SCHW", "AXP", "V", "MA",
    # Healthcare
    "UNH", "LLY", "ABBV", "MRK", "PFE", "ABT", "TMO", "AMGN", "GILD",
    "VRTX", "REGN",
    # Consumer
    "HD", "MCD", "NKE", "SBUX", "TGT", "COST", "WMT", "LOW",
    # Energy
    "XOM", "CVX", "COP", "EOG",
    # Industrial / Defence
    "CAT", "DE", "HON", "GE", "BA", "UNP", "RTX", "LMT", "NOC",
    # Communication / media
    "NFLX", "DIS", "CMCSA",
    # Materials / Real estate
    "NEM", "FCX", "AMT", "EQIX",
    # Liquid ETFs (high volume, clean OR patterns)
    "SPY", "QQQ", "IWM", "GLD", "XLF", "XLE", "XLK", "XLV", "XLP", "XLI",
]

MAX_POSITIONS    = 10       # max concurrent positions (conservative vs paper's 20)
NOTIONAL_PER_POS = 1500     # $ per position
MIN_RVOL         = 1.5      # minimum relative volume to qualify
MIN_PRICE        = 5.0      # $ price floor
MIN_ATR          = 0.50     # $ 14-day ATR floor
MIN_AVG_VOL      = 500_000  # avg daily volume floor
ATR_STOP_MULT    = 0.10     # stop at 10% of ATR(14) from fill
ENTRY_SLIP       = 0.003    # 0.3% above OR high (below OR low) on entry limit
STOP_SLIP        = 0.010    # 1% give on protective stop limit

POLL_INTERVAL_SEC = 60
MAX_RUNTIME_MIN   = 330
ENTRY_CUTOFF      = (10, 0)   # don't enter after 10 AM ET
EOD_CLOSE         = (15, 55)  # flatten all at 3:55 PM ET

logging.basicConfig(
    filename=f"{BASE_DIR}/sip_orb.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger()


# ── Helpers ───────────────────────────────────────────────────────────────────

def now_et():
    return datetime.now(ET)


def at_or_after(hm):
    n = now_et()
    return (n.hour, n.minute) >= hm


def market_is_open():
    r = requests.get(f"{BASE_URL}/clock", headers=HEADERS)
    return r.json()["is_open"]


def get_all_positions():
    r = requests.get(f"{BASE_URL}/positions", headers=HEADERS)
    return {p["symbol"]: p for p in (r.json() if r.ok else [])}


def get_open_orders():
    r = requests.get(f"{BASE_URL}/orders", headers=HEADERS,
                     params={"status": "open", "limit": 200})
    return {o["id"]: o for o in (r.json() if r.ok else [])}


def get_order(order_id):
    r = requests.get(f"{BASE_URL}/orders/{order_id}", headers=HEADERS)
    return r.json() if r.ok else None


def cancel_order(order_id):
    try:
        requests.delete(f"{BASE_URL}/orders/{order_id}", headers=HEADERS)
    except Exception:
        pass


def close_position(symbol):
    r = requests.delete(f"{BASE_URL}/positions/{symbol}", headers=HEADERS)
    return r.ok


# ── Multi-stock data ──────────────────────────────────────────────────────────

def _fetch_bars_batch(symbols, timeframe, start, limit=50):
    """Fetch bars for a batch of symbols in one API call."""
    r = requests.get(
        "https://data.alpaca.markets/v2/stocks/bars",
        headers=DATA_HEADERS,
        params={
            "symbols":   ",".join(symbols),
            "timeframe": timeframe,
            "start":     start,
            "limit":     limit,
            "sort":      "asc",
            "adjustment":"raw",
        },
        timeout=30,
    )
    return r.json().get("bars", {}) if r.ok else {}


def fetch_daily_bars(symbols, days=40):
    """Return {symbol: [bars]} for the last `days` calendar days."""
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = {}
    # Batch into chunks of 50 to stay within URL-length limits
    for i in range(0, len(symbols), 50):
        batch = symbols[i:i+50]
        result.update(_fetch_bars_batch(batch, "1Day", start, limit=30))
    return result


def fetch_5m_today(symbols):
    """Return {symbol: [bars]} for today's session (up to 11 AM ET to keep it lean)."""
    today = datetime.now(timezone.utc).date()
    start = f"{today}T13:00:00Z"   # 9:00 AM ET (UTC-4 in EDT)
    result = {}
    for i in range(0, len(symbols), 50):
        batch = symbols[i:i+50]
        result.update(_fetch_bars_batch(batch, "5Min", start, limit=20))
    return result


# ── Indicators ────────────────────────────────────────────────────────────────

def atr14(daily_bars):
    """True-range based ATR(14) from daily bars."""
    if len(daily_bars) < 15:
        return None
    bars = daily_bars[-15:]
    trs = []
    for i in range(1, len(bars)):
        hi, lo, prev_c = bars[i]["h"], bars[i]["l"], bars[i-1]["c"]
        trs.append(max(hi - lo, abs(hi - prev_c), abs(lo - prev_c)))
    return sum(trs[-14:]) / 14


def avg_daily_vol(daily_bars, lookback=20):
    if len(daily_bars) < 5:
        return 0
    vols = [b["v"] for b in daily_bars[-lookback:]]
    return sum(vols) / len(vols)


def find_or_bar(bars_5m):
    """Return the 9:30–9:35 ET bar from today's 5-min bars, or None."""
    today = now_et().date()
    for b in bars_5m:
        t_et = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(ET)
        if t_et.date() == today and t_et.hour == 9 and t_et.minute == 30:
            return b
    return None


# ── Order placement ───────────────────────────────────────────────────────────

def place_entry_stop(symbol, side, qty, trigger_price):
    """Resting stop-limit entry: only fills if price breaks the OR boundary."""
    if side == "buy":
        limit_px = round(trigger_price * (1 + ENTRY_SLIP), 2)
    else:
        limit_px = round(trigger_price * (1 - ENTRY_SLIP), 2)
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json={
        "symbol":        symbol,
        "qty":           str(qty),
        "side":          side,
        "type":          "stop_limit",
        "stop_price":    str(round(trigger_price, 2)),
        "limit_price":   str(limit_px),
        "time_in_force": "day",
    })
    if r.ok:
        return r.json()
    log.error(f"Entry order failed {symbol}: {r.text[:200]}")
    return None


def place_protective_stop(symbol, exit_side, qty, stop_px):
    """Exchange-side stop_limit to cap loss if the trade goes wrong."""
    if exit_side == "sell":
        limit_px = round(stop_px * (1 - STOP_SLIP), 2)
    else:
        limit_px = round(stop_px * (1 + STOP_SLIP), 2)
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json={
        "symbol":        symbol,
        "qty":           str(qty),
        "side":          exit_side,
        "type":          "stop_limit",
        "stop_price":    str(round(stop_px, 2)),
        "limit_price":   str(limit_px),
        "time_in_force": "day",
    })
    if r.ok:
        return r.json()
    log.error(f"Stop order failed {symbol}: {r.text[:200]}")
    return None


# ── State ─────────────────────────────────────────────────────────────────────

def load_state():
    today = now_et().strftime("%Y-%m-%d")
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            s = json.load(f)
        if s.get("date") == today:
            return s
    return {"date": today, "phase": "waiting", "positions": {}}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Core logic ────────────────────────────────────────────────────────────────

def score_universe():
    """Fetch data and score each symbol; return list of (symbol, meta) sorted
    by RVol descending, filtered by all qualifying criteria."""
    log.info("Fetching historical daily bars for universe…")
    daily = fetch_daily_bars(UNIVERSE)

    log.info("Fetching today's 5-min bars for universe…")
    intraday = fetch_5m_today(UNIVERSE)

    existing = set(get_all_positions().keys())
    candidates = []

    for sym in UNIVERSE:
        if sym in existing:
            continue  # already held by another strategy

        d_bars = daily.get(sym, [])
        i_bars = intraday.get(sym, [])
        if not d_bars or not i_bars:
            continue

        or_bar = find_or_bar(i_bars)
        if not or_bar:
            continue

        price = or_bar["c"]
        if price < MIN_PRICE:
            continue

        atr = atr14(d_bars)
        if atr is None or atr < MIN_ATR:
            continue

        adv = avg_daily_vol(d_bars)
        if adv < MIN_AVG_VOL:
            continue

        # RVol proxy: today's first-5-min volume vs expected (daily avg × 5/390)
        expected_5m_vol = adv * (5 / 390)
        rvol = or_bar["v"] / expected_5m_vol if expected_5m_vol > 0 else 0
        if rvol < MIN_RVOL:
            continue

        # Skip dead-flat opening range
        or_range = or_bar["h"] - or_bar["l"]
        if or_range < 0.01 or or_bar["c"] == or_bar["o"]:
            continue

        direction = "long" if or_bar["c"] > or_bar["o"] else "short"
        entry_trigger = or_bar["h"] if direction == "long" else or_bar["l"]
        stop_distance = round(ATR_STOP_MULT * atr, 4)

        qty = int(NOTIONAL_PER_POS // price)
        if qty < 1:
            continue

        candidates.append({
            "symbol":    sym,
            "direction": direction,
            "entry_trigger": entry_trigger,
            "stop_distance": stop_distance,
            "atr":       round(atr, 4),
            "rvol":      round(rvol, 2),
            "price":     price,
            "qty":       qty,
            "or_high":   or_bar["h"],
            "or_low":    or_bar["l"],
        })

    candidates.sort(key=lambda x: x["rvol"], reverse=True)
    return candidates[:MAX_POSITIONS]


def enter_all(candidates, state):
    """Place resting entry stop-limit orders for all selected candidates."""
    for c in candidates:
        sym  = c["symbol"]
        side = "buy" if c["direction"] == "long" else "sell"
        order = place_entry_stop(sym, side, c["qty"], c["entry_trigger"])
        if order:
            state["positions"][sym] = {
                "direction":      c["direction"],
                "entry_order_id": order["id"],
                "entry_price":    None,   # filled later
                "qty":            c["qty"],
                "stop_order_id":  None,
                "stop_price":     None,
                "stop_distance":  c["stop_distance"],
                "atr":            c["atr"],
                "rvol":           c["rvol"],
                "phase":          "pending",  # pending | in_trade | closed
            }
            log.info(f"ENTRY ORDER {c['direction']} {sym} x{c['qty']} "
                     f"trigger={c['entry_trigger']:.2f} RVol={c['rvol']:.1f} ATR={c['atr']:.2f}")
            print(f"[SIP-ORB] {c['direction'].upper()} {sym} x{c['qty']} "
                  f"trigger={c['entry_trigger']:.2f} RVol={c['rvol']:.1f}")


def manage_positions(state):
    """Check fills on pending orders, attach stops; check stops on in-trade."""
    positions_api = get_all_positions()

    for sym, info in list(state["positions"].items()):
        if info["phase"] == "closed":
            continue

        # ── Pending: check if entry order filled ─────────────────────────────
        if info["phase"] == "pending":
            eid = info["entry_order_id"]
            order = get_order(eid)
            if not order:
                continue
            status = order.get("status", "")
            if status == "filled":
                fill = float(order.get("filled_avg_price") or 0)
                if not fill:
                    log.warning(f"{sym}: filled order has no filled_avg_price — skipping")
                    continue
                info["entry_price"] = fill
                info["phase"] = "in_trade"

                # Place protective stop now that we know the fill price
                d = info["direction"]
                raw_stop = (fill - info["stop_distance"] if d == "long"
                            else fill + info["stop_distance"])
                exit_side = "sell" if d == "long" else "buy"
                stop_order = place_protective_stop(sym, exit_side, info["qty"],
                                                   round(raw_stop, 2))
                info["stop_price"]    = round(raw_stop, 2)
                info["stop_order_id"] = stop_order["id"] if stop_order else None
                note = f"stop ${raw_stop:.2f}" if stop_order else "STOP FAILED"
                log.info(f"FILLED {sym} @ ${fill:.2f} | {note}")
                print(f"[SIP-ORB] FILLED {sym} @ ${fill:.2f} | {note}")

            elif status in ("cancelled", "expired", "rejected"):
                info["phase"] = "closed"
                log.info(f"Entry order {status}: {sym}")

        # ── In trade: check if stop fired or position gone ───────────────────
        elif info["phase"] == "in_trade":
            if sym not in positions_api:
                # Position closed — check stop order for fill price
                sid = info.get("stop_order_id")
                stop_order = get_order(sid) if sid else None
                entry = info.get("entry_price") or 0
                qty   = info["qty"]
                if stop_order and stop_order.get("status") == "filled":
                    fill = float(stop_order.get("filled_avg_price") or 0) or entry
                    pnl  = ((fill - entry) * qty if info["direction"] == "long"
                            else (entry - fill) * qty)
                    record_trade("sip_orb", sym, pnl, "stop filled")
                    log.info(f"STOP FILLED {sym} @ ${fill:.2f} | P&L ${pnl:+.2f}")
                    print(f"[SIP-ORB] STOP FILLED {sym} @ ${fill:.2f} | P&L ${pnl:+.2f}")
                else:
                    cancel_order(sid)
                    log.info(f"Position {sym} gone (not via our stop) — cleaned up")
                info["phase"] = "closed"
            else:
                pos = positions_api[sym]
                pl  = float(pos.get("unrealized_pl", 0) or 0)
                log.info(f"HOLDING {sym} @ ${float(pos['current_price']):.2f} "
                         f"P&L ${pl:+.2f}")


def eod_close(state):
    """Cancel all pending entry orders and close all open positions."""
    for sym, info in state["positions"].items():
        if info["phase"] == "closed":
            continue
        if info["phase"] == "pending":
            cancel_order(info["entry_order_id"])
            info["phase"] = "closed"
            log.info(f"EOD: cancelled pending entry for {sym}")
        elif info["phase"] == "in_trade":
            cancel_order(info.get("stop_order_id"))
            positions_api = get_all_positions()
            if sym in positions_api:
                pos = positions_api[sym]
                pnl = float(pos.get("unrealized_pl", 0) or 0)
                if close_position(sym):
                    record_trade("sip_orb", sym, pnl, "EOD close")
                    log.info(f"EOD CLOSE {sym} | P&L ${pnl:+.2f}")
                    print(f"[SIP-ORB] EOD CLOSE {sym} | P&L ${pnl:+.2f}")
            info["phase"] = "closed"


# ── Run ───────────────────────────────────────────────────────────────────────

def run():
    start_mono = time.monotonic()
    log.info("SIP-ORB loop started")
    print("SIP-ORB loop started")

    entered = False

    while (time.monotonic() - start_mono) / 60 < MAX_RUNTIME_MIN:
        try:
            if not market_is_open():
                log.info("Market closed — sleeping.")
                time.sleep(POLL_INTERVAL_SEC)
                continue

            state = load_state()

            if at_or_after(EOD_CLOSE):
                eod_close(state)
                save_state(state)
                log.info("EOD close complete — done for the day.")
                break

            # Score and enter once, after the OR bar has printed
            if not entered and at_or_after(ENTRY_CUTOFF):
                log.info("Past entry cutoff without entering — standing down.")
                state["phase"] = "done"
                save_state(state)
                break

            if not entered and at_or_after((9, 35)):
                candidates = score_universe()
                if candidates:
                    enter_all(candidates, state)
                    state["phase"] = "managing"
                    log.info(f"Entered {len(candidates)} positions")
                    print(f"[SIP-ORB] Entered {len(candidates)} positions "
                          f"(top RVol: {candidates[0]['symbol']} {candidates[0]['rvol']:.1f}x)")
                else:
                    log.info("No qualifying stocks today — standing down.")
                    print("[SIP-ORB] No qualifying stocks today")
                    state["phase"] = "done"
                    save_state(state)
                    break
                entered = True
                save_state(state)

            elif entered:
                manage_positions(state)
                save_state(state)
            else:
                log.info(f"Waiting for 9:35 ET (now {now_et():%H:%M})")

        except Exception as e:
            log.exception(f"tick error (continuing): {e}")

        time.sleep(POLL_INTERVAL_SEC)

    log.info("SIP-ORB loop ended")


if __name__ == "__main__":
    run()
