"""
Backtest the LIVE Mean Reversion bot (mean_reversion.py) faithfully.
Same universe, same indicators, same entry/exit/stop, max 4 concurrent.
Research only — not wired into anything.

Entry (at close): RSI(14)<30 AND close<lower Bollinger(20,2) AND close>prev close
Exit:  RSI(14)>70  OR  close>middle Bollinger  OR  -8% stop (intraday low)
Size:  $2000/name, max 4 concurrent positions.

Run: python backtest_meanrev.py
"""

import os
import math
import statistics
import requests
from dotenv import dotenv_values

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
config   = dotenv_values(f"{BASE_DIR}/.env")
DATA_HEADERS = {
    "APCA-API-KEY-ID":     config["ALPACA_API_KEY"],
    "APCA-API-SECRET-KEY": config["ALPACA_SECRET_KEY"],
}

UNIVERSE = ["KO", "PEP", "JNJ", "WMT", "PG", "MCD", "VZ", "MRK"]
START    = "2015-01-01"
RSI_P, RSI_OS, RSI_OB = 14, 30, 70
BB_P, BB_STD = 20, 2.0
SIZE, MAX_POS, STOP = 2000, 4, 0.08


def fetch_daily(symbol, start=START):
    bars, token = [], None
    while True:
        params = {"timeframe": "1Day", "start": f"{start}T00:00:00Z",
                  "limit": 10000, "sort": "asc", "adjustment": "raw"}
        if token:
            params["page_token"] = token
        r = requests.get(f"https://data.alpaca.markets/v2/stocks/{symbol}/bars",
                         headers=DATA_HEADERS, params=params, timeout=30)
        if not r.ok:
            break
        j = r.json()
        bars.extend(j.get("bars") or [])
        token = j.get("next_page_token")
        if not token:
            break
    return bars


def rsi(closes, period=RSI_P):
    if len(closes) < period + 1:
        return None
    g = l = 0.0
    for i in range(-period, 0):
        ch = closes[i] - closes[i-1]
        g += max(ch, 0); l += max(-ch, 0)
    ag, al = g/period, l/period
    if al == 0:
        return 100.0
    return 100 - 100/(1 + ag/al)


def bollinger(closes, period=BB_P, n=BB_STD):
    if len(closes) < period:
        return None
    w = closes[-period:]
    m = sum(w)/period
    sd = (sum((c-m)**2 for c in w)/period) ** 0.5
    return {"middle": m, "lower": m - n*sd, "upper": m + n*sd}


def backtest():
    data = {s: fetch_daily(s) for s in UNIVERSE}
    for s in UNIVERSE:
        print(f"  {s}: {len(data[s])} bars")

    # common date index
    dates = sorted(set.intersection(*[{b["t"][:10] for b in data[s]} for s in UNIVERSE]))
    byd = {s: {b["t"][:10]: b for b in data[s]} for s in UNIVERSE}

    open_pos = {}     # symbol -> {entry, qty, stop, entry_date}
    trades = []

    for di, d in enumerate(dates):
        # ── manage exits first ──
        for s in list(open_pos.keys()):
            p = open_pos[s]
            bar = byd[s].get(d)
            if not bar:
                continue
            closes = [byd[s][dd]["c"] for dd in dates[max(0, di-40):di+1] if dd in byd[s]]
            # stop check (intraday low)
            if bar["l"] <= p["stop"]:
                ex = p["stop"]
                pnl = (ex - p["entry"]) * p["qty"]
                trades.append({"symbol": s, "entry_date": p["entry_date"], "exit_date": d,
                               "pnl": pnl, "ret_pct": (ex/p["entry"]-1)*100, "reason": "stop"})
                del open_pos[s]; continue
            rv = rsi(closes); bb = bollinger(closes)
            reason = None
            if rv is not None and rv > RSI_OB:
                reason = "rsi_ob"
            elif bb and bar["c"] > bb["middle"]:
                reason = "mean"
            if reason:
                ex = bar["c"]
                pnl = (ex - p["entry"]) * p["qty"]
                trades.append({"symbol": s, "entry_date": p["entry_date"], "exit_date": d,
                               "pnl": pnl, "ret_pct": (ex/p["entry"]-1)*100, "reason": reason})
                del open_pos[s]

        # ── entries (respect max concurrent) ──
        for s in UNIVERSE:
            if len(open_pos) >= MAX_POS:
                break
            if s in open_pos:
                continue
            if d not in byd[s] or di < 1:
                continue
            closes = [byd[s][dd]["c"] for dd in dates[max(0, di-40):di+1] if dd in byd[s]]
            if len(closes) < BB_P + 1:
                continue
            price = closes[-1]; prev = closes[-2]
            rv = rsi(closes); bb = bollinger(closes)
            if rv is None or bb is None:
                continue
            if rv < RSI_OS and price < bb["lower"] and price > prev:
                qty = int(SIZE // price)
                if qty < 1:
                    continue
                open_pos[s] = {"entry": price, "qty": qty,
                               "stop": round(price*(1-STOP), 2), "entry_date": d}

    # ── metrics ──
    if not trades:
        print("\n  No trades generated.")
        return
    wins = [t for t in trades if t["pnl"] > 0]
    los  = [t for t in trades if t["pnl"] <= 0]
    total = sum(t["pnl"] for t in trades)
    gw = sum(t["pnl"] for t in wins); gl = sum(t["pnl"] for t in los)
    pf = abs(gw/gl) if gl else float("inf")
    wr = len(wins)/len(trades)*100
    rets = [t["ret_pct"] for t in trades]
    avg = sum(rets)/len(rets)
    sd = statistics.stdev(rets) if len(rets) > 1 else 0
    yrs = (int(dates[-1][:4]) - int(dates[0][:4])) + 1
    tpy = len(trades)/yrs
    sharpe = (avg/sd)*math.sqrt(tpy) if sd > 0 else 0
    # equity drawdown
    eq, peak, mdd = 0, 0, 0
    for t in sorted(trades, key=lambda x: x["exit_date"]):
        eq += t["pnl"]; peak = max(peak, eq)
        if peak > 0:
            mdd = max(mdd, (peak-eq)/peak*100)
    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1

    print("\n" + "="*60)
    print("MEAN REVERSION (live bot rules) — backtest")
    print("="*60)
    print(f"  Window: {dates[0]} → {dates[-1]} (~{yrs}y)")
    print(f"  Trades: {len(trades)} ({tpy:.0f}/yr)")
    print(f"  Win rate: {wr:.1f}%   Profit factor: {pf:.2f}")
    print(f"  Avg trade: {avg:+.2f}%   Total P&L: ${total:+,.0f}")
    print(f"  Per-trade Sharpe: {sharpe:.2f}   Max DD (closed-trade): {mdd:.1f}%")
    print(f"  Exit reasons: {reasons}")
    avg_win = (sum(t['ret_pct'] for t in wins)/len(wins)) if wins else 0
    avg_los = (sum(t['ret_pct'] for t in los)/len(los)) if los else 0
    print(f"  Avg win: {avg_win:+.2f}%   Avg loss: {avg_los:+.2f}%")


if __name__ == "__main__":
    backtest()
