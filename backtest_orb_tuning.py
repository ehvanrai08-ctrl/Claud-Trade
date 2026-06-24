"""
ORB tuning — find a configuration that lifts profit factor above 1.0, or prove
honestly that it can't in our unleveraged form. Tests real, non-hand-wavy ideas:

  A. baseline: hold to 3:55 close, stop at opposite OR edge (current live rule)
  B. R-multiple profit target: exit at entry +/- K*(risk), risk = OR range
  C. intraday trailing stop: trail by M*(OR range) from the best price reached
  D. directional filter: only longs above 20d SMA, only shorts below
  E. best combo

Single symbol (QQQ), real 5-min bars, no look-ahead. Reports PF/WR/Sharpe.
Run: python backtest_orb_tuning.py
"""

import os
import math
import statistics
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
from dotenv import dotenv_values

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
config   = dotenv_values(f"{BASE_DIR}/.env")
DATA_HEADERS = {
    "APCA-API-KEY-ID":     config["ALPACA_API_KEY"],
    "APCA-API-SECRET-KEY": config["ALPACA_SECRET_KEY"],
}
ET = ZoneInfo("America/New_York")

SYMBOL = "QQQ"
START  = "2023-01-01"
NOTIONAL = 2000
SLIP = 0.0005
MIN_RANGE_FRAC = 0.003


def fetch_5m(symbol, start):
    bars, token = [], None
    while True:
        params = {"timeframe": "5Min", "start": f"{start}T00:00:00Z",
                  "limit": 10000, "sort": "asc", "adjustment": "raw"}
        if token:
            params["page_token"] = token
        r = requests.get(f"https://data.alpaca.markets/v2/stocks/{symbol}/bars",
                         headers=DATA_HEADERS, params=params, timeout=45)
        if not r.ok:
            break
        j = r.json()
        bars.extend(j.get("bars") or [])
        token = j.get("next_page_token")
        if not token:
            break
    by_date = defaultdict(list)
    for b in bars:
        t = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(ET)
        b["_et"] = t
        by_date[t.date()].append(b)
    return by_date


def fetch_daily(symbol, start):
    r = requests.get(f"https://data.alpaca.markets/v2/stocks/{symbol}/bars",
                     headers=DATA_HEADERS,
                     params={"timeframe": "1Day", "start": f"{start}T00:00:00Z",
                             "limit": 10000, "sort": "asc", "adjustment": "raw"}, timeout=30)
    return r.json().get("bars") or [] if r.ok else []


def report(name, trades, years):
    if not trades:
        print(f"  {name:42} no trades"); return
    wins = [t for t in trades if t["pnl"] > 0]
    los  = [t for t in trades if t["pnl"] <= 0]
    gw = sum(t["pnl"] for t in wins); gl = sum(t["pnl"] for t in los)
    pf = abs(gw/gl) if gl else float("inf")
    wr = len(wins)/len(trades)*100
    total = sum(t["pnl"] for t in trades)
    rets = [t["ret"] for t in trades]
    sd = statistics.stdev(rets) if len(rets) > 1 else 0
    sharpe = (statistics.mean(rets)/sd)*math.sqrt(len(trades)/years) if sd > 0 else 0
    flag = " <<< PF>1" if pf > 1.0 else ""
    print(f"  {name:42} n={len(trades):4} WR={wr:4.1f}% PF={pf:4.2f} "
          f"Sharpe={sharpe:5.2f} P&L=${total:+7.0f}{flag}")


def simulate(by_date, daily, tp_mult=None, trail_mult=None, trend_filter=False):
    """tp_mult: take-profit at K*risk. trail_mult: trail by M*OR_range. Either/both/neither."""
    d_close = {b["t"][:10]: b["c"] for b in daily}
    d_dates = [b["t"][:10] for b in daily]
    trades = []
    for d in sorted(by_date):
        ds = str(d)
        bars = sorted(by_date[d], key=lambda b: b["_et"])
        orb = next((b for b in bars if b["_et"].hour == 9 and b["_et"].minute == 30), None)
        if not orb:
            continue
        o, c, hi, lo = orb["o"], orb["c"], orb["h"], orb["l"]
        rng = hi - lo
        if c == o or rng <= 0 or rng/c < MIN_RANGE_FRAC:
            continue
        direction = "long" if c > o else "short"
        if trend_filter and ds in d_dates:
            idx = d_dates.index(ds)
            if idx >= 20:
                sma20 = sum(d_close.get(d_dates[j], c) for j in range(idx-20, idx))/20
                if direction == "long" and c < sma20:
                    continue
                if direction == "short" and c > sma20:
                    continue
        stop = lo if direction == "long" else hi
        risk = abs(c - stop) if abs(c - stop) > 0 else rng
        after = [b for b in bars if b["_et"].hour > 9 or
                 (b["_et"].hour == 9 and b["_et"].minute >= 35)]
        if not after:
            continue
        entry = after[0]["o"] * (1+SLIP if direction == "long" else 1-SLIP)
        qty = int(NOTIONAL // entry)
        if qty < 1:
            continue
        target = None
        if tp_mult:
            target = entry + tp_mult*risk if direction == "long" else entry - tp_mult*risk
        best = entry   # for trailing
        exit_p, reason = None, None
        for b in after:
            if b["_et"].hour > 15 or (b["_et"].hour == 15 and b["_et"].minute >= 55):
                exit_p = b["o"]; reason = "eod"; break
            # stop
            if direction == "long" and b["l"] <= stop:
                exit_p = stop*(1-SLIP); reason = "stop"; break
            if direction == "short" and b["h"] >= stop:
                exit_p = stop*(1+SLIP); reason = "stop"; break
            # target
            if target is not None:
                if direction == "long" and b["h"] >= target:
                    exit_p = target*(1-SLIP); reason = "target"; break
                if direction == "short" and b["l"] <= target:
                    exit_p = target*(1+SLIP); reason = "target"; break
            # trailing
            if trail_mult:
                if direction == "long":
                    best = max(best, b["h"])
                    tstop = best - trail_mult*rng
                    if b["l"] <= tstop and tstop > stop:
                        exit_p = tstop*(1-SLIP); reason = "trail"; break
                else:
                    best = min(best, b["l"])
                    tstop = best + trail_mult*rng
                    if b["h"] >= tstop and tstop < stop:
                        exit_p = tstop*(1+SLIP); reason = "trail"; break
        if exit_p is None:
            exit_p = after[-1]["c"]; reason = "eod"
        pnl = ((exit_p-entry) if direction == "long" else (entry-exit_p)) * qty
        trades.append({"pnl": pnl, "ret": pnl/(entry*qty)*100, "reason": reason})
    return trades


if __name__ == "__main__":
    print(f"Fetching {SYMBOL} 5-min + daily since {START}…")
    by_date = fetch_5m(SYMBOL, START)
    daily   = fetch_daily(SYMBOL, START)
    years = max((max(by_date)-min(by_date)).days/365.25, 0.5)
    print(f"{len(by_date)} days\n")

    print("A. baseline (hold to close):")
    report("baseline", simulate(by_date, daily), years)

    print("\nB. R-multiple profit targets:")
    for k in (1, 1.5, 2, 3):
        report(f"target {k}R", simulate(by_date, daily, tp_mult=k), years)

    print("\nC. trailing stop (M * OR range):")
    for m in (0.5, 1.0, 1.5):
        report(f"trail {m}xOR", simulate(by_date, daily, trail_mult=m), years)

    print("\nD. directional (trend) filter only:")
    report("trend filter", simulate(by_date, daily, trend_filter=True), years)

    print("\nE. combos (trend filter + best target/trail):")
    for k in (1, 1.5, 2):
        report(f"trend + target {k}R", simulate(by_date, daily, tp_mult=k, trend_filter=True), years)
    for m in (0.5, 1.0):
        report(f"trend + trail {m}xOR", simulate(by_date, daily, trail_mult=m, trend_filter=True), years)
