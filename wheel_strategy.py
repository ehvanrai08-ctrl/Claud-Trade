"""
Wheel Strategy Bot — TSLA
Stage 1: Sell cash-secured puts at -10% strike, 2-4 weeks out
Stage 2: If assigned, sell covered calls at +10% above cost basis
- Close contracts early at 50% profit
- Never sell puts without enough cash to cover assignment
- Never sell calls below cost basis
- Daily summary at market close
- Runs every 15 minutes via GitHub Actions during market hours
"""

import json
import logging
import os
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import dotenv_values
from perf import record_trade

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
config = dotenv_values(f"{BASE_DIR}/.env")

BASE_URL  = config["ALPACA_BASE_URL"]
HEADERS   = {
    "APCA-API-KEY-ID":     config["ALPACA_API_KEY"],
    "APCA-API-SECRET-KEY": config["ALPACA_SECRET_KEY"],
    "Content-Type":        "application/json",
}

SYMBOL           = "TSLA"
PUT_STRIKE_PCT   = 0.90   # sell put 10% below current price
CALL_STRIKE_PCT  = 1.10   # sell call 10% above cost basis
MIN_EXP_DAYS     = 14
MAX_EXP_DAYS     = 28
EARLY_CLOSE_PCT  = 0.50   # close at 50% profit

STATE_FILE = f"{BASE_DIR}/wheel_state.json"

logging.basicConfig(
    filename=f"{BASE_DIR}/wheel.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger()


# ── Helpers ───────────────────────────────────────────────────────────────────

def api_get(path, params=None):
    r = requests.get(f"{BASE_URL}{path}", headers=HEADERS, params=params)
    r.raise_for_status()
    return r.json()

def api_post(path, payload):
    r = requests.post(f"{BASE_URL}{path}", headers=HEADERS, json=payload)
    if not r.ok:
        log.error(f"POST {path} failed: {r.status_code} {r.text}")
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

def market_is_open():
    return api_get("/clock")["is_open"]

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"stage": 1, "active_contract": None, "cost_basis": None,
                "total_premium": 0.0, "cycles": 0}
    with open(STATE_FILE) as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def get_cash():
    return float(api_get("/account")["cash"])

def get_position(symbol):
    try:
        return api_get(f"/positions/{symbol}")
    except Exception:
        return None

def get_options_quote(contract_symbol):
    """Return the ask price for a short option (cost to buy back), falling back to last trade."""
    try:
        r = requests.get(
            f"https://data.alpaca.markets/v1beta1/options/snapshots?symbols={contract_symbol}",
            headers=HEADERS,
        )
        if r.ok:
            snapshots = r.json().get("snapshots", {})
            snap = snapshots.get(contract_symbol, {})
            ask = snap.get("latestQuote", {}).get("ap")  # ask price
            if ask and float(ask) > 0:
                return float(ask)
    except Exception:
        pass
    # Fallback: last trade price
    try:
        r = requests.get(
            f"https://data.alpaca.markets/v1beta1/options/trades/latest?symbols={contract_symbol}",
            headers=HEADERS,
        )
        if r.ok:
            trades = r.json().get("trades", {})
            if contract_symbol in trades:
                return float(trades[contract_symbol]["p"])
    except Exception:
        pass
    return None

def find_best_contract(option_type, strike_target, min_days, max_days):
    """Find the contract closest to strike_target expiring in min-max days."""
    exp_from = (datetime.now() + timedelta(days=min_days)).strftime("%Y-%m-%d")
    exp_to   = (datetime.now() + timedelta(days=max_days)).strftime("%Y-%m-%d")
    low  = round(strike_target * 0.97, 0)
    high = round(strike_target * 1.03, 0)
    data = api_get("/options/contracts", params={
        "underlying_symbols":   SYMBOL,
        "type":                 option_type,
        "expiration_date_gte":  exp_from,
        "expiration_date_lte":  exp_to,
        "strike_price_gte":     str(low),
        "strike_price_lte":     str(high),
        "limit": 10,
    })
    contracts = data.get("option_contracts", [])
    if not contracts:
        return None
    # Pick closest strike to target
    return min(contracts, key=lambda c: abs(float(c["strike_price"]) - strike_target))

def sell_contract(contract_symbol, qty=1):
    return api_post("/orders", {
        "symbol":          contract_symbol,
        "qty":             str(qty),
        "side":            "sell",
        "type":            "market",
        "time_in_force":   "day",
    })

def close_contract(contract_symbol, qty=1):
    """Buy to close."""
    return api_post("/orders", {
        "symbol":        contract_symbol,
        "qty":           str(qty),
        "side":          "buy",
        "type":          "market",
        "time_in_force": "day",
    })

def get_option_position(contract_symbol):
    """Return (status, position): 'open', 'gone' (confirmed 404), or 'error'
    (transient API failure). Callers must NOT treat 'error' as 'gone' — declaring
    a live short contract expired on a network blip would record phantom P&L and
    sell a second contract on top of the first."""
    try:
        r = requests.get(f"{BASE_URL}/positions/{contract_symbol}",
                         headers=HEADERS, timeout=15)
        if r.ok:
            return "open", r.json()
        if r.status_code == 404:
            return "gone", None
        return "error", None
    except Exception:
        return "error", None


def get_options_bid(contract_symbol):
    """Bid price — what a SELLER receives. (get_options_quote's ask is the
    buy-back cost and overstates the credit on a sale.)"""
    try:
        r = requests.get(
            f"https://data.alpaca.markets/v1beta1/options/snapshots?symbols={contract_symbol}",
            headers=HEADERS,
        )
        if r.ok:
            snap = r.json().get("snapshots", {}).get(contract_symbol, {})
            bid = snap.get("latestQuote", {}).get("bp")
            if bid and float(bid) > 0:
                return float(bid)
    except Exception:
        pass
    return None


def sell_fill_price(order_id, contract_symbol):
    """Premium actually received on a sale: the order's real fill price, else
    the bid as the estimate."""
    try:
        filled = api_get(f"/orders/{order_id}")
        p = float(filled.get("filled_avg_price") or 0)
        if p > 0:
            return p
    except Exception:
        pass
    return get_options_bid(contract_symbol)


# ── Stage logic ───────────────────────────────────────────────────────────────

def stage1_sell_put(state, price):
    """Sell a cash-secured put at -10% strike."""
    cash = get_cash()
    strike_target = round(price * PUT_STRIKE_PCT, 0)
    required_cash = strike_target * 100  # 1 contract = 100 shares

    if cash < required_cash:
        log.warning(f"Not enough cash (${cash:,.0f}) to cover put assignment at ${strike_target} (need ${required_cash:,.0f})")
        print(f"[WHEEL] Insufficient cash for put. Need ${required_cash:,.0f}, have ${cash:,.0f}")
        return

    contract = find_best_contract("put", strike_target, MIN_EXP_DAYS, MAX_EXP_DAYS)
    if not contract:
        log.warning("No suitable put contract found")
        print("[WHEEL] No suitable put contract found")
        return

    order = sell_contract(contract["symbol"])
    premium = sell_fill_price(order["id"], contract["symbol"]) or 0
    collected = premium * 100

    state["stage"]           = 1
    state["active_contract"] = {
        "symbol":     contract["symbol"],
        "type":       "put",
        "strike":     float(contract["strike_price"]),
        "expiration": contract["expiration_date"],
        "sell_price": premium,
        "order_id":   order["id"],
    }
    state["total_premium"] += collected

    log.info(f"SOLD PUT: {contract['symbol']} strike=${contract['strike_price']} exp={contract['expiration_date']} premium~${premium:.2f} | order {order['id']}")
    print(f"[WHEEL] Sold put: {contract['symbol']} @ ${contract['strike_price']} exp {contract['expiration_date']} | premium ~${premium:.2f}/share")


def stage2_sell_call(state, cost_basis):
    """Sell a covered call at +10% above cost basis."""
    position = get_position(SYMBOL)
    if not position or int(float(position["qty"])) < 100:
        log.warning("Need 100 shares to sell covered call")
        print("[WHEEL] Need 100 shares to sell covered call")
        return

    strike_target = round(cost_basis * CALL_STRIKE_PCT, 0)
    if strike_target < cost_basis:
        strike_target = round(cost_basis * 1.02, 0)  # never sell below cost basis

    contract = find_best_contract("call", strike_target, MIN_EXP_DAYS, MAX_EXP_DAYS)
    if not contract:
        log.warning("No suitable call contract found")
        return

    order = sell_contract(contract["symbol"])
    premium = sell_fill_price(order["id"], contract["symbol"]) or 0
    collected = premium * 100

    state["stage"]           = 2
    state["active_contract"] = {
        "symbol":     contract["symbol"],
        "type":       "call",
        "strike":     float(contract["strike_price"]),
        "expiration": contract["expiration_date"],
        "sell_price": premium,
        "order_id":   order["id"],
    }
    state["total_premium"] += collected

    log.info(f"SOLD CALL: {contract['symbol']} strike=${contract['strike_price']} exp={contract['expiration_date']} premium~${premium:.2f} | order {order['id']}")
    print(f"[WHEEL] Sold call: {contract['symbol']} @ ${contract['strike_price']} exp {contract['expiration_date']} | premium ~${premium:.2f}/share")


def check_early_close(state):
    """Close contract early if it has gained 50% in value (i.e., price dropped to 50% of what we sold it for)."""
    contract = state.get("active_contract")
    if not contract:
        return False

    current_price = get_options_quote(contract["symbol"])
    sell_price    = contract.get("sell_price", 0)

    if not current_price or not sell_price:
        return False

    STOP_LOSS_PCT = 2.0   # close if contract costs 200% of premium received (lost 2x)

    profit_pct = (sell_price - current_price) / sell_price
    log.info(f"EARLY CLOSE CHECK: {contract['symbol']} sell=${sell_price:.2f} now=${current_price:.2f} pnl={profit_pct*100:.1f}% (close_target=+{EARLY_CLOSE_PCT*100:.0f}%)")

    if profit_pct >= EARLY_CLOSE_PCT:
        order = close_contract(contract["symbol"])
        locked = (sell_price - current_price) * 100
        record_trade("wheel", contract["symbol"], locked, "early close 50% profit")
        log.info(f"EARLY CLOSE ({profit_pct*100:.0f}% profit): {contract['symbol']} buy_back=${current_price:.2f} | locked ${locked:.2f} | order {order['id']}")
        print(f"[WHEEL] Early close at {profit_pct*100:.0f}% profit: {contract['symbol']} | locked in ${locked:.2f}")
        state["active_contract"] = None
        state["cycles"] += 1
        return True

    loss_pct = (current_price - sell_price) / sell_price
    if loss_pct >= STOP_LOSS_PCT:
        order = close_contract(contract["symbol"])
        loss_amt = (current_price - sell_price) * 100
        record_trade("wheel", contract["symbol"], -loss_amt, "stop loss 200% of premium")
        log.warning(f"STOP LOSS ({loss_pct*100:.0f}% loss): {contract['symbol']} buy_back=${current_price:.2f} | loss -${loss_amt:.2f} | order {order['id']}")
        print(f"[WHEEL] Stop loss triggered at {loss_pct*100:.0f}% loss: {contract['symbol']} | -${loss_amt:.2f}")
        state["active_contract"] = None
        state["cycles"] += 1
        return True

    return False


def check_assignment_or_expiry(state):
    """Check if we were assigned (put) or shares called away (call), or if contract expired."""
    contract = state.get("active_contract")
    if not contract:
        return

    status, _position = get_option_position(contract["symbol"])

    # Only act on a CONFIRMED 404 — 'open' means nothing to do, and 'error'
    # (transient API failure) must not be read as "contract gone": that would
    # record phantom P&L and sell a second contract on top of a live one.
    # The next 15-minute run retries for free.
    if status != "gone":
        return

    if contract["type"] == "put":
        # Check if we now own TSLA shares (assignment)
        stock_pos = get_position(SYMBOL)
        if stock_pos and int(float(stock_pos["qty"])) >= 100:
            # Cost basis = the strike we were assigned at. The broker's
            # avg_entry_price is blended with the TSLA monitor bot's own
            # shares, which would skew the covered-call strike.
            cost_basis = float(contract["strike"])
            state["stage"]           = 2
            state["active_contract"] = None
            state["cost_basis"]      = cost_basis
            log.info(f"PUT ASSIGNED: now own {stock_pos['qty']} {SYMBOL} @ strike ${cost_basis:.2f}")
            print(f"[WHEEL] Put assigned — own {stock_pos['qty']} shares @ ${cost_basis:.2f}. Moving to Stage 2.")
        else:
            # Expired worthless — back to Stage 1
            kept = contract.get("sell_price", 0) * 100
            record_trade("wheel", contract["symbol"], kept, "put expired worthless")
            state["stage"]           = 1
            state["active_contract"] = None
            state["cycles"]         += 1
            log.info(f"PUT EXPIRED WORTHLESS: {contract['symbol']} — premium kept, cycling back to Stage 1")
            print("[WHEEL] Put expired worthless. Premium kept. Back to Stage 1.")

    elif contract["type"] == "call":
        stock_pos = get_position(SYMBOL)
        if not stock_pos or int(float(stock_pos["qty"])) < 100:
            # Shares called away
            state["stage"]           = 1
            state["active_contract"] = None
            state["cost_basis"]      = None
            state["cycles"]         += 1
            log.info(f"CALL ASSIGNED: shares sold at ${contract['strike']} — back to Stage 1")
            print(f"[WHEEL] Shares called away at ${contract['strike']}. Back to Stage 1.")
        else:
            # Call expired worthless
            kept = contract.get("sell_price", 0) * 100
            record_trade("wheel", contract["symbol"], kept, "call expired worthless")
            state["stage"]           = 2
            state["active_contract"] = None
            state["cycles"]         += 1
            log.info(f"CALL EXPIRED WORTHLESS: {contract['symbol']} — premium kept, selling another call")
            print("[WHEEL] Call expired worthless. Selling another covered call.")


def daily_summary(state):
    # Fire on the last in-hours run of the day (~3:45 PM ET). run() returns
    # early when the market is closed, so the old 4 PM trigger could never fire.
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if now_et.hour == 15 and now_et.minute >= 45:
        price = get_price(SYMBOL)
        position = get_position(SYMBOL)
        shares = int(float(position["qty"])) if position else 0
        unrealized = float(position["unrealized_pl"]) if position else 0
        contract = state.get("active_contract")

        summary = (
            f"\n{'='*55}\n"
            f"WHEEL DAILY SUMMARY — {now_et.strftime('%Y-%m-%d')}\n"
            f"{'='*55}\n"
            f"  Stage           : {state['stage']} ({'Selling puts' if state['stage']==1 else 'Selling calls'})\n"
            f"  TSLA price      : ${price:.2f}\n"
            f"  Shares held     : {shares}\n"
        )
        # Conditional on its own line so a None cost basis can't swallow the header.
        summary += (f"  Cost basis      : ${state['cost_basis']:.2f}\n"
                    if state.get("cost_basis") else "  Cost basis      : —\n")
        summary += (
            f"  Unrealized P&L  : ${unrealized:+.2f}\n"
            f"  Total premiums  : ${state['total_premium']:,.2f}\n"
            f"  Cycles completed: {state['cycles']}\n"
        )
        if contract:
            summary += (
                f"  Active contract : {contract['symbol']}\n"
                f"    Type          : {contract['type'].upper()}\n"
                f"    Strike        : ${contract['strike']:.2f}\n"
                f"    Expires       : {contract['expiration']}\n"
            )
        summary += f"{'='*55}"
        print(summary)
        log.info(f"DAILY SUMMARY: stage={state['stage']} shares={shares} premiums=${state['total_premium']:,.2f} cycles={state['cycles']}")


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    if not market_is_open():
        return

    state = load_state()
    price = get_price(SYMBOL)

    log.info(f"TICK: {SYMBOL} ${price:.2f} stage={state['stage']} contract={state.get('active_contract', {}).get('symbol') if state.get('active_contract') else 'none'}")

    # Check if current contract hit 50% profit — close early
    if state.get("active_contract"):
        closed = check_early_close(state)
        if not closed:
            check_assignment_or_expiry(state)

    # No active contract — act based on stage
    if not state.get("active_contract"):
        if state["stage"] == 1:
            # Guard: verify no open short-option sell order already exists before
            # selling. Note: a symbols=TSLA filter would NOT match option orders
            # (their symbol is the OCC contract, e.g. TSLA260715P00375000), so we
            # fetch all open orders and match on the contract-root prefix. Fails
            # CLOSED — if we can't see the order book we must not sell into it.
            try:
                open_orders = api_get("/orders", params={"status": "open", "limit": 100})
                dupes = [o for o in open_orders
                         if o.get("asset_class") == "us_option"
                         and o.get("side") == "sell"
                         and str(o.get("symbol", "")).startswith(SYMBOL)]
            except Exception as e:
                log.warning(f"Order-book check failed ({e}) — skipping put sale this run.")
                print("[WHEEL] Skipping — could not verify open orders")
                dupes = None
            if dupes is None:
                pass
            elif dupes:
                log.warning(f"Skipping new put sale — open option sell order already exists: {dupes[0]['id']}")
                print(f"[WHEEL] Skipping — active sell order found: {dupes[0]['id']}")
            else:
                stage1_sell_put(state, price)
        elif state["stage"] == 2:
            cost = state.get("cost_basis")
            if cost:
                stage2_sell_call(state, cost)
            else:
                # Shouldn't happen, but recover gracefully
                state["stage"] = 1
                stage1_sell_put(state, price)

    daily_summary(state)
    save_state(state)


if __name__ == "__main__":
    run()
