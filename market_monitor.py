"""
Market-hours monitor for TSLA strategy.

Runs as a SINGLE self-looping job (started by GitHub Actions at market open
and again at 2 PM ET for handoff coverage). It polls every POLL_INTERVAL_SEC
while the market is open instead of relying on GitHub to fire dozens of
separate cron runs (which GitHub throttles and drops under load).

- Activates trailing stop once up TRAIL_TRIGGER_PCT from entry
- Raises the stop floor (ATR-based, only moves up — never down)
- Re-enters if stop was hit and price recovers
- Halts for the day if the daily loss limit is breached
- Persists state to git on every meaningful change so a restart/handoff
  never re-reads a stale stop and accidentally lowers it
- Logs all actions to monitor.log
"""

import json
import logging
import os
import subprocess
import time
import requests
from perf import record_trade
from datetime import datetime, timezone, timedelta
from dotenv import dotenv_values

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
config = dotenv_values(f"{BASE_DIR}/.env")

BASE_URL = config["ALPACA_BASE_URL"]
HEADERS = {
    "APCA-API-KEY-ID": config["ALPACA_API_KEY"],
    "APCA-API-SECRET-KEY": config["ALPACA_SECRET_KEY"],
    "Content-Type": "application/json",
}

BRANCH             = "claude/charming-edison-b7imkj"
POLL_INTERVAL_SEC  = 60     # seconds between ticks while market is open
MAX_RUNTIME_MIN    = 330    # safety cap below GitHub's 6-hour job limit
TRAIL_TRIGGER_PCT  = 0.10   # activate trailing once up 10% from entry
TRAIL_OFFSET_PCT   = 0.05   # trail stop sits 5% below running high (fallback if ATR unavailable)
REENTRY_PCT        = 0.02   # re-enter if price recovers 2% above stop after a fill
ATR_MULTIPLIER     = 2.0    # trail stop = HWM - (ATR * multiplier)
DAILY_LOSS_LIMIT   = 0.02   # halt all trading if portfolio drops 2% in a single day

logging.basicConfig(
    filename=f"{BASE_DIR}/monitor.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger()


# ── Helpers ───────────────────────────────────────────────────────────────────

def api_get(path):
    r = requests.get(f"{BASE_URL}{path}", headers=HEADERS)
    r.raise_for_status()
    return r.json()


def api_post(path, payload):
    r = requests.post(f"{BASE_URL}{path}", headers=HEADERS, json=payload)
    r.raise_for_status()
    return r.json()


def api_delete(path):
    requests.delete(f"{BASE_URL}{path}", headers=HEADERS)


def get_price(symbol):
    r = requests.get(
        f"https://data.alpaca.markets/v2/stocks/{symbol}/trades/latest",
        headers=HEADERS,
    )
    r.raise_for_status()
    return float(r.json()["trade"]["p"])


def load_state():
    with open(f"{BASE_DIR}/strategy_state.json") as f:
        return json.load(f)


def save_state(state):
    with open(f"{BASE_DIR}/strategy_state.json", "w") as f:
        json.dump(state, f, indent=2)


def persist_state(state, msg):
    """Save state to disk AND push it to git.

    Called only on meaningful changes (stop raised, trailing armed, fill) so a
    handoff/restart picks up the live stop level. Git failures are non-fatal —
    the loop must never crash because a push was rejected.
    """
    save_state(state)
    try:
        subprocess.run(["git", "-C", BASE_DIR, "add", "strategy_state.json"],
                       check=True, capture_output=True)
        # nothing staged → nothing to commit
        if subprocess.run(["git", "-C", BASE_DIR, "diff", "--cached", "--quiet"]).returncode != 0:
            subprocess.run(["git", "-C", BASE_DIR, "commit", "-m", f"{msg} [skip ci]"],
                           check=True, capture_output=True)
            subprocess.run(["git", "-C", BASE_DIR, "push", "origin", BRANCH],
                           check=True, capture_output=True)
            log.info(f"State pushed: {msg}")
    except Exception as e:
        log.warning(f"git persist failed (non-fatal): {e}")


def market_is_open():
    data = api_get("/clock")
    return data["is_open"]


def get_order(order_id):
    try:
        return api_get(f"/orders/{order_id}")
    except Exception:
        return None


def get_position(symbol):
    try:
        return api_get(f"/positions/{symbol}")
    except Exception:
        return None


def place_stop(symbol, qty, stop_price):
    # Use stop_limit to avoid wash-trade rejection when ladder buy orders are open.
    # Limit is set 1% below stop to ensure fill in fast-moving markets.
    limit_price = round(stop_price * 0.99, 2)
    return api_post("/orders", {
        "symbol":        symbol,
        "qty":           str(qty),
        "side":          "sell",
        "type":          "stop_limit",
        "stop_price":    str(round(stop_price, 2)),
        "limit_price":   str(limit_price),
        "time_in_force": "gtc",
    })


def get_daily_bars(symbol, n=30):
    """Return last n daily bars in chronological order."""
    try:
        start = (datetime.now(timezone.utc) - timedelta(days=n + 10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        r = requests.get(
            f"https://data.alpaca.markets/v2/stocks/{symbol}/bars",
            headers=HEADERS,
            params={"timeframe": "1Day", "start": start, "limit": n + 5,
                    "adjustment": "raw", "sort": "asc"},
        )
        return r.json().get("bars", []) if r.ok else []
    except Exception:
        return []


def get_atr(symbol, period=14):
    """Calculate ATR(14) from daily bars. Returns None if data unavailable."""
    bars = get_daily_bars(symbol, period + 2)
    if len(bars) < 2:
        return None
    true_ranges = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i-1]["c"]
        true_ranges.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(true_ranges[-period:]) / min(len(true_ranges), period)


def swing_low_stop(symbol, current_price, lookback=10):
    """
    Find the highest swing low below current_price in the last `lookback` bars.
    A swing low = a bar whose low is lower than both its neighbors.
    Returns None if no swing low found below price.
    """
    bars = get_daily_bars(symbol, lookback + 5)
    if len(bars) < 3:
        return None
    recent = bars[-(lookback + 2):]
    swing_lows = []
    for i in range(1, len(recent) - 1):
        if recent[i]["l"] < recent[i-1]["l"] and recent[i]["l"] < recent[i+1]["l"]:
            swing_lows.append(recent[i]["l"])
    # Take the highest swing low that's below current price (closest meaningful support)
    candidates = [sl for sl in swing_lows if sl < current_price]
    return max(candidates) if candidates else None


def portfolio_daily_loss_exceeded():
    """Returns True if portfolio has dropped more than DAILY_LOSS_LIMIT today."""
    try:
        r = requests.get(f"{BASE_URL}/account", headers=HEADERS)
        acct = r.json()
        equity      = float(acct["equity"])
        last_equity = float(acct.get("last_equity") or equity)
        if last_equity == 0:
            return False
        daily_return = (equity - last_equity) / last_equity
        if daily_return <= -DAILY_LOSS_LIMIT:
            log.warning(f"DAILY LOSS LIMIT HIT: {daily_return*100:.2f}% — halting all trading")
            return True
        return False
    except Exception:
        return False


def place_bracket_buy(symbol, qty, stop_price):
    return api_post("/orders", {
        "symbol": symbol,
        "qty": str(qty),
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "order_class": "bracket",
        "stop_loss": {"stop_price": str(round(stop_price, 2))},
        "take_profit": {"limit_price": "9999.00"},
    })


# ── One monitoring tick ─────────────────────────────────────────────────────────

def tick():
    """Evaluate the strategy once. Persists meaningful changes to git."""
    state = load_state()
    symbol        = state["symbol"]
    entry_price   = state["entry_price"]
    entry_qty     = state["entry_qty"]
    stop_order_id = state["stop_order_id"]
    current_stop  = state["current_stop"]
    trailing      = state["trailing_active"]
    hwm           = state["high_water_mark"]

    price = get_price(symbol)

    # Check if we still have a position
    position = get_position(symbol)

    # Sanity-check: warn if price is >60% below avg cost — likely a split/spinoff,
    # not a normal drawdown. Do not act automatically; flag for human review.
    if position:
        avg_cost = float(position.get("avg_entry_price") or 0)
        if avg_cost > 0 and price < avg_cost * 0.40:
            log.warning(
                f"POSSIBLE CORPORATE ACTION: {symbol} avg_cost=${avg_cost:.2f} "
                f"current=${price:.2f} ({(price/avg_cost-1)*100:.1f}%) — "
                f"verify for split/spinoff before acting on stop"
            )

    # ── Stop was hit: no position left ───────────────────────────────────────
    if position is None:
        stop_order = get_order(stop_order_id)
        if stop_order and stop_order["status"] == "filled":
            fill = float(stop_order.get("filled_avg_price") or current_stop)
            realized = (fill - entry_price) * entry_qty
            record_trade("tsla_trailing", symbol, realized, "stop hit")
            log.info(f"STOP HIT: sold {entry_qty} {symbol} @ ${fill:.2f}")
            print(f"[STOP HIT] Sold {entry_qty} {symbol} @ ${fill:.2f}")

            # Re-enter if price has recovered 2% above the stop
            if price >= fill * (1 + REENTRY_PCT):
                new_stop = round(price * (1 - TRAIL_OFFSET_PCT), 2)
                order = place_bracket_buy(symbol, entry_qty, new_stop)
                log.info(f"RE-ENTRY: bought {entry_qty} {symbol} @ market, new stop ${new_stop:.2f} | order {order['id']}")
                print(f"[RE-ENTRY] Bought {entry_qty} {symbol} @ market | stop ${new_stop:.2f}")
                state["entry_price"]   = price
                legs = order.get("legs", [])
                stop_leg = next((l for l in legs if l.get("type") == "stop_loss"), None)
                stop_leg_id = stop_leg["id"] if stop_leg else (legs[1]["id"] if len(legs) > 1 else order["id"])
                state["stop_order_id"] = stop_leg_id
                state["current_stop"]  = new_stop
                state["high_water_mark"] = price
                state["trailing_active"] = True
        persist_state(state, "chore: stop hit / re-entry")
        return

    # ── Activate trailing once up 10% ────────────────────────────────────────
    # Use HWM (not entry_price) so a restart with trailing_active=False but
    # price already above the trigger arms immediately rather than waiting for
    # the next new high.
    trigger_price = entry_price * (1 + TRAIL_TRIGGER_PCT)
    if not trailing and (hwm >= trigger_price or price >= trigger_price):
        log.info(f"TRAILING ACTIVATED: {symbol} HWM ${hwm:.2f} (trigger ${trigger_price:.2f}, entry ${entry_price:.2f})")
        print(f"[TRAILING ON] {symbol} HWM ${hwm:.2f}")
        state["trailing_active"] = True
        trailing = True
        persist_state(state, "chore: trailing activated")

    # Update HWM if price has made a new high (must happen BEFORE stop calculation
    # so the stop raise uses the current HWM, not last tick's value)
    if price > hwm:
        hwm = price
        state["high_water_mark"] = price
        log.info(f"HWM UPDATED: ${hwm:.2f}")

    # ── Raise the floor if trailing is active ────────────────────────────────
    if trailing:
        # Primary: ATR-based trailing; fallback to fixed %
        atr = get_atr(symbol)
        if atr:
            atr_stop = round(hwm - (atr * ATR_MULTIPLIER), 2)
        else:
            log.warning(f"ATR unavailable for {symbol} — falling back to fixed {TRAIL_OFFSET_PCT*100:.0f}% offset")
            atr_stop = round(hwm * (1 - TRAIL_OFFSET_PCT), 2)
        # Swing-level enhancement: if the nearest swing low is tighter (higher)
        # than the ATR stop, use it instead — real structure beats arbitrary math.
        swing = swing_low_stop(symbol, price)
        if swing and swing > atr_stop:
            new_stop = swing
            log.info(f"SWING STOP used: ${swing:.2f} (ATR stop was ${atr_stop:.2f})")
        else:
            new_stop = atr_stop
        if new_stop > current_stop:
            api_delete(f"/orders/{stop_order_id}")
            new_order = place_stop(symbol, entry_qty, new_stop)
            log.info(f"STOP RAISED: ${current_stop:.2f} → ${new_stop:.2f} | new order {new_order['id']}")
            print(f"[STOP RAISED] ${current_stop:.2f} → ${new_stop:.2f}")
            state["stop_order_id"] = new_order["id"]
            state["current_stop"]  = new_stop
            persist_state(state, "chore: stop raised")

    proximity_pct = (price - state["current_stop"]) / price if price > 0 else 1.0
    if proximity_pct < 0.02:
        log.warning(
            f"STOP PROXIMITY WARNING: {symbol} ${price:.2f} is only "
            f"{proximity_pct*100:.2f}% above stop ${state['current_stop']:.2f} — near stop-out"
        )
        consecutive = state.get("proximity_warn_count", 0) + 1
        state["proximity_warn_count"] = consecutive
        if consecutive >= 5 and consecutive % 5 == 0:
            print(
                f"[ALERT] {symbol} has been within 2% of stop for {consecutive} consecutive ticks "
                f"(${price:.2f} vs stop ${state['current_stop']:.2f}) — consider manual review"
            )
    else:
        state["proximity_warn_count"] = 0
    log.info(f"TICK: {symbol} ${price:.2f} | HWM ${hwm:.2f} | Stop ${state['current_stop']:.2f} | Trailing: {trailing}")
    save_state(state)  # disk only — routine HWM drift isn't worth a commit


# ── Self-looping main ─────────────────────────────────────────────────────────

def portfolio_corporate_action_scan():
    """Warn on any position where current price is less than 40% of avg cost — likely a split/spinoff."""
    try:
        positions = api_get("/positions")
        log.info(f"Corporate-action scan: checking {len(positions)} positions")
        flagged = 0
        for pos in positions:
            symbol   = pos.get("symbol", "")
            avg_raw  = pos.get("avg_entry_price")
            curr_raw = pos.get("current_price")
            # Log explicitly when fields are missing so silent failures are visible
            if avg_raw is None or curr_raw is None:
                log.info(f"Corporate-action scan: {symbol} skipped — avg_entry_price={avg_raw!r} current_price={curr_raw!r}")
                continue
            avg_cost = float(avg_raw)
            curr     = float(curr_raw)
            if avg_cost <= 0 or curr <= 0:
                log.info(f"Corporate-action scan: {symbol} skipped — zero/negative price (avg={avg_cost} curr={curr})")
                continue
            ratio = curr / avg_cost
            if ratio < 0.40:
                log.warning(
                    f"POSSIBLE CORPORATE ACTION: {symbol} avg_cost=${avg_cost:.2f} "
                    f"current=${curr:.2f} ({(ratio-1)*100:.1f}%) — "
                    f"verify for split/spinoff before acting"
                )
                flagged += 1
        log.info(f"Corporate-action scan complete: {flagged} flag(s) raised")
    except Exception as e:
        log.warning(f"Portfolio corporate-action scan failed (non-fatal): {e}")


def run():
    start = time.monotonic()
    log.info("Monitor loop started")
    print("Monitor loop started")
    portfolio_corporate_action_scan()

    while True:
        elapsed_min = (time.monotonic() - start) / 60
        if elapsed_min >= MAX_RUNTIME_MIN:
            log.info(f"Max runtime ({MAX_RUNTIME_MIN} min) reached — exiting; handoff job will continue.")
            break

        try:
            if not market_is_open():
                log.info("Market closed — exiting loop.")
                print("Market closed — exiting loop.")
                break
            if portfolio_daily_loss_exceeded():
                log.warning("Daily loss limit breached — halting for the day.")
                print("Daily loss limit breached — halting for the day.")
                break
            tick()
        except Exception as e:
            # One bad tick (transient API blip) must never kill the whole loop.
            log.exception(f"Tick error (continuing): {e}")

        time.sleep(POLL_INTERVAL_SEC)

    log.info("Monitor loop ended")


if __name__ == "__main__":
    run()
