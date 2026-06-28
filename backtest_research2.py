"""
Backtest of research batch #2 — NEW strategy families (none overlap the live
bots or batch #1). Tested on real Alpaca daily data, same discipline as always.

Families (from deep research, then verified HERE on our data):
  1. ETF PAIRS TRADING (distance/cointegration) — market-neutral. For each pair,
     z-score the price ratio over a rolling window; long the spread when z<-entry
     (long A / short B), short when z>+entry, exit when |z|<exit. Dollar-neutral,
     so the benchmark is ABSOLUTE Sharpe + low correlation to SPY (a diversifier),
     NOT beating SPY buy-and-hold. Pairs from the literature: SPY/IVV, QQQ/XLK,
     XLE/VDE, HYG/JNK, BND/AGG, USO/XLE.
  2. OPEX-WEEK EFFECT — long SPY only during options-expiration week (Mon–Fri of
     the 3rd-Friday week); cash otherwise. Reported Sharpe ~0.61 (1988-2010).
  3. VIX-SPIKE MEAN REVERSION — buy SPY when short-horizon realized volatility
     spikes above its longer average (the "buy the fear" reversion); exit when it
     normalizes. Realized vol is a clean VIX proxy from SPY's own returns.

Run: python backtest_research2.py
"""

import statistics
import time
from backtest_research import fetch_daily, perf_from_daily_returns, show

SLIP = 0.0005


# ── 1. ETF pairs trading ──────────────────────────────────────────────────────

PAIRS = [("SPY","IVV"), ("QQQ","XLK"), ("XLE","VDE"),
         ("HYG","JNK"), ("BND","AGG"), ("USO","XLE")]


def align(b1, b2):
    """Return (dates, c1, c2) on the intersection of trading dates."""
    m1 = {b["d"]: b["c"] for b in b1}
    m2 = {b["d"]: b["c"] for b in b2}
    dates = sorted(set(m1) & set(m2))
    return dates, [m1[d] for d in dates], [m2[d] for d in dates]


def pair_returns(c1, c2, lookback=60, entry=2.0, exit_z=0.5):
    """Daily dollar-neutral spread returns for one pair. Spread = ratio c1/c2."""
    ratio = [a / b for a, b in zip(c1, c2)]
    pos, rets = 0, []          # pos: +1 long spread (long c1/short c2), -1 short
    for i in range(len(ratio)):
        r = 0.0
        if i > 0 and pos != 0:
            r1 = c1[i] / c1[i-1] - 1
            r2 = c2[i] / c2[i-1] - 1
            r = pos * (r1 - r2)          # long spread profits when c1 outperforms c2
        if i >= lookback:
            window = ratio[i-lookback:i]
            mu = statistics.mean(window)
            sd = statistics.stdev(window) if len(window) > 1 else 0
            z = (ratio[i] - mu) / sd if sd > 0 else 0
            new = pos
            if pos == 0:
                if z > entry:    new = -1
                elif z < -entry: new = +1
            else:
                if abs(z) < exit_z:
                    new = 0
            if new != pos:
                r -= SLIP            # cost on every position change
                pos = new
        rets.append(r)
    return rets


# ── 2. OpEx week ──────────────────────────────────────────────────────────────

def opex_week_indices(bars):
    """Indices whose date falls in the Mon–Fri of an option-expiration week
    (the week containing the 3rd Friday of the month)."""
    from datetime import date
    in_week = set()
    by_month = {}
    for idx, b in enumerate(bars):
        y, m, d = (int(x) for x in b["d"].split("-"))
        by_month.setdefault((y, m), []).append((idx, date(y, m, d)))
    for (y, m), items in by_month.items():
        # 3rd Friday = first Friday + 14 days
        fridays = [dt for _, dt in items if dt.weekday() == 4]
        if not fridays:
            continue
        third_fri = sorted(fridays)[2] if len(fridays) >= 3 else sorted(fridays)[-1]
        for idx, dt in items:
            # same ISO week as the 3rd Friday
            if dt.isocalendar()[1] == third_fri.isocalendar()[1]:
                in_week.add(idx)
    return in_week


def opex_returns(bars):
    in_week = opex_week_indices(bars)
    closes = [b["c"] for b in bars]
    rets = []
    for i in range(len(bars)):
        r = closes[i] / closes[i-1] - 1 if (i > 0 and (i-1) in in_week) else 0.0
        rets.append(r)
    return rets


# ── 3. VIX-spike mean reversion (realized-vol proxy) ──────────────────────────

def vix_spike_returns(bars, fast=10, slow=50, spike=1.5, hold_exit=True):
    """Buy SPY when fast realized vol > spike × slow realized vol (a fear spike);
    exit when fast vol falls back below slow vol. Long-flat."""
    closes = [b["c"] for b in bars]
    drets  = [closes[i]/closes[i-1]-1 for i in range(1, len(bars))]
    rets, holding = [0.0], False
    for i in range(1, len(bars)):
        r = drets[i-1] if holding else 0.0
        rets.append(r)
        if i >= slow:
            fv = statistics.stdev(drets[i-fast:i])
            sv = statistics.stdev(drets[i-slow:i])
            if not holding and sv > 0 and fv > spike * sv:
                holding = True
            elif holding and fv <= sv:
                holding = False
    return rets


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    syms = sorted({s for p in PAIRS for s in p} | {"SPY"})
    print(f"Fetching daily bars for {len(syms)} symbols…")
    data = {}
    for s in syms:
        b = fetch_daily(s)
        if b:
            data[s] = b
        time.sleep(0.1)
    spy = data["SPY"]
    spy_bh = [spy[i]["c"]/spy[i-1]["c"]-1 for i in range(1, len(spy))]
    bench = perf_from_daily_returns(spy_bh)
    print(f"SPY {spy[0]['d']}→{spy[-1]['d']}\n")
    show("SPY buy & hold (benchmark)", bench)

    print("\n── 1. ETF pairs trading (market-neutral; want absolute Sharpe>1, low corr) ──")
    all_pair_rets = []
    for a, b in PAIRS:
        if a not in data or b not in data:
            print(f"  {a}/{b}: missing data"); continue
        dates, c1, c2 = align(data[a], data[b])
        if len(dates) < 120:
            print(f"  {a}/{b}: too short ({len(dates)})"); continue
        rets = pair_returns(c1, c2, lookback=60, entry=2.0, exit_z=0.5)
        m = perf_from_daily_returns(rets)
        # correlation to SPY over shared dates
        show(f"{a}/{b}", m)
        all_pair_rets.append((dates, rets))

    # Combined equal-weight market-neutral portfolio across pairs.
    from collections import defaultdict
    acc, cnt = defaultdict(float), defaultdict(int)
    for dates, rets in all_pair_rets:
        for d, r in zip(dates, rets):
            acc[d] += r; cnt[d] += 1
    combo_dates = sorted(acc)
    combo = [acc[d]/cnt[d] for d in combo_dates]
    mc = perf_from_daily_returns(combo)
    show("COMBINED pairs (eq-wt)", mc)
    # correlation of combined to SPY
    spd = {spy[i]["d"]: spy_bh[i-1] for i in range(1, len(spy))}
    pairs_xy = [(combo[k], spd[combo_dates[k]]) for k in range(len(combo_dates)) if combo_dates[k] in spd]
    if len(pairs_xy) > 2:
        xs = [x for x, _ in pairs_xy]; ys = [y for _, y in pairs_xy]
        mx, my = statistics.mean(xs), statistics.mean(ys)
        cov = sum((x-mx)*(y-my) for x, y in pairs_xy)/len(pairs_xy)
        corr = cov/(statistics.pstdev(xs)*statistics.pstdev(ys)) if statistics.pstdev(xs)*statistics.pstdev(ys) else 0
        print(f"     combined-vs-SPY correlation: {corr:+.2f}  (near 0 = good diversifier)")

    print("\n── 2. OpEx-week effect (long SPY only that week) ──")
    show("OpEx week SPY", perf_from_daily_returns(opex_returns(spy)), bench)

    print("\n── 3. VIX-spike mean reversion (realized-vol proxy, long-flat) ──")
    for spike in (1.3, 1.5, 2.0):
        show(f"VIX-spike SPY (x{spike})",
             perf_from_daily_returns(vix_spike_returns(spy, spike=spike)), bench)

    print("\nPairs are market-neutral → judge by absolute Sharpe + low SPY corr. "
          "OpEx/VIX-spike are long-flat → judge vs SPY buy&hold.")
