"""
Improved backtests — testing targeted fixes for each broken strategy.

Diagnoses found from backtest_bots.py:
  DM:      Went into AGG in 2022 when bonds crashed -15%. Fix: use BIL (cash)
           as the risk-off asset instead of AGG (avoids duration risk).
  ORB:     Trades every day unconditionally (win rate 23%, PF 0.72, losing).
           Fix: (a) wider OR-range filter, (b) trend direction filter,
           (c) volume confirmation.
  SIP-ORB: Stop at 0.10×ATR = $0.70 on QQQ — 23% of OR range, stopped out 91%
           of the time. Fix: stop at opposite OR boundary (same as paper/vanilla ORB).

Run: python backtest_improvements.py
"""

import os
import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import time
import requests
from dotenv import dotenv_values

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
config   = dotenv_values(f"{BASE_DIR}/.env")
DATA_HEADERS = {
    "APCA-API-KEY-ID":     config["ALPACA_API_KEY"],
    "APCA-API-SECRET-KEY": config["ALPACA_SECRET_KEY"],
}
ET = ZoneInfo("America/New_York")


def fetch_daily(symbol, start, adjustment="all"):
    bars, token = [], None
    while True:
        params = {"timeframe": "1Day", "start": f"{start}T00:00:00Z",
                  "limit": 10000, "sort": "asc", "adjustment": adjustment}
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


def fetch_5m_sym(symbol, start, end=None):
    bars, token = [], None
    while True:
        params = {"timeframe": "5Min", "start": f"{start}T00:00:00Z",
                  "limit": 10000, "sort": "asc", "adjustment": "raw"}
        if end:
            params["end"] = f"{end}T23:59:59Z"
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
        t_et = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(ET)
        b["_et"] = t_et
        by_date[t_et.date()].append(b)
    return by_date


def fetch_multi_5m(symbols, start, end=None):
    """Multi-symbol 5-min fetch with retry/backoff on 429."""
    result = {}
    for sym in symbols:
        bars, token = [], None
        while True:
            params = {"timeframe": "5Min", "start": f"{start}T00:00:00Z",
                      "limit": 10000, "sort": "asc", "adjustment": "raw"}
            if end:
                params["end"] = f"{end}T23:59:59Z"
            if token:
                params["page_token"] = token
            for attempt in range(3):
                r = requests.get(
                    f"https://data.alpaca.markets/v2/stocks/{sym}/bars",
                    headers=DATA_HEADERS, params=params, timeout=45)
                if r.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                break
            if not r.ok:
                break
            j = r.json()
            bars.extend(j.get("bars") or [])
            token = j.get("next_page_token")
            if not token:
                break
        by_date = defaultdict(list)
        for b in bars:
            t_et = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(ET)
            b["_et"] = t_et
            by_date[t_et.date()].append(b)
        if by_date:
            result[sym] = by_date
        time.sleep(0.3)
    return result


def summarize(label, trades, years, bh_ret=None):
    if not trades:
        print(f"  {label}: no trades"); return
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total  = sum(t["pnl"] for t in trades)
    gw     = sum(t["pnl"] for t in wins)
    gl     = sum(t["pnl"] for t in losses)
    pf     = abs(gw / gl) if gl else float("inf")
    wr     = len(wins) / len(trades) * 100
    rets   = [t.get("ret_pct", 0) for t in trades]
    avg_r  = sum(rets) / len(rets)
    if len(rets) > 1 and statistics.stdev(rets) > 0:
        tpy = len(trades) / years
        sharpe = (avg_r / statistics.stdev(rets)) * math.sqrt(tpy)
    else:
        sharpe = 0
    eq, peak, mdd = 0, 0, 0
    for t in sorted(trades, key=lambda x: x["exit_date"]):
        eq += t["pnl"]; peak = max(peak, eq)
        if peak > 0:
            mdd = max(mdd, (peak-eq)/peak*100)
    stops = sum(1 for t in trades if t.get("reason") == "stop")
    print(f"  {label}")
    print(f"    Trades: {len(trades)} ({len(trades)/years:.0f}/yr) | "
          f"Win: {wr:.1f}% | PF: {pf:.2f} | Avg: {avg_r:+.2f}% | Sharpe: {sharpe:.2f}")
    print(f"    Total P&L: ${total:+,.0f} | Max DD: {mdd:.1f}% | "
          f"Stop exits: {stops}/{len(trades)} ({stops/len(trades)*100:.0f}%)")
    if bh_ret is not None:
        print(f"    B&H: {bh_ret:+.1f}%")
    return {"pf": pf, "wr": wr, "sharpe": sharpe, "total": total, "mdd": mdd}


# ══════════════════════════════════════════════════════════════════════════════
# FIX 1: Dual Momentum — replace AGG with BIL as risk-off asset
# ══════════════════════════════════════════════════════════════════════════════

def dm_backtest(risk_off_asset, label):
    LOOKBACKS = [6, 7, 8, 9, 10, 11, 12]
    TDPM = 21
    symbols = ["SPY", "EFA", "BIL", risk_off_asset]
    data = {s: fetch_daily(s, "2013-06-01") for s in set(symbols)}
    closes = {s: {b["t"][:10]: b["c"] for b in data[s]} for s in set(symbols)}
    dates  = sorted(set(closes["SPY"]) & set(closes["EFA"]) &
                    set(closes["BIL"]) & set(closes[risk_off_asset]))

    series = {s: [(d, closes[s][d]) for d in dates] for s in set(symbols)}
    def score(s, idx):
        cl = [c for _, c in series[s]]
        longest = max(LOOKBACKS) * TDPM
        if idx < longest: return None
        rs = [(cl[idx]/cl[idx-m*TDPM]-1) for m in LOOKBACKS if cl[idx-m*TDPM]>0]
        return sum(rs)/len(rs) if rs else None

    equity = 10000.0; holding = None; last_month = None
    rotations = []; eq_curve = [equity]
    for idx in range(len(dates)):
        d = dates[idx]; month = d[:7]
        if holding and idx > 0:
            prev = closes[holding][dates[idx-1]]
            cur  = closes[holding][d]
            if prev > 0: equity *= cur/prev
        eq_curve.append(equity)
        if month == last_month: continue
        last_month = month
        sp, ef, bl = score("SPY", idx), score("EFA", idx), score("BIL", idx)
        if None in (sp, ef, bl): continue
        target = risk_off_asset if sp <= bl else ("SPY" if sp >= ef else "EFA")
        if target != holding:
            rotations.append((d, holding, target))
            holding = target

    first = next((r[0] for r in rotations), dates[0])
    yrs = (datetime.strptime(dates[-1], "%Y-%m-%d") -
           datetime.strptime(first, "%Y-%m-%d")).days / 365.25
    cagr = ((equity/10000)**(1/yrs)-1)*100 if yrs > 0 else 0
    peak, mdd = eq_curve[0], 0
    for e in eq_curve:
        peak = max(peak, e); mdd = max(mdd, (peak-e)/peak*100)
    monthly = []
    for i in range(TDPM, len(eq_curve), TDPM):
        if eq_curve[i-TDPM] > 0: monthly.append(eq_curve[i]/eq_curve[i-TDPM]-1)
    sh = (statistics.mean(monthly)/statistics.stdev(monthly)*math.sqrt(12)
          if len(monthly)>1 and statistics.stdev(monthly)>0 else 0)
    i0 = dates.index(first)
    spy_bh = (closes["SPY"][dates[-1]]/closes["SPY"][dates[i0]]-1)*100
    print(f"  {label}: CAGR {cagr:+.1f}% | DD {mdd:.1f}% | Sharpe {sh:.2f} | "
          f"Rotations {len(rotations)} | Final ${equity:,.0f} | SPY B&H {spy_bh:+.1f}%")
    return {"cagr": cagr, "mdd": mdd, "sharpe": sh, "equity": equity}


# ══════════════════════════════════════════════════════════════════════════════
# FIX 2: ORB — add filters (wider range, trend, volume)
# ══════════════════════════════════════════════════════════════════════════════

def orb_backtest(symbol, start, notional, config_label,
                 min_range_frac, trend_filter, vol_filter, slip=0.0005):
    by_date = fetch_5m_sym(symbol, start)
    daily   = fetch_daily(symbol, start)
    d_closes = {b["t"][:10]: b["c"] for b in daily}
    d_vols   = {b["t"][:10]: b["v"] for b in daily}
    dates_d  = [b["t"][:10] for b in daily]

    trades = []
    for d in sorted(by_date):
        ds = str(d)
        bars = sorted(by_date[d], key=lambda b: b["_et"])
        orb = next((b for b in bars
                    if b["_et"].hour == 9 and b["_et"].minute == 30), None)
        if not orb:
            continue
        o, c, hi, lo = orb["o"], orb["c"], orb["h"], orb["l"]
        rng = hi - lo
        if c == o or rng <= 0 or rng/c < min_range_frac:
            continue

        # Trend filter: only trade in direction of 20-day SMA
        if trend_filter and ds in dates_d:
            idx = dates_d.index(ds)
            if idx >= 20:
                sma20 = sum(d_closes.get(dates_d[j], c) for j in range(idx-20, idx)) / 20
                direction_wanted = "long" if c > o else "short"
                if direction_wanted == "long" and c < sma20:
                    continue
                if direction_wanted == "short" and c > sma20:
                    continue

        # Volume filter: OR volume > 1.5x the expected first-5min volume
        if vol_filter and ds in dates_d:
            idx = dates_d.index(ds)
            if idx >= 20:
                avg_vol = sum(d_vols.get(dates_d[j], 0) for j in range(idx-20, idx)) / 20
                exp_5m = avg_vol * (5/390)
                if exp_5m > 0 and orb["v"] < 1.5 * exp_5m:
                    continue

        direction = "long" if c > o else "short"
        stop = lo if direction == "long" else hi
        after = [b for b in bars if b["_et"].hour > 9 or
                 (b["_et"].hour == 9 and b["_et"].minute >= 35)]
        if not after:
            continue
        entry = after[0]["o"] * (1+slip if direction == "long" else 1-slip)
        qty = int(notional // entry)
        if qty < 1:
            continue
        exit_price, reason = None, None
        for b in after:
            if b["_et"].hour > 15 or (b["_et"].hour == 15 and b["_et"].minute >= 55):
                exit_price = b["o"]; reason = "eod"; break
            if direction == "long" and b["l"] <= stop:
                exit_price = stop*(1-slip); reason = "stop"; break
            if direction == "short" and b["h"] >= stop:
                exit_price = stop*(1+slip); reason = "stop"; break
        if exit_price is None:
            exit_price = after[-1]["c"]; reason = "eod"
        pnl = ((exit_price-entry) if direction == "long" else (entry-exit_price)) * qty
        trades.append({"entry_date": ds, "exit_date": ds, "pnl": pnl,
                       "ret_pct": pnl/(entry*qty)*100, "reason": reason,
                       "direction": direction})

    yrs = max((max(by_date)-min(by_date)).days/365.25, 0.5)
    days = sorted(by_date)
    bh = (by_date[days[-1]][-1]["c"] / by_date[days[0]][0]["o"]-1)*100
    summarize(config_label, trades, yrs, bh)


# ══════════════════════════════════════════════════════════════════════════════
# FIX 3: SIP-ORB — stop at opposite OR boundary instead of 0.10×ATR
# ══════════════════════════════════════════════════════════════════════════════

SIP_UNIVERSE_SMALL = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "AMD", "AVGO",
    "JPM", "BAC", "GS", "MS", "V", "MA",
    "UNH", "LLY", "ABBV", "MRK",
    "HD", "MCD", "NKE", "COST", "WMT",
    "XOM", "CVX", "CAT", "GE", "BA",
    "NFLX", "DIS",
    "SPY", "QQQ", "IWM", "GLD", "XLF", "XLE", "XLK",
]


def sip_orb_improved(start, use_or_stop, label):
    """
    use_or_stop=True  → stop at opposite OR boundary (the fix)
    use_or_stop=False → stop at 0.10×ATR (the bug)
    """
    print("  Fetching daily bars…")
    daily = {}
    ds = (datetime.strptime(start, "%Y-%m-%d") - timedelta(days=60)).strftime("%Y-%m-%d")
    for s in SIP_UNIVERSE_SMALL:
        daily[s] = fetch_daily(s, ds, adjustment="raw")
        time.sleep(0.1)

    print("  Fetching 5-min bars (with rate-limit backoff)…")
    intraday = fetch_multi_5m(SIP_UNIVERSE_SMALL, start)
    print(f"  Got intraday for {len(intraday)}/{len(SIP_UNIVERSE_SMALL)} symbols")

    def atr14(bars, upto):
        prior = [b for b in bars if b["t"][:10] < upto]
        if len(prior) < 15: return None
        b = prior[-15:]
        trs = [max(b[i]["h"]-b[i]["l"], abs(b[i]["h"]-b[i-1]["c"]),
                   abs(b[i]["l"]-b[i-1]["c"])) for i in range(1, len(b))]
        return sum(trs[-14:])/14

    def adv(bars, upto, lb=20):
        prior = [b for b in bars if b["t"][:10] < upto]
        if len(prior) < 5: return 0
        v = [x["v"] for x in prior[-lb:]]
        return sum(v)/len(v)

    all_dates = set()
    for s in intraday: all_dates |= set(intraday[s].keys())
    all_dates = sorted(all_dates)

    trades = []
    for d in all_dates:
        ds_str = str(d)
        cands = []
        for s in SIP_UNIVERSE_SMALL:
            if s not in intraday: continue
            bars = sorted(intraday[s].get(d, []), key=lambda b: b["_et"])
            orb = next((b for b in bars
                        if b["_et"].hour == 9 and b["_et"].minute == 30), None)
            if not orb: continue
            price = orb["c"]
            if price < 5.0: continue
            a = atr14(daily[s], ds_str)
            if a is None or a < 0.50: continue
            v = adv(daily[s], ds_str)
            if v < 500_000: continue
            exp5 = v*(5/390)
            rvol = orb["v"]/exp5 if exp5 > 0 else 0
            if rvol < 1.5: continue
            rng = orb["h"]-orb["l"]
            if rng < 0.01 or orb["c"] == orb["o"]: continue
            direction = "long" if orb["c"] > orb["o"] else "short"
            trigger = orb["h"] if direction == "long" else orb["l"]
            or_stop = orb["l"] if direction == "long" else orb["h"]
            atr_stop = (price - 0.10*a if direction == "long" else price + 0.10*a)
            cands.append({"sym": s, "dir": direction, "trigger": trigger,
                          "or_stop": or_stop, "atr_stop": atr_stop,
                          "atr": a, "rvol": rvol, "bars": bars, "price": price})
        cands.sort(key=lambda x: x["rvol"], reverse=True)
        for c in cands[:10]:
            s = c["sym"]
            after = [b for b in c["bars"]
                     if b["_et"].hour > 9 or (b["_et"].hour == 9 and b["_et"].minute >= 35)]
            entry = None
            for j, b in enumerate(after):
                if c["dir"] == "long" and b["h"] >= c["trigger"]:
                    entry = c["trigger"]*1.003; eidx = j; break
                if c["dir"] == "short" and b["l"] <= c["trigger"]:
                    entry = c["trigger"]*0.997; eidx = j; break
            if entry is None: continue
            qty = int(1500 // entry)
            if qty < 1: continue
            stop = c["or_stop"] if use_or_stop else c["atr_stop"]
            slip = 0.0005
            exit_price, reason = None, None
            for b in after[eidx:]:
                if b["_et"].hour > 15 or (b["_et"].hour == 15 and b["_et"].minute >= 55):
                    exit_price = b["o"]; reason = "eod"; break
                if c["dir"] == "long" and b["l"] <= stop:
                    exit_price = stop*(1-slip); reason = "stop"; break
                if c["dir"] == "short" and b["h"] >= stop:
                    exit_price = stop*(1+slip); reason = "stop"; break
            if exit_price is None:
                exit_price = after[-1]["c"]; reason = "eod"
            pnl = ((exit_price-entry) if c["dir"] == "long" else (entry-exit_price))*qty
            trades.append({"entry_date": ds_str, "exit_date": ds_str, "symbol": s,
                           "direction": c["dir"], "entry": entry, "exit": exit_price,
                           "pnl": pnl, "ret_pct": pnl/(entry*qty)*100, "reason": reason,
                           "rvol": c["rvol"]})

    yrs = max((all_dates[-1]-all_dates[0]).days/365.25, 0.25)
    summarize(label, trades, yrs)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("\n" + "="*68)
    print("FIX 1: DUAL MOMENTUM — AGG vs BIL as risk-off asset")
    print("="*68)
    print("  Original (AGG risk-off):")
    r_agg = dm_backtest("AGG", "    DM + AGG risk-off")
    print("  Improved (BIL risk-off — avoids bond duration risk):")
    r_bil = dm_backtest("BIL", "    DM + BIL risk-off")

    print("\n" + "="*68)
    print("FIX 2: ORB — adding filters (progressive)")
    print("="*68)
    print("  v1 baseline (current, no filters):")
    orb_backtest("QQQ", "2023-01-01", 2000, "    ORB baseline",
                 min_range_frac=0.0008, trend_filter=False, vol_filter=False)
    print("  v2 wider range filter (>0.3%):")
    orb_backtest("QQQ", "2023-01-01", 2000, "    ORB + range>0.3%",
                 min_range_frac=0.003, trend_filter=False, vol_filter=False)
    print("  v3 range + trend alignment:")
    orb_backtest("QQQ", "2023-01-01", 2000, "    ORB + range + trend",
                 min_range_frac=0.003, trend_filter=True, vol_filter=False)
    print("  v4 range + trend + volume confirmation:")
    orb_backtest("QQQ", "2023-01-01", 2000, "    ORB + range + trend + vol",
                 min_range_frac=0.003, trend_filter=True, vol_filter=True)

    print("\n" + "="*68)
    print("FIX 3: SIP-ORB — OR-boundary stop vs 0.10×ATR stop")
    print("="*68)
    sip_orb_improved("2025-01-01", use_or_stop=False, label="    SIP-ORB (bug: 0.10*ATR stop)")
    sip_orb_improved("2025-01-01", use_or_stop=True,  label="    SIP-ORB (fix: OR-boundary stop)")

    print("\n" + "="*68)
    print("DONE — apply winning configs to live bots")
    print("="*68)
