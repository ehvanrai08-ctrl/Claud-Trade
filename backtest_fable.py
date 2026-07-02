"""
Backtest — Fable regime-aware meta-allocator (original strategy candidate)
==========================================================================
Hypothesis: momentum and mean-reversion are complementary styles — momentum
earns in calm/trending tape, mean reversion earns in volatile/choppy tape.
A meta-allocator that measures the volatility regime and tilts capital between
the two sleeves should beat holding either sleeve alone AND a static blend.

Sleeves (mirrors of live bots, so a PASS is actually deployable):
  MOM — top-3 of 11 sector SPDRs by ensembled 9-12mo momentum, monthly
        (= sector_momentum.py).
  MR  — 50/50 of IBS(<0.2 in, >0.8 out) on QQQ and RSI2(<10 in, >5dSMA out,
        200SMA gate) on SPY (= ibs_strategy.py + rsi2_strategy.py).

Regime signal: ratio of fast (20d) to slow (60d) realized vol of SPY.
  ratio > T  → volatile/choppy → weight MR sleeve wMR_hi (e.g. 80%)
  ratio <= T → calm/trending   → weight MOM sleeve wMOM_hi (e.g. 80%)
Rebalanced daily on yesterday's signal (no lookahead), 5bps on weight switches.

THE HONEST BAR (all three, or it fails):
  1. beat SPY buy-and-hold risk-adjusted (Sharpe), with lower maxDD
  2. beat the STATIC 50/50 blend of the same sleeves — the null that kills
     "regime intelligence is just diversification in a costume"
  3. robust across the threshold/window grid, not a single lucky cell

Run: python backtest_fable.py
"""

import statistics
import time
from backtest_research import fetch_daily, perf_from_daily_returns, show

SECTORS = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLP", "XLY", "XLU", "XLB", "XLRE", "XLC"]
SLIP = 0.0005


# ── Sleeve return series (aligned to a shared date index) ─────────────────────

def dates_union(data):
    ds = set()
    for bars in data.values():
        ds.update(b["d"] for b in bars)
    return sorted(ds)


def mom_sleeve(data, dates, lookbacks=(9, 10, 11, 12), top_n=3):
    """Daily returns of the sector-momentum sleeve (monthly top-N rotation)."""
    cbd = {s: {b["d"]: b["c"] for b in data[s]} for s in SECTORS}
    hist = {s: [] for s in SECTORS}
    prev = {}
    rets, holdings, month = [], [], None
    for d in dates:
        for s in SECTORS:
            px = cbd[s].get(d)
            if px is not None:
                hist[s].append(px)
        r = 0.0
        if holdings:
            cs = [cbd[s][d] / prev[s] - 1 for s in holdings
                  if cbd[s].get(d) is not None and prev.get(s)]
            r = sum(cs) / len(holdings) if cs else 0.0
        rets.append(r)
        for s in SECTORS:
            if cbd[s].get(d) is not None:
                prev[s] = cbd[s][d]
        ym = d[:7]
        if ym != month:
            month = ym
            scored = []
            for s in SECTORS:
                h = hist[s]
                need = max(lookbacks) * 21 + 1
                if len(h) < need:
                    continue
                rs = [h[-1] / h[-1 - m * 21] - 1 for m in lookbacks if h[-1 - m * 21] > 0]
                if rs:
                    scored.append((s, sum(rs) / len(rs)))
            scored.sort(key=lambda x: x[1], reverse=True)
            new = [s for s, _ in scored[:top_n]]
            if set(new) != set(holdings):
                rets[-1] -= SLIP
            holdings = new
    return rets


def ibs_series(bars):
    """IBS mean reversion on one symbol: buy close if IBS<0.2, exit IBS>0.8."""
    rets, holding = [], False
    for i, b in enumerate(bars):
        r = 0.0
        if holding and i > 0:
            r = b["c"] / bars[i - 1]["c"] - 1
        rets.append(r)
        rng = b["h"] - b["l"]
        ibs = (b["c"] - b["l"]) / rng if rng > 0 else 0.5
        if not holding and ibs < 0.20:
            holding = True
            rets[-1] -= SLIP
        elif holding and ibs > 0.80:
            holding = False
            rets[-1] -= SLIP
    return rets


def rsi2_series(bars):
    """RSI(2)<10 + >200SMA in, close>5dSMA out (Connors)."""
    closes = [b["c"] for b in bars]
    rets, holding = [], False
    for i in range(len(bars)):
        r = 0.0
        if holding and i > 0:
            r = closes[i] / closes[i - 1] - 1
        rets.append(r)
        if i < 200:
            continue
        gains = losses = 0.0
        for k in (i - 1, i):
            ch = closes[k] - closes[k - 1]
            gains += max(ch, 0); losses += max(-ch, 0)
        rsi = 100.0 if losses == 0 else 100 - 100 / (1 + (gains / 2) / (losses / 2))
        sma200 = sum(closes[i - 199:i + 1]) / 200
        sma5 = sum(closes[i - 4:i + 1]) / 5
        if not holding and rsi < 10 and closes[i] > sma200:
            holding = True
            rets[-1] -= SLIP
        elif holding and closes[i] > sma5:
            holding = False
            rets[-1] -= SLIP
    return rets


def align(series, bars, dates):
    """Map a per-bar return series onto the shared date index (0 when absent)."""
    m = {b["d"]: series[i] for i, b in enumerate(bars)}
    return [m.get(d, 0.0) for d in dates]


# ── Regime signal + combination ───────────────────────────────────────────────

def vol_ratio_signal(spy_bars, dates, fast=20, slow=60):
    """Yesterday's fast/slow realized-vol ratio of SPY per shared date (no lookahead)."""
    closes = [b["c"] for b in spy_bars]
    drets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    sig_by_date = {}
    for i in range(1, len(spy_bars)):
        j = i - 1                       # returns up to and including day i-1
        if j >= slow:
            fv = statistics.stdev(drets[j - fast:j])
            sv = statistics.stdev(drets[j - slow:j])
            sig_by_date[spy_bars[i]["d"]] = fv / sv if sv > 0 else 1.0
    out, last = [], 1.0
    for d in dates:
        last = sig_by_date.get(d, last)
        out.append(last)
    return out


def combine(mom, mr, signal, thresh=1.25, w_hi=0.8):
    """Daily blend: calm → w_hi on MOM, choppy → w_hi on MR. 5bps per switch."""
    rets, prev_regime = [], None
    for i in range(len(mom)):
        choppy = signal[i] > thresh
        w_mr = w_hi if choppy else 1 - w_hi
        r = w_mr * mr[i] + (1 - w_mr) * mom[i]
        if prev_regime is not None and choppy != prev_regime:
            r -= SLIP
        prev_regime = choppy
        rets.append(r)
    return rets


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    syms = SECTORS + ["SPY", "QQQ"]
    print(f"Fetching {len(syms)} symbols…")
    data = {}
    for s in syms:
        b = fetch_daily(s)
        if b:
            data[s] = b
        time.sleep(0.1)
    dates = dates_union({s: data[s] for s in SECTORS})

    spy, qqq = data["SPY"], data["QQQ"]
    spy_bh = [spy[i]["c"] / spy[i - 1]["c"] - 1 for i in range(1, len(spy))]
    bench = perf_from_daily_returns(spy_bh)
    print(f"\n{dates[0]} → {dates[-1]} ({len(dates)} days)\n")
    show("SPY buy & hold", bench)

    mom = mom_sleeve(data, dates)
    mr = [(a + b) / 2 for a, b in zip(align(ibs_series(qqq), qqq, dates),
                                      align(rsi2_series(spy), spy, dates))]
    show("MOM sleeve alone", perf_from_daily_returns(mom), bench)
    show("MR sleeve alone", perf_from_daily_returns(mr), bench)

    static = [(a + b) / 2 for a, b in zip(mom, mr)]
    static_m = perf_from_daily_returns(static)
    show("STATIC 50/50 (the null)", static_m, bench)

    print("\n── Regime-aware blends (grid = robustness test) ──")
    best = None
    for fast, slow in ((10, 40), (20, 60), (20, 100)):
        signal = vol_ratio_signal(spy, dates, fast, slow)
        for thresh in (1.10, 1.25, 1.40):
            for w_hi in (0.7, 0.8, 1.0):
                m = perf_from_daily_returns(combine(mom, mr, signal, thresh, w_hi))
                tag = f"vol{fast}/{slow} T={thresh} w={w_hi}"
                show(f"FABLE {tag}", m, bench)
                if best is None or m["sharpe"] > best[1]["sharpe"]:
                    best = (tag, m)

    print("\n── VERDICT ──")
    print(f"  Best cell: {best[0]} — Sharpe {best[1]['sharpe']:.2f}, maxDD {best[1]['mdd']:.1f}%")
    print(f"  Null (static 50/50): Sharpe {static_m['sharpe']:.2f}, maxDD {static_m['mdd']:.1f}%")
    print(f"  SPY: Sharpe {bench['sharpe']:.2f}, maxDD {bench['mdd']:.1f}%")
    print("  PASS requires: best-cell AND median-cell > static 50/50 AND > SPY "
          "(Sharpe), with the grid broadly positive — not one lucky cell.")
