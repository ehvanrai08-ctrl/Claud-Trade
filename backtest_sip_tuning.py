"""
SIP-ORB tuning — the real lever is SELECTIVITY. The paper's edge comes from
isolating genuine catalyst stocks (extreme relative volume). Taking the top 10
each day dilutes that; taking only the top 1-3 with a high RVol bar may surface
the actual edge. Sweep selectivity, RVol threshold, and OR-range minimum.

Honest goal: find a config with PF > 1, or show it can't be reached unleveraged.
Run: python backtest_sip_tuning.py
"""

import os
import math
import statistics
import time
from collections import defaultdict
from datetime import datetime, timedelta
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

UNIVERSE = [
    "AAPL","MSFT","NVDA","AMZN","META","GOOGL","AMD","AVGO","JPM","BAC","GS","MS",
    "V","MA","UNH","LLY","ABBV","MRK","HD","MCD","NKE","COST","WMT","XOM","CVX",
    "CAT","GE","BA","NFLX","DIS","SPY","QQQ","IWM","GLD","XLF","XLE","XLK",
]
START = "2024-06-01"
NOTIONAL = 1500


def fetch_daily(symbol, start):
    r = requests.get(f"https://data.alpaca.markets/v2/stocks/{symbol}/bars",
                     headers=DATA_HEADERS,
                     params={"timeframe": "1Day", "start": f"{start}T00:00:00Z",
                             "limit": 10000, "sort": "asc", "adjustment": "raw"}, timeout=30)
    return r.json().get("bars") or [] if r.ok else []


def fetch_5m(symbol, start):
    bars, token = [], None
    while True:
        params = {"timeframe": "5Min", "start": f"{start}T00:00:00Z",
                  "limit": 10000, "sort": "asc", "adjustment": "raw"}
        if token:
            params["page_token"] = token
        for attempt in range(4):
            r = requests.get(f"https://data.alpaca.markets/v2/stocks/{symbol}/bars",
                             headers=DATA_HEADERS, params=params, timeout=45)
            if r.status_code == 429:
                time.sleep(2 ** attempt); continue
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
        t = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(ET)
        b["_et"] = t
        by_date[t.date()].append(b)
    return by_date


def atr14(bars, upto):
    prior = [b for b in bars if b["t"][:10] < upto]
    if len(prior) < 15:
        return None
    b = prior[-15:]
    trs = [max(b[i]["h"]-b[i]["l"], abs(b[i]["h"]-b[i-1]["c"]), abs(b[i]["l"]-b[i-1]["c"]))
           for i in range(1, len(b))]
    return sum(trs[-14:])/14


def adv(bars, upto, lb=20):
    prior = [b for b in bars if b["t"][:10] < upto]
    if len(prior) < 5:
        return 0
    v = [x["v"] for x in prior[-lb:]]
    return sum(v)/len(v)


def report(name, trades, years):
    if not trades:
        print(f"  {name:34} no trades"); return
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
    print(f"  {name:34} n={len(trades):4} WR={wr:4.1f}% PF={pf:4.2f} "
          f"Sharpe={sharpe:5.2f} ${total:+7.0f}{flag}")


def run_sweep(daily, intraday, max_pos, min_rvol, min_range_frac, use_tight_stop):
    all_dates = sorted({d for s in intraday for d in intraday[s]})
    trades = []
    for d in all_dates:
        ds = str(d)
        cands = []
        for s in UNIVERSE:
            if s not in intraday:
                continue
            bars = sorted(intraday[s].get(d, []), key=lambda b: b["_et"])
            orb = next((b for b in bars if b["_et"].hour == 9 and b["_et"].minute == 30), None)
            if not orb:
                continue
            price = orb["c"]
            if price < 5:
                continue
            a = atr14(daily[s], ds)
            if a is None or a < 0.50:
                continue
            v = adv(daily[s], ds)
            if v < 500_000:
                continue
            exp5 = v*(5/390)
            rvol = orb["v"]/exp5 if exp5 > 0 else 0
            if rvol < min_rvol:
                continue
            rng = orb["h"]-orb["l"]
            if rng < 0.01 or orb["c"] == orb["o"] or rng/price < min_range_frac:
                continue
            direction = "long" if orb["c"] > orb["o"] else "short"
            trigger = orb["h"] if direction == "long" else orb["l"]
            or_stop = orb["l"] if direction == "long" else orb["h"]
            cands.append({"s": s, "dir": direction, "trigger": trigger, "or_stop": or_stop,
                          "atr": a, "rvol": rvol, "bars": bars})
        cands.sort(key=lambda x: x["rvol"], reverse=True)
        for c in cands[:max_pos]:
            after = [b for b in c["bars"] if b["_et"].hour > 9 or
                     (b["_et"].hour == 9 and b["_et"].minute >= 35)]
            entry, eidx = None, None
            for j, b in enumerate(after):
                if c["dir"] == "long" and b["h"] >= c["trigger"]:
                    entry = c["trigger"]*1.003; eidx = j; break
                if c["dir"] == "short" and b["l"] <= c["trigger"]:
                    entry = c["trigger"]*0.997; eidx = j; break
            if entry is None:
                continue
            qty = int(NOTIONAL // entry)
            if qty < 1:
                continue
            stop = (entry - 0.10*c["atr"] if c["dir"] == "long" else entry + 0.10*c["atr"]) \
                if use_tight_stop else c["or_stop"]
            exit_p = None
            for b in after[eidx:]:
                if b["_et"].hour > 15 or (b["_et"].hour == 15 and b["_et"].minute >= 55):
                    exit_p = b["o"]; break
                if c["dir"] == "long" and b["l"] <= stop:
                    exit_p = stop*0.9995; break
                if c["dir"] == "short" and b["h"] >= stop:
                    exit_p = stop*1.0005; break
            if exit_p is None:
                exit_p = after[-1]["c"]
            pnl = ((exit_p-entry) if c["dir"] == "long" else (entry-exit_p))*qty
            trades.append({"pnl": pnl, "ret": pnl/(entry*qty)*100})
    return trades


if __name__ == "__main__":
    print(f"Fetching daily + 5-min for {len(UNIVERSE)} symbols since {START} (rate-limited)…")
    daily = {s: fetch_daily(s, (datetime.strptime(START, "%Y-%m-%d")-timedelta(days=60)).strftime("%Y-%m-%d"))
             for s in UNIVERSE}
    intraday = {}
    for s in UNIVERSE:
        bd = fetch_5m(s, START)
        if bd:
            intraday[s] = bd
        time.sleep(0.2)
    days = sorted({d for s in intraday for d in intraday[s]})
    years = max((days[-1]-days[0]).days/365.25, 0.25)
    print(f"Got {len(intraday)} symbols, {len(days)} days (~{years:.1f}y)\n")

    print("Baseline (top 10, RVol>1.5, OR-boundary stop):")
    report("top10 rvol1.5 OR-stop", run_sweep(daily, intraday, 10, 1.5, 0.0, False), years)

    print("\nSelectivity sweep (OR-boundary stop, RVol>1.5):")
    for mp in (1, 2, 3, 5):
        report(f"top{mp} rvol1.5", run_sweep(daily, intraday, mp, 1.5, 0.0, False), years)

    print("\nHigher RVol threshold (top 3):")
    for rv in (2.0, 3.0, 4.0):
        report(f"top3 rvol{rv}", run_sweep(daily, intraday, 3, rv, 0.0, False), years)

    print("\nAdd OR-range minimum (top 3, RVol>2):")
    for rf in (0.005, 0.01):
        report(f"top3 rvol2 range>{rf}", run_sweep(daily, intraday, 3, 2.0, rf, False), years)

    print("\nTight 0.10xATR stop variants (for comparison):")
    report("top3 rvol3 tightstop", run_sweep(daily, intraday, 3, 3.0, 0.0, True), years)
    report("top1 rvol3 tightstop", run_sweep(daily, intraday, 1, 3.0, 0.0, True), years)
