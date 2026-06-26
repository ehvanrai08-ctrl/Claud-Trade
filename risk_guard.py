"""
Risk Guard — portfolio-level guardrails enforced in CODE, not prompts
=====================================================================
Every bot sizes its own position, but nothing used to stop *aggregate* risk:
ten bots could each take a "small" position on the same red day and blow the
account's daily budget, or one runaway entry could put 60% of equity in a single
name. The video's strongest operational point was "guardrails belong in code,
not in the prompt." This module is that code.

A bot calls ONE function right before it submits a buy:

    from risk_guard import can_enter
    ok, reason = can_enter("ibs", "QQQ", notional)
    if not ok:
        log.warning(f"Risk guard blocked entry: {reason}")
        return

Three hard limits, checked against LIVE Alpaca account/position data (the broker
is the single source of truth shared across every GitHub runner — no fragile
cross-process counter):

  1. DAILY LOSS KILL-SWITCH — once the account is down MAX_DAILY_LOSS_PCT on the
     day, no bot may open a NEW position. Exits are never blocked (this module is
     only called on entries). Mirrors the per-bot halt already in market_monitor.
  2. PER-POSITION CAP — a single symbol may not exceed MAX_POSITION_PCT of equity
     once this order fills (existing exposure + new notional).
  3. MAX NEW ENTRIES PER DAY — counts today's BUY orders at the broker; caps
     runaway days where a bug fires entry after entry.

Importable as a library WITHOUT a .env present (config.get fallbacks), so a bot
that imports it never crashes at import time. If the account lookup itself fails,
can_enter FAILS CLOSED (blocks the entry) — a guard that can't see risk must not
wave trades through.
"""

import os
import requests
from datetime import datetime, timezone
from dotenv import dotenv_values

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
config   = dotenv_values(f"{BASE_DIR}/.env")

BASE_URL = config.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2")
HEADERS  = {
    "APCA-API-KEY-ID":     config.get("ALPACA_API_KEY", ""),
    "APCA-API-SECRET-KEY": config.get("ALPACA_SECRET_KEY", ""),
}

# ── Limits (tune here — code is the contract, not a prompt) ────────────────────
MAX_DAILY_LOSS_PCT  = 0.04   # halt new entries once the account is -4% on the day
MAX_POSITION_PCT    = 0.25   # no single symbol may exceed 25% of equity
MAX_NEW_ENTRIES_DAY = 20     # cap total new BUY orders across all bots per day


def _get(path, params=None):
    try:
        r = requests.get(f"{BASE_URL}{path}", headers=HEADERS, params=params, timeout=15)
        return r.json() if r.ok else None
    except Exception:
        return None


def account_snapshot():
    """(equity, last_equity, daily_pnl_pct) or None if the lookup failed."""
    acct = _get("/account")
    if not acct:
        return None
    equity      = float(acct.get("equity", 0) or 0)
    last_equity = float(acct.get("last_equity", equity) or equity)
    if equity <= 0 or last_equity <= 0:
        return None
    daily_pct = (equity - last_equity) / last_equity
    return equity, last_equity, daily_pct


def position_value(symbol):
    """Current market value of an existing position in `symbol` (0.0 if flat)."""
    p = _get(f"/positions/{symbol}")
    if not p or "market_value" not in p:
        return 0.0
    try:
        return abs(float(p["market_value"]))
    except Exception:
        return 0.0


def new_buys_today():
    """Count of BUY orders submitted today (any status). Authoritative shared
    counter — every runner sees the same broker order book."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    orders = _get("/orders", {"status": "all", "after": f"{today}T00:00:00Z", "limit": 500})
    if not orders:
        return 0
    return sum(1 for o in orders if o.get("side") == "buy")


def can_enter(strategy, symbol, notional):
    """Return (allowed: bool, reason: str). Call immediately before a buy.

    Fails CLOSED: if the account snapshot is unavailable, the entry is blocked —
    a guard that cannot measure risk must not approve new exposure.
    """
    snap = account_snapshot()
    if snap is None:
        return False, "account lookup failed — blocking entry (fail-closed)"
    equity, _last, daily_pct = snap

    # 1. Daily loss kill-switch.
    if daily_pct <= -MAX_DAILY_LOSS_PCT:
        return False, (f"daily loss kill-switch: account {daily_pct*100:+.2f}% "
                       f"<= -{MAX_DAILY_LOSS_PCT*100:.0f}% — no new entries today")

    # 2. Per-position cap (existing exposure + this order).
    projected = position_value(symbol) + max(float(notional), 0)
    cap = MAX_POSITION_PCT * equity
    if projected > cap:
        return False, (f"position cap: {symbol} would be ${projected:,.0f} "
                       f"> {MAX_POSITION_PCT*100:.0f}% of equity (${cap:,.0f})")

    # 3. Max new entries per day.
    count = new_buys_today()
    if count >= MAX_NEW_ENTRIES_DAY:
        return False, f"max new entries reached: {count} buys today >= {MAX_NEW_ENTRIES_DAY}"

    return True, f"ok (day {daily_pct*100:+.2f}%, {count} buys so far)"


if __name__ == "__main__":
    # Manual smoke test against the live paper account.
    snap = account_snapshot()
    if not snap:
        print("Account lookup failed (no .env / no network?).")
    else:
        eq, le, dp = snap
        print(f"Equity ${eq:,.2f} | day {dp*100:+.2f}% | buys today {new_buys_today()}")
        for sym, notion in (("QQQ", 2000), ("SPY", 2000)):
            ok, why = can_enter("test", sym, notion)
            print(f"  can_enter({sym}, ${notion}) -> {ok}  [{why}]")
