"""
Backtest the live bots against real Alpaca history — faithful to each bot's
actual rules — so we can see which strategies actually carry their weight
before any real money is involved.

Covers:
  1. Dual Momentum (GEM)  — daily, monthly rebalance, SPY/EFA/BIL/AGG ensemble
  2. ORB (QQQ)            — intraday 5-min opening-range breakout, EOD close
  3. SIP-ORB              — multi-stock Stocks-in-Play ORB (heavier; subset window)

No look-ahead: every signal uses only data available at decision time.
Run:  python backtest_bots.py [dm|orb|sip|all]
"""

import os
import sys
import math
import statistics
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


# ── Data fetchers ─────────────────────────────────────────────────────────────

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
            print(f"  daily error {symbol}: {r.status_code} {r.text[:120]}")
            break
        j = r.json()
        bars.extend(j.get("bars") or [])
        token = j.get("next_page_token")
        if not token:
            break
    return bars


def fetch_5m(symbol, start, end=None, adjustment="raw"):
    """5-min bars between start and end (ISO dates), grouped by ET date."""
    bars, token = [], None
    while True:
        params = {"timeframe": "5Min", "start": f"{start}T00:00:00Z",
                  "limit": 10000, "sort": "asc", "adjustment": adjustment}
        if end:
            params["end"] = f"{end}T23:59:59Z"
        if token:
            params["page_token"] = token
        r = requests.get(f"https://data.alpaca.markets/v2/stocks/{symbol}/bars",
                         headers=DATA_HEADERS, params=params, timeout=45)
        if not r.ok:
            print(f"  5m error {symbol}: {r.status_code} {r.text[:120]}")
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


# ── Generic metrics ───────────────────────────────────────────────────────────

def trade_metrics(trades, label, years, bh_ret=None, bh_label=""):
    if not trades:
        return f"{label}: no trades."
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total  = sum(t["pnl"] for t in trades)
    gw     = sum(t["pnl"] for t in wins)
    gl     = sum(t["pnl"] for t in losses)
    pf     = abs(gw / gl) if gl != 0 else float("inf")
    wr     = len(wins) / len(trades) * 100
    rets   = [t.get("ret_pct", 0) for t in trades]
    avg_r  = sum(rets) / len(rets)
    if len(rets) > 1 and statistics.stdev(rets) > 0:
        tpy = len(trades) / years if years else len(trades)
        sharpe = (avg_r / statistics.stdev(rets)) * math.sqrt(tpy)
    else:
        sharpe = 0
    # closed-trade equity drawdown
    eq, peak, mdd = 0, 0, 0
    for t in sorted(trades, key=lambda x: x["exit_date"]):
        eq += t["pnl"]; peak = max(peak, eq)
        if peak > 0:
            mdd = max(mdd, (peak - eq) / (peak if peak else 1) * 100)
    out = [
        f"{label}",
        f"  Trades: {len(trades)} ({len(trades)/years:.0f}/yr) over {years:.1f}y",
        f"  Win rate: {wr:.1f}%   Profit factor: {pf:.2f}   Avg trade: {avg_r:+.2f}%",
        f"  Total P&L: ${total:+,.2f}   Per-trade Sharpe: {sharpe:.2f}",
    ]
    if bh_ret is not None:
        out.append(f"  Buy-and-hold {bh_label}: {bh_ret:+.1f}%")
    return "\n".join(out)


# ── 1. Dual Momentum (GEM) ────────────────────────────────────────────────────

def backtest_dual_momentum():
    print("\n=== DUAL MOMENTUM (GEM) ===")
    LOOKBACKS = [6, 7, 8, 9, 10, 11, 12]
    TDPM = 21
    symbols = ["SPY", "EFA", "BIL", "AGG"]
    data = {s: fetch_daily(s, "2013-06-01") for s in symbols}
    for s in symbols:
        print(f"  {s}: {len(data[s])} daily bars")

    # align by date
    closes = {s: {b["t"][:10]: b["c"] for b in data[s]} for s in symbols}
    dates  = sorted(set(closes["SPY"]) & set(closes["EFA"]) &
                    set(closes["BIL"]) & set(closes["AGG"]))
    # series of (date, close) per symbol over the common dates
    series = {s: [(d, closes[s][d]) for d in dates] for s in symbols}

    def score(s, idx):
        cl = [c for _, c in series[s]]
        longest = max(LOOKBACKS) * TDPM
        if idx < longest:
            return None
        now = cl[idx]
        rs = []
        for m in LOOKBACKS:
            past = cl[idx - m * TDPM]
            if past > 0:
                rs.append(now / past - 1)
        return sum(rs) / len(rs) if rs else None

    # walk monthly: act on first trading day of each month
    equity = 10000.0
    eq_curve = [equity]
    holding = None
    last_month = None
    rotations = []
    spy_cl = [c for _, c in series["SPY"]]

    for idx in range(len(dates)):
        d = dates[idx]
        month = d[:7]
        if month == last_month:
            # accrue daily return of held asset
            if holding and idx > 0:
                prev_c = closes[holding][dates[idx-1]]
                cur_c  = closes[holding][d]
                if prev_c > 0:
                    equity *= cur_c / prev_c
            eq_curve.append(equity)
            continue
        last_month = month
        # accrue today's move first (held into new month open)
        if holding and idx > 0:
            prev_c = closes[holding][dates[idx-1]]
            cur_c  = closes[holding][d]
            if prev_c > 0:
                equity *= cur_c / prev_c
        eq_curve.append(equity)

        sp, ef, bl = score("SPY", idx), score("EFA", idx), score("BIL", idx)
        if None in (sp, ef, bl):
            continue
        if sp <= bl:
            target = "AGG"
        elif sp >= ef:
            target = "SPY"
        else:
            target = "EFA"
        if target != holding:
            rotations.append((d, holding, target, sp, ef, bl))
            holding = target

    # metrics
    first_act = next((r[0] for r in rotations), dates[0])
    yrs = (datetime.strptime(dates[-1], "%Y-%m-%d") -
           datetime.strptime(first_act, "%Y-%m-%d")).days / 365.25
    total_ret = equity / 10000 * 100 - 100
    cagr = ((equity / 10000) ** (1 / yrs) - 1) * 100 if yrs > 0 else 0
    # max drawdown on equity curve
    peak, mdd = eq_curve[0], 0
    for e in eq_curve:
        peak = max(peak, e)
        mdd = max(mdd, (peak - e) / peak * 100)
    # monthly Sharpe
    monthly = []
    for i in range(TDPM, len(eq_curve), TDPM):
        if eq_curve[i-TDPM] > 0:
            monthly.append(eq_curve[i] / eq_curve[i-TDPM] - 1)
    if len(monthly) > 1 and statistics.stdev(monthly) > 0:
        sharpe = (statistics.mean(monthly) / statistics.stdev(monthly)) * math.sqrt(12)
    else:
        sharpe = 0

    # SPY buy-hold over same window
    i0 = dates.index(first_act)
    spy_bh = (spy_cl[-1] / spy_cl[i0] - 1) * 100

    print(f"  Window: {first_act} → {dates[-1]} ({yrs:.1f}y)")
    print(f"  Rotations: {len(rotations)} ({len(rotations)/yrs:.1f}/yr)")
    print(f"  Final equity: ${equity:,.0f}  (total {total_ret:+.1f}%)")
    print(f"  CAGR: {cagr:+.1f}%   Max drawdown: {mdd:.1f}%   Sharpe: {sharpe:.2f}")
    print(f"  SPY buy-and-hold same window: {spy_bh:+.1f}%")
    print("  Recent rotations:")
    for d, frm, to, sp, ef, bl in rotations[-6:]:
        print(f"    {d}: {frm}→{to}  (SPY {sp:+.1%} EFA {ef:+.1%} BIL {bl:+.1%})")
    return {"cagr": cagr, "mdd": mdd, "sharpe": sharpe, "total": total_ret,
            "spy_bh": spy_bh, "rotations": len(rotations)}


# ── 2. ORB on QQQ ─────────────────────────────────────────────────────────────

def backtest_orb(symbol="QQQ", start="2023-01-01",
                 notional=2000, min_range_frac=0.0008, slip=0.0005):
    print(f"\n=== ORB ({symbol}) ===")
    by_date = fetch_5m(symbol, start)
    print(f"  {len(by_date)} trading days of 5-min bars")
    trades = []
    for d in sorted(by_date):
        bars = sorted(by_date[d], key=lambda b: b["_et"])
        orb = next((b for b in bars if b["_et"].hour == 9 and b["_et"].minute == 30), None)
        if not orb:
            continue
        o, c, hi, lo = orb["o"], orb["c"], orb["h"], orb["l"]
        rng = hi - lo
        if c == o or rng <= 0 or rng / c < min_range_frac:
            continue
        direction = "long" if c > o else "short"
        stop = lo if direction == "long" else hi
        # entry on the 9:35 bar open
        after = [b for b in bars if b["_et"].hour > 9 or
                 (b["_et"].hour == 9 and b["_et"].minute >= 35)]
        if not after:
            continue
        entry = after[0]["o"] * (1 + slip if direction == "long" else 1 - slip)
        qty = int(notional // entry)
        if qty < 1:
            continue
        # walk to EOD (3:55) checking stop
        exit_price, reason = None, None
        for b in after:
            if b["_et"].hour > 15 or (b["_et"].hour == 15 and b["_et"].minute >= 55):
                exit_price = b["o"]; reason = "eod"; break
            if direction == "long" and b["l"] <= stop:
                exit_price = stop * (1 - slip); reason = "stop"; break
            if direction == "short" and b["h"] >= stop:
                exit_price = stop * (1 + slip); reason = "stop"; break
        if exit_price is None:
            exit_price = after[-1]["c"]; reason = "eod"
        pnl = ((exit_price - entry) if direction == "long" else (entry - exit_price)) * qty
        ret = pnl / (entry * qty) * 100
        trades.append({"entry_date": str(d), "exit_date": str(d), "direction": direction,
                       "entry": entry, "exit": exit_price, "pnl": pnl,
                       "ret_pct": ret, "reason": reason})
    yrs = max((max(by_date) - min(by_date)).days / 365.25, 0.5)
    # QQQ buy-hold
    allc = sorted(by_date)
    bh = (by_date[allc[-1]][-1]["c"] / by_date[allc[0]][0]["o"] - 1) * 100
    print(trade_metrics(trades, f"  ORB {symbol}", yrs, bh, symbol))
    stops = sum(1 for t in trades if t["reason"] == "stop")
    print(f"  Stop exits: {stops}/{len(trades)} ({stops/len(trades)*100:.0f}%) | "
          f"longs {sum(1 for t in trades if t['direction']=='long')} "
          f"shorts {sum(1 for t in trades if t['direction']=='short')}")
    return {"trades": trades, "years": yrs, "bh": bh}


# ── 3. SIP-ORB (heavier, subset window) ───────────────────────────────────────

SIP_UNIVERSE = [
    "AAPL","MSFT","NVDA","AMZN","META","GOOGL","AMD","AVGO","QCOM","TXN","MU",
    "INTC","AMAT","LRCX","CRM","ORCL","NOW","ADBE","INTU","PANW","CRWD","PLTR",
    "MRVL","JPM","BAC","WFC","GS","MS","C","BLK","SCHW","AXP","V","MA","UNH",
    "LLY","ABBV","MRK","PFE","ABT","TMO","AMGN","GILD","VRTX","REGN","HD","MCD",
    "NKE","SBUX","TGT","COST","WMT","LOW","XOM","CVX","COP","EOG","CAT","DE",
    "HON","GE","BA","UNP","RTX","LMT","NOC","NFLX","DIS","CMCSA","NEM","FCX",
    "AMT","EQIX","SPY","QQQ","IWM","GLD","XLF","XLE","XLK","XLV","XLP","XLI",
]


def backtest_sip_orb(start="2025-01-01", max_positions=10, notional=1500,
                     min_rvol=1.5, min_price=5.0, min_atr=0.50, min_avg_vol=500_000,
                     atr_stop_mult=0.10, slip=0.0005):
    print(f"\n=== SIP-ORB (universe {len(SIP_UNIVERSE)}, since {start}) ===")
    # daily bars for ATR/ADV
    daily = {}
    for s in SIP_UNIVERSE:
        daily[s] = fetch_daily(s, (datetime.strptime(start, "%Y-%m-%d") -
                                   timedelta(days=60)).strftime("%Y-%m-%d"),
                               adjustment="raw")
    print(f"  daily bars fetched for {sum(1 for s in daily if daily[s])} symbols")
    # 5-min bars per symbol
    intraday = {}
    for i, s in enumerate(SIP_UNIVERSE):
        intraday[s] = fetch_5m(s, start)
    print(f"  5-min bars fetched for {len(intraday)} symbols")

    def atr14(bars, upto_date):
        prior = [b for b in bars if b["t"][:10] < upto_date]
        if len(prior) < 15:
            return None
        b = prior[-15:]
        trs = [max(b[i]["h"]-b[i]["l"], abs(b[i]["h"]-b[i-1]["c"]),
                   abs(b[i]["l"]-b[i-1]["c"])) for i in range(1, len(b))]
        return sum(trs[-14:]) / 14

    def adv(bars, upto_date, lb=20):
        prior = [b for b in bars if b["t"][:10] < upto_date]
        if len(prior) < 5:
            return 0
        v = [x["v"] for x in prior[-lb:]]
        return sum(v) / len(v)

    # collect all trading dates
    all_dates = set()
    for s in SIP_UNIVERSE:
        all_dates |= set(intraday[s].keys())
    all_dates = sorted(all_dates)

    trades = []
    for d in all_dates:
        ds = str(d)
        cands = []
        for s in SIP_UNIVERSE:
            bars = sorted(intraday[s].get(d, []), key=lambda b: b["_et"])
            orb = next((b for b in bars if b["_et"].hour == 9 and b["_et"].minute == 30), None)
            if not orb:
                continue
            price = orb["c"]
            if price < min_price:
                continue
            a = atr14(daily[s], ds)
            if a is None or a < min_atr:
                continue
            v = adv(daily[s], ds)
            if v < min_avg_vol:
                continue
            exp5 = v * (5/390)
            rvol = orb["v"] / exp5 if exp5 > 0 else 0
            if rvol < min_rvol:
                continue
            rng = orb["h"] - orb["l"]
            if rng < 0.01 or orb["c"] == orb["o"]:
                continue
            direction = "long" if orb["c"] > orb["o"] else "short"
            trigger = orb["h"] if direction == "long" else orb["l"]
            cands.append({"sym": s, "dir": direction, "trigger": trigger,
                          "atr": a, "rvol": rvol, "bars": bars})
        cands.sort(key=lambda x: x["rvol"], reverse=True)
        for c in cands[:max_positions]:
            s, direction, trigger = c["sym"], c["dir"], c["trigger"]
            after = [b for b in c["bars"] if b["_et"].hour > 9 or
                     (b["_et"].hour == 9 and b["_et"].minute >= 35)]
            # stop-limit entry: only fills if price breaks the OR boundary
            entry, eidx = None, None
            for j, b in enumerate(after):
                if direction == "long" and b["h"] >= trigger:
                    entry = trigger * (1 + slip); eidx = j; break
                if direction == "short" and b["l"] <= trigger:
                    entry = trigger * (1 - slip); eidx = j; break
            if entry is None:
                continue  # never broke out — no fill
            qty = int(notional // entry)
            if qty < 1:
                continue
            stop = (entry - atr_stop_mult * c["atr"] if direction == "long"
                    else entry + atr_stop_mult * c["atr"])
            exit_price, reason = None, None
            for b in after[eidx:]:
                if b["_et"].hour > 15 or (b["_et"].hour == 15 and b["_et"].minute >= 55):
                    exit_price = b["o"]; reason = "eod"; break
                if direction == "long" and b["l"] <= stop:
                    exit_price = stop * (1 - slip); reason = "stop"; break
                if direction == "short" and b["h"] >= stop:
                    exit_price = stop * (1 + slip); reason = "stop"; break
            if exit_price is None:
                exit_price = after[-1]["c"]; reason = "eod"
            pnl = ((exit_price-entry) if direction == "long" else (entry-exit_price)) * qty
            trades.append({"entry_date": ds, "exit_date": ds, "symbol": s,
                           "direction": direction, "entry": entry, "exit": exit_price,
                           "pnl": pnl, "ret_pct": pnl/(entry*qty)*100, "reason": reason})

    yrs = max((all_dates[-1] - all_dates[0]).days / 365.25, 0.25)
    print(trade_metrics(trades, "  SIP-ORB", yrs))
    if trades:
        stops = sum(1 for t in trades if t["reason"] == "stop")
        print(f"  Stop exits: {stops}/{len(trades)} ({stops/len(trades)*100:.0f}%) | "
              f"days traded: {len(set(t['entry_date'] for t in trades))}")
    return {"trades": trades, "years": yrs}


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("dm", "all"):
        backtest_dual_momentum()
    if which in ("orb", "all"):
        backtest_orb()
    if which in ("sip", "all"):
        backtest_sip_orb()
