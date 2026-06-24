"""
Backtest candidate strategies NOT currently in the live system.
For research only — these are NOT wired into any live bot.

Candidates (all documented >60% win rate in the literature):
  1. IBS (Internal Bar Strength)  — QuantifiedStrategies: ~74% WR, Sharpe 1.7
  2. Connors RSI(2)               — 70-85% WR documented
  3. Alvarez 3-Lower-Lows         — 67.8% WR, CAGR ~22% (Russell 1000)

Faithful daily-bar simulation, no look-ahead (every signal uses only data
available at that day's close; limit fills checked against the NEXT day's range).

Run: python backtest_candidates.py
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

SYMBOLS = ["SPY", "QQQ"]
START   = "2015-01-01"
ALLOC   = 10000
SLIP    = 0.0005


def fetch_daily(symbol, start=START):
    bars, token = [], None
    while True:
        params = {"timeframe": "1Day", "start": f"{start}T00:00:00Z",
                  "limit": 10000, "sort": "asc", "adjustment": "all"}
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


def rsi(closes, period):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(-period, 0):
        ch = closes[i] - closes[i-1]
        gains.append(max(ch, 0)); losses.append(max(-ch, 0))
    ag = sum(gains)/period; al = sum(losses)/period
    if al == 0:
        return 100.0
    return 100 - 100/(1 + ag/al)


def ibs(b):
    rng = b["h"] - b["l"]
    return (b["c"] - b["l"]) / rng if rng > 0 else 0.5


def sma(closes, n):
    return sum(closes[-n:]) / n if len(closes) >= n else None


def atr(bars, n=10):
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(-n, 0):
        hi, lo, pc = bars[i]["h"], bars[i]["l"], bars[i-1]["c"]
        trs.append(max(hi-lo, abs(hi-pc), abs(lo-pc)))
    return sum(trs) / n


def metrics(trades, label, years, bh):
    if not trades:
        return f"{label}: no trades"
    wins = [t for t in trades if t["pnl"] > 0]
    los  = [t for t in trades if t["pnl"] <= 0]
    total = sum(t["pnl"] for t in trades)
    gw = sum(t["pnl"] for t in wins); gl = sum(t["pnl"] for t in los)
    pf = abs(gw/gl) if gl else float("inf")
    wr = len(wins)/len(trades)*100
    rets = [t["ret_pct"] for t in trades]
    avg = sum(rets)/len(rets)
    sd = statistics.stdev(rets) if len(rets) > 1 else 0
    tpy = len(trades)/years
    sharpe = (avg/sd)*math.sqrt(tpy) if sd > 0 else 0
    eq, peak, mdd = 0, 0, 0
    for t in sorted(trades, key=lambda x: x["exit_date"]):
        eq += t["pnl"]; peak = max(peak, eq)
        if peak > 0:
            mdd = max(mdd, (peak-eq)/peak*100)
    strat_ret = total/ALLOC*100
    return (f"{label}\n"
            f"    Trades: {len(trades)} ({len(trades)/years:.0f}/yr) | "
            f"Win: {wr:.1f}% | PF: {pf:.2f} | Avg: {avg:+.2f}%\n"
            f"    Total P&L (1 unit recycled): ${total:+,.0f} ({strat_ret:+.0f}%) | "
            f"Sharpe: {sharpe:.2f} | MaxDD: {mdd:.1f}%\n"
            f"    Buy-hold same window: {bh:+.0f}%")


# ── 1. IBS strategy ───────────────────────────────────────────────────────────
# Buy when IBS < 0.2; sell when IBS > 0.8. Optional 200-day trend filter.

def bt_ibs(symbol, bars, trend_filter=True):
    trades = []
    in_pos = False; entry = ed = qty = 0
    for i in range(1, len(bars)):
        today = bars[i]
        closes = [b["c"] for b in bars[:i+1]]
        if in_pos:
            if ibs(today) > 0.8:
                ex = today["c"]*(1-SLIP)
                pnl = (ex-entry)*qty
                trades.append({"entry_date": ed, "exit_date": today["t"][:10],
                               "pnl": pnl, "ret_pct": (ex/entry-1)*100})
                in_pos = False
            continue
        if i < 200:
            continue
        s200 = sma(closes, 200)
        if trend_filter and today["c"] < s200:
            continue
        if ibs(today) < 0.2:
            entry = today["c"]*(1+SLIP); qty = ALLOC/entry
            ed = today["t"][:10]; in_pos = True
    return trades


# ── 2. Connors RSI(2) ─────────────────────────────────────────────────────────
# Entry: RSI(2) < 10 AND close > 200-day SMA. Exit: close > 5-day SMA.

def bt_rsi2(symbol, bars):
    trades = []
    in_pos = False; entry = ed = qty = 0
    for i in range(1, len(bars)):
        today = bars[i]
        closes = [b["c"] for b in bars[:i+1]]
        if in_pos:
            s5 = sma(closes, 5)
            if today["c"] > s5:
                ex = today["c"]*(1-SLIP)
                pnl = (ex-entry)*qty
                trades.append({"entry_date": ed, "exit_date": today["t"][:10],
                               "pnl": pnl, "ret_pct": (ex/entry-1)*100})
                in_pos = False
            continue
        if i < 200:
            continue
        s200 = sma(closes, 200)
        r2 = rsi(closes, 2)
        if r2 is None:
            continue
        if r2 < 10 and today["c"] > s200:
            entry = today["c"]*(1+SLIP); qty = ALLOC/entry
            ed = today["t"][:10]; in_pos = True
    return trades


# ── 3. Alvarez 3-Lower-Lows ───────────────────────────────────────────────────
# Setup (at close): close>100-day MA, close<5-day MA, 3 consecutive lower lows.
# Limit buy NEXT day at (close - 0.5*ATR10); exit next open after an up-close.

def bt_alvarez(symbol, bars):
    trades = []
    in_pos = False; entry = ed = qty = 0
    pending_limit = None
    for i in range(1, len(bars)):
        today = bars[i]
        closes = [b["c"] for b in bars[:i+1]]

        # Handle a resting limit from yesterday's setup
        if not in_pos and pending_limit is not None:
            if today["l"] <= pending_limit:           # limit filled intraday
                entry = pending_limit*(1+SLIP); qty = ALLOC/entry
                ed = today["t"][:10]; in_pos = True
            pending_limit = None                       # limit valid 1 day only

        if in_pos:
            # exit on the open AFTER an up-close day
            if today["c"] > bars[i-1]["c"]:
                ex = today["c"]*(1-SLIP)               # approximate next-open with close
                pnl = (ex-entry)*qty
                trades.append({"entry_date": ed, "exit_date": today["t"][:10],
                               "pnl": pnl, "ret_pct": (ex/entry-1)*100})
                in_pos = False
            continue

        if i < 100:
            continue
        s100 = sma(closes, 100); s5 = sma(closes, 5)
        a = atr(bars[:i+1], 10)
        if a is None:
            continue
        three_lower = (bars[i]["l"] < bars[i-1]["l"] < bars[i-2]["l"])
        if today["c"] > s100 and today["c"] < s5 and three_lower:
            pending_limit = today["c"] - 0.5*a         # rest a limit for tomorrow
    return trades


if __name__ == "__main__":
    data = {s: fetch_daily(s) for s in SYMBOLS}
    for s in SYMBOLS:
        print(f"  {s}: {len(data[s])} daily bars")
    years = 9.5
    bh = {s: (data[s][-1]["c"]/data[s][0]["c"]-1)*100 for s in SYMBOLS}

    print("\n" + "="*70)
    print("CANDIDATE 1: IBS (Internal Bar Strength) — buy IBS<0.2, sell IBS>0.8")
    print("="*70)
    for s in SYMBOLS:
        print(metrics(bt_ibs(s, data[s], trend_filter=True), f"  IBS {s} (200d trend filter)", years, bh[s]))
        print(metrics(bt_ibs(s, data[s], trend_filter=False), f"  IBS {s} (no filter)", years, bh[s]))

    print("\n" + "="*70)
    print("CANDIDATE 2: Connors RSI(2) — RSI2<10 & close>200d SMA, exit close>5d SMA")
    print("="*70)
    for s in SYMBOLS:
        print(metrics(bt_rsi2(s, data[s]), f"  RSI(2) {s}", years, bh[s]))

    print("\n" + "="*70)
    print("CANDIDATE 3: Alvarez 3-Lower-Lows — limit buy 0.5*ATR below, exit on up-close")
    print("="*70)
    for s in SYMBOLS:
        print(metrics(bt_alvarez(s, data[s]), f"  Alvarez {s}", years, bh[s]))

    print("\n" + "="*70)
    print("NOTE: research only — NOT wired into any live bot.")
    print("="*70)
