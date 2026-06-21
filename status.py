"""
status.py — read-only health snapshot of the whole system.

Run `python status.py` for a one-shot view of the live paper account, every
position (with option-expiry warnings), the TSLA trailing-stop proximity, the
latest reports, and recent bot activity. Places no orders and writes no state —
purely GET requests and local file reads, safe to run anytime.
"""

import json
import os
import subprocess
from datetime import datetime
from dotenv import dotenv_values

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
config   = dotenv_values(f"{BASE_DIR}/.env")

BASE_URL = config.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2")
HEADERS  = {
    "APCA-API-KEY-ID":     config.get("ALPACA_API_KEY", ""),
    "APCA-API-SECRET-KEY": config.get("ALPACA_SECRET_KEY", ""),
}


def _get(path, params=None):
    r = requests.get(f"{BASE_URL}{path}", headers=HEADERS, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except Exception:
        return None


def account_section():
    acct = _get("/account")
    equity      = float(acct.get("equity", 0) or 0)
    last_equity = float(acct.get("last_equity", equity) or equity)
    cash        = float(acct.get("cash", 0) or 0)
    daily_pnl   = equity - last_equity
    daily_pct   = (daily_pnl / last_equity * 100) if last_equity else 0
    print("ACCOUNT")
    print(f"  Equity      ${equity:,.2f}")
    print(f"  Day P&L     ${daily_pnl:+,.2f} ({daily_pct:+.2f}%)")
    print(f"  Cash        ${cash:,.2f} ({(cash/equity*100) if equity else 0:.0f}% of book)")
    print(f"  Buying pwr  ${float(acct.get('buying_power', 0) or 0):,.2f}")


def positions_section():
    positions = _get("/positions")
    print("\nPOSITIONS")
    if not positions:
        print("  (none)")
        return
    total = sum(float(p.get("unrealized_pl", 0) or 0) for p in positions)
    ranked = sorted(positions, key=lambda p: float(p.get("unrealized_pl", 0) or 0), reverse=True)
    for p in ranked:
        pl   = float(p.get("unrealized_pl", 0) or 0)
        plpc = float(p.get("unrealized_plpc", 0) or 0) * 100
        flag = "  ⚠ down >20%" if plpc <= -20 else ""
        print(f"  {p['symbol']:22} qty {p['qty']:>10}  "
              f"@ ${float(p['avg_entry_price']):.2f} → ${float(p['current_price']):.2f}  "
              f"P&L ${pl:+,.2f} ({plpc:+.1f}%){flag}")
    print(f"  {'':22} {'':10}  {'total unrealized':>24}  ${total:+,.2f}")

    # Option expiry warnings (OCC symbol: ROOT + YYMMDD + C/P + 8-digit strike)
    for p in positions:
        sym = p["symbol"]
        if len(sym) >= 16 and sym[-9] in ("C", "P") and sym[-8:].isdigit():
            try:
                exp = datetime.strptime(sym[-15:-9], "%y%m%d").date()
                days = (exp - datetime.utcnow().date()).days
                kind = "call" if sym[-9] == "C" else "put"
                marker = "  ⚠" if days <= 10 else ""
                print(f"  {sym} ({kind}) expires {exp} — {days} day(s){marker}")
            except Exception:
                pass


def mean_reversion_section():
    raw = _read(f"{BASE_DIR}/mean_reversion_state.json")
    if not raw:
        return
    try:
        st = json.loads(raw)
        entries = st.get("entries", {})
        if not entries:
            return
        print("\nMEAN REVERSION POSITIONS")
        for sym, info in entries.items():
            sid = info.get("stop_order_id")
            stop_status = "no stop"
            if sid:
                try:
                    o = _get(f"/orders/{sid}")
                    stop_status = f"stop order {o.get('status','?')} @ ${info.get('stop_price','?')}"
                except Exception:
                    stop_status = f"stop order {sid[:8]}… (check failed)"
            print(f"  {sym:8} qty {info.get('qty','?')}  entry ${info.get('entry_price','?')}  "
                  f"RSI={info.get('entry_rsi','?')}  {stop_status}")
    except Exception:
        pass


def trailing_section():
    raw = _read(f"{BASE_DIR}/strategy_state.json")
    if not raw:
        return
    try:
        st = json.loads(raw)
        positions = _get("/positions")
        pos = next((p for p in positions if p["symbol"] == st.get("symbol")), None)
        if not pos:
            return
        price   = float(pos["current_price"])
        entry   = float(st.get("entry_price", 0) or 0)
        stop    = float(st.get("current_stop", 0) or 0)
        trigger = entry * 1.10
        print("\nTSLA TRAILING STOP")
        if st.get("trailing_active"):
            print(f"  ACTIVE — price ${price:.2f}, stop ${stop:.2f} "
                  f"({(price-stop)/price*100:.1f}% above stop)")
        elif entry:
            print(f"  Arms at ${trigger:.2f} (+10% from entry). Price ${price:.2f} — "
                  f"{(trigger-price)/price*100:+.1f}% away. Current stop ${stop:.2f}.")
    except Exception:
        pass


def reports_section():
    rdir = f"{BASE_DIR}/reports"
    print("\nREPORTS")
    try:
        files = sorted(f for f in os.listdir(rdir) if f.endswith(".md"))
        if not files:
            print("  (none)")
            return
        for f in files[-3:]:
            print(f"  reports/{f}")
        print(f"  Latest: reports/{files[-1]}")
    except Exception:
        print("  (no reports dir)")


def activity_section():
    print("\nRECENT COMMITS")
    try:
        out = subprocess.run(
            ["git", "-C", BASE_DIR, "log", "--oneline", "-8", "--no-decorate"],
            capture_output=True, text=True, timeout=10)
        print("\n".join("  " + ln for ln in out.stdout.splitlines()) or "  (none)")
    except Exception:
        print("  (git unavailable)")


def main():
    print(f"=== Claud-Trade status — {datetime.utcnow():%Y-%m-%d %H:%M} UTC ===\n")
    if not HEADERS["APCA-API-KEY-ID"]:
        print("⚠ No Alpaca keys found in .env — account/position data unavailable.")
        print("  (Workflows write .env from GitHub Secrets; this only affects local runs.)\n")
    else:
        try:
            account_section()
            positions_section()
            mean_reversion_section()
            trailing_section()
        except Exception as e:
            print(f"⚠ Could not reach Alpaca: {e}")
    reports_section()
    activity_section()


if __name__ == "__main__":
    main()
