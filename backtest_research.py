"""
Backtest of researched strategies — pulled from academic/Quantpedia/practitioner
sources via deep research, then tested HERE on real Alpaca daily data with the
same discipline as the rest of the repo: in/out-of-sample where meaningful, and
every strategy benchmarked against SPY buy-and-hold (the SPIVA question — does it
actually beat just owning the index?).

The research returned CLAIMS. This file is the arbiter. We trust nothing until it
survives on our data. Strategies tested (all distinct from the live bots):

  1. Double 7's        — SPY/QQQ: buy when close>200SMA AND close=7-day low;
                          sell when close=7-day high. (Connors/Hill)
  2. Turn-of-the-Month — SPY/QQQ: long from -1 trading day before month-end
                          through the 3rd trading day of the new month.
  3. Overnight edge    — SPY/QQQ: hold close→open (overnight) vs open→close
                          (intraday) vs buy&hold. (overnight-return anomaly)
  4. Sector momentum   — top 3 of 11 sector SPDRs by N-month momentum, monthly.
  5. Faber rotation    — top 3 sector SPDRs by 3-mo return, only when SPY>10-mo
                          SMA, else cash. (Faber)

Alpaca daily history starts ~2016, so monthly-rebalance strategies get ~9 years
(~110 months) — a directional read, not a 1928-2009 study. Noted in output.

Run: python backtest_research.py
"""

import os
import math
import statistics
import time
import requests
from dotenv import dotenv_values

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
config   = dotenv_values(f"{BASE_DIR}/.env")
DATA_HEADERS = {
    "APCA-API-KEY-ID":     config.get("ALPACA_API_KEY", ""),
    "APCA-API-SECRET-KEY": config.get("ALPACA_SECRET_KEY", ""),
}
START = "2016-01-01"
SLIP  = 0.0005   # 5 bps round-trip-ish per side on entries/exits


# ── Data ──────────────────────────────────────────────────────────────────────

def fetch_daily(symbol, start=START):
    """All daily bars since `start` (ascending). Paginated, SSL-retry hardened."""
    bars, token = [], None
    while True:
        params = {"timeframe": "1Day", "start": f"{start}T00:00:00Z",
                  "limit": 10000, "sort": "asc", "adjustment": "all"}
        if token:
            params["page_token"] = token
        r = None
        for attempt in range(5):
            try:
                r = requests.get(f"https://data.alpaca.markets/v2/stocks/{symbol}/bars",
                                 headers=DATA_HEADERS, params=params, timeout=45)
            except requests.exceptions.RequestException:
                time.sleep(2 ** attempt); continue
            if r.status_code == 429:
                time.sleep(2 ** attempt); continue
            break
        if r is None or not r.ok:
            break
        j = r.json()
        bars.extend(j.get("bars") or [])
        token = j.get("next_page_token")
        if not token:
            break
    for b in bars:
        b["d"] = b["t"][:10]
    return bars


# ── Metrics ───────────────────────────────────────────────────────────────────

def perf_from_daily_returns(rets, periods_per_year=252):
    """rets = list of per-period strategy returns (fraction). Returns dict."""
    if not rets:
        return None
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        eq *= (1 + r); peak = max(peak, eq); mdd = max(mdd, (peak - eq) / peak)
    total = eq - 1
    yrs = len(rets) / periods_per_year
    cagr = (eq ** (1 / yrs) - 1) if yrs > 0 else 0
    sd = statistics.stdev(rets) if len(rets) > 1 else 0
    sharpe = (statistics.mean(rets) / sd * math.sqrt(periods_per_year)) if sd > 0 else 0
    return {"total": total * 100, "cagr": cagr * 100, "sharpe": sharpe,
            "mdd": mdd * 100, "n": len(rets)}


def show(name, m, bench=None):
    if not m:
        print(f"  {name:30} no data"); return
    line = (f"  {name:30} CAGR={m['cagr']:6.2f}%  Sharpe={m['sharpe']:5.2f}  "
            f"maxDD={m['mdd']:5.1f}%  total={m['total']:+7.1f}%  n={m['n']}")
    if bench is not None and m and bench:
        alpha = m['cagr'] - bench['cagr']
        line += f"  | vs SPY: {alpha:+.2f}%/yr {'✓' if alpha>0 else '✗'}"
    print(line)


# ── Strategy 1: Double 7's ────────────────────────────────────────────────────

def double7(bars):
    """Long when close>200SMA and close=min(7 closes); exit when close=max(7).
    Returns a per-day return series (0 when flat)."""
    closes = [b["c"] for b in bars]
    rets, holding = [], False
    for i in range(len(bars)):
        # today's return is realized if we were holding from yesterday's signal
        r = 0.0
        if holding and i > 0:
            r = closes[i] / closes[i-1] - 1
        rets.append(r)
        if i < 200:
            continue
        sma200 = sum(closes[i-199:i+1]) / 200
        last7  = closes[i-6:i+1]
        if not holding and closes[i] > sma200 and closes[i] == min(last7):
            holding = True
        elif holding and closes[i] == max(last7):
            holding = False
    return rets


# ── Strategy 2: Turn-of-the-Month ─────────────────────────────────────────────

def turn_of_month(bars, days_before=1, days_after=3):
    """Long from `days_before` trading days before month-end through the
    `days_after`-th trading day of the next month; flat otherwise."""
    from collections import defaultdict
    by_month = defaultdict(list)
    for idx, b in enumerate(bars):
        by_month[b["d"][:7]].append(idx)
    months = sorted(by_month)
    in_window = set()
    for mi, m in enumerate(months):
        idxs = by_month[m]
        for j in idxs[-days_before:]:
            in_window.add(j)
        if mi + 1 < len(months):
            nxt = by_month[months[mi+1]]
            for j in nxt[:days_after]:
                in_window.add(j)
    closes = [b["c"] for b in bars]
    rets = []
    for i in range(len(bars)):
        r = 0.0
        if i > 0 and (i-1) in in_window:   # held overnight from a window day
            r = closes[i] / closes[i-1] - 1
        rets.append(r)
    return rets


# ── Strategy 3: Overnight vs intraday ─────────────────────────────────────────

def overnight_intraday(bars):
    """Three series: overnight (prev close→open), intraday (open→close), buyhold."""
    overnight, intraday, buyhold = [], [], []
    for i in range(1, len(bars)):
        po = bars[i-1]["c"]; o = bars[i]["o"]; c = bars[i]["c"]
        overnight.append(o / po - 1)
        intraday.append(c / o - 1)
        buyhold.append(c / bars[i-1]["c"] - 1)
    return overnight, intraday, buyhold


# ── Strategies 4 & 5: monthly rotation ────────────────────────────────────────

def month_end_indices(bars):
    from collections import defaultdict
    by_month = defaultdict(list)
    for idx, b in enumerate(bars):
        by_month[b["d"][:7]].append(idx)
    return [by_month[m][-1] for m in sorted(by_month)]   # last trading day each month


def rotation(symbol_bars, lookback_days=252, top_n=3, regime_sym=None, regime_sma_months=10):
    """Generic monthly cross-sectional momentum rotation.
    symbol_bars: dict sym->bars (all same length/dates assumed aligned by index).
    Holds top_n by trailing `lookback_days` return, equal weight, rebalanced
    monthly. If regime_sym given, only invest when regime_sym close>its 10-mo SMA,
    else cash. Returns monthly strategy-return series."""
    syms = list(symbol_bars)
    ref  = symbol_bars[syms[0]]
    me   = month_end_indices(ref)
    regime_closes = [b["c"] for b in symbol_bars[regime_sym]] if regime_sym else None
    monthly = []
    for k in range(len(me) - 1):
        i, j = me[k], me[k+1]
        if i < lookback_days:
            continue
        # regime gate (10-month SMA ≈ 210 trading days)
        if regime_closes is not None:
            sma = sum(regime_closes[max(0,i-209):i+1]) / min(210, i+1)
            if regime_closes[i] <= sma:
                monthly.append(0.0); continue   # cash
        moms = []
        for s in syms:
            cb = symbol_bars[s]
            if i < lookback_days or j >= len(cb):
                continue
            moms.append((s, cb[i]["c"] / cb[i-lookback_days]["c"] - 1))
        if not moms:
            monthly.append(0.0); continue
        moms.sort(key=lambda x: x[1], reverse=True)
        picks = [s for s, _ in moms[:top_n]]
        # next-month return, equal weight, minus slippage on rebalance
        rs = [symbol_bars[s][j]["c"] / symbol_bars[s][i]["c"] - 1 for s in picks]
        monthly.append(sum(rs) / len(rs) - SLIP)
    return monthly


# ── Main ──────────────────────────────────────────────────────────────────────

SECTORS = ["XLK","XLF","XLE","XLV","XLI","XLP","XLY","XLU","XLB","XLRE","XLC"]
ASSETS  = ["SPY","EFA","BND","VNQ","GSG","GLD","AGG","IWM"]

if __name__ == "__main__":
    print(f"Fetching daily bars since {START}…")
    need = sorted(set(["SPY","QQQ"] + SECTORS + ASSETS))
    data = {}
    for s in need:
        b = fetch_daily(s)
        if b:
            data[s] = b
        time.sleep(0.1)
    spy = data["SPY"]
    print(f"Got {len(data)} symbols; SPY has {len(spy)} daily bars "
          f"({spy[0]['d']} → {spy[-1]['d']}).\n")

    # Benchmark: SPY buy-and-hold daily returns.
    spy_bh = [spy[i]["c"]/spy[i-1]["c"]-1 for i in range(1, len(spy))]
    bench = perf_from_daily_returns(spy_bh)
    show("SPY buy & hold (benchmark)", bench)
    print()

    print("── 1. Double 7's (daily, long-flat) ──")
    for s in ("SPY", "QQQ"):
        show(f"Double7 {s}", perf_from_daily_returns(double7(data[s])), bench)

    print("\n── 2. Turn-of-the-Month (long window only) ──")
    for s in ("SPY", "QQQ"):
        for db, da in ((1,3),(4,3)):
            show(f"TOM {s} (-{db}/+{da})",
                 perf_from_daily_returns(turn_of_month(data[s], db, da)), bench)

    print("\n── 3. Overnight vs Intraday (SPY/QQQ) ──")
    for s in ("SPY", "QQQ"):
        ov, intr, bh = overnight_intraday(data[s])
        show(f"{s} overnight (close→open)", perf_from_daily_returns(ov), bench)
        show(f"{s} intraday (open→close)", perf_from_daily_returns(intr), bench)

    print("\n── 4. Sector momentum rotation (monthly, top 3 of 11) ──")
    sect = {s: data[s] for s in SECTORS if s in data}
    # align lengths to the shortest (XLRE/XLC listed later) by trimming front
    minlen = min(len(b) for b in sect.values())
    sect = {s: b[-minlen:] for s, b in sect.items()}
    for lb in (126, 252):
        m = rotation(sect, lookback_days=lb, top_n=3)
        show(f"Sector mom {lb}d top3", perf_from_daily_returns(m, 12), bench)

    print("\n── 5. Faber sector rotation (top3 3-mo, SPY>10mo SMA gate) ──")
    sectspy = dict(sect); sectspy["SPY"] = data["SPY"][-minlen:]
    m = rotation({s: b for s, b in sectspy.items() if s != "SPY"} | {"SPY": sectspy["SPY"]},
                 lookback_days=63, top_n=3, regime_sym="SPY")
    show("Faber 63d top3 +regime", perf_from_daily_returns(m, 12), bench)

    print("\n── 6. Asset-class momentum rotation (monthly, top 3) ──")
    asset = {s: data[s] for s in ASSETS if s in data}
    minlen2 = min(len(b) for b in asset.values())
    asset = {s: b[-minlen2:] for s, b in asset.items()}
    for lb in (126, 252):
        m = rotation(asset, lookback_days=lb, top_n=3)
        show(f"Asset mom {lb}d top3", perf_from_daily_returns(m, 12), bench)

    print("\n── 7. Sector-momentum ROBUSTNESS (is 252d/top3 a fluke?) ──")
    print("   lookback × top_n grid — want the edge stable across neighbors, not one cell:")
    for lb in (189, 210, 231, 252):
        row = []
        for tn in (2, 3, 4):
            m = perf_from_daily_returns(rotation(sect, lookback_days=lb, top_n=tn), 12)
            row.append(f"top{tn}: CAGR={m['cagr']:5.1f}% Sh={m['sharpe']:.2f}")
        print(f"   {lb}d  | " + "  ".join(row))

    print("\n   Out-of-sample split (252d top3): first half IS vs second half OOS:")
    monthly = rotation(sect, lookback_days=252, top_n=3)
    half = len(monthly) // 2
    mis = perf_from_daily_returns(monthly[:half], 12)
    moos = perf_from_daily_returns(monthly[half:], 12)
    # SPY benchmark over the same monthly windows
    me = month_end_indices(sect[list(sect)[0]])
    spy_m = []
    spyb = data["SPY"][-minlen:]
    for k in range(len(me)-1):
        if me[k] < 252: continue
        spy_m.append(spyb[me[k+1]]["c"]/spyb[me[k]]["c"]-1)
    sis = perf_from_daily_returns(spy_m[:half], 12)
    soos = perf_from_daily_returns(spy_m[half:], 12)
    show("  IS  sector252 top3", mis); show("  IS  SPY buy&hold", sis)
    show("  OOS sector252 top3", moos); show("  OOS SPY buy&hold", soos)

    print("\nBenchmark column: does it beat SPY buy & hold (SPIVA)? "
          "✓ = positive annual alpha. Monthly strategies have ~9y of data only.")
