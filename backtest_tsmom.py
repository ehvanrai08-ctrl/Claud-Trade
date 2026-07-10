"""
Backtest — Multi-Asset Time-Series Momentum (TSMOM) robustness grid
===================================================================
Follow-up to reports/strategy_research_50fresh.md: the 50-strategy sweep's
standout was a 12-month TSMOM sleeve across SPY/TLT/GLD/DBC/UUP (Sharpe 1.20,
maxDD 7.8%), with a second variant from the same family (50/200 SMA crossover,
Sharpe 1.00, maxDD 8.9%) independently near the bar. Per project convention a
single-cell result earns a GRID, not capital — sector_momentum only deployed
after surviving its lookback×top_n sweep.

Design (Moskowitz-Ooi-Pedersen style, long-only, ETF-implementable):
  Each asset independently: if its trend signal is ON at month-end, hold it
  next month; if OFF, that slot sits in BIL (cash). Equal slot weights.
  Monthly rebalance, 5bps slippage on any slot that flips.

Grid axes:
  - Signal: total-return lookback 3/6/9/12 months, ensemble(6,9,12),
    and the 50/200 SMA crossover variant.
  - Trend gate: none vs price>200d SMA (AND-ed with the return signal).
  - Universe: core 5 (SPY/TLT/GLD/DBC/UUP) vs no-UUP 4 (UUP is a rate
    product more than an asset class; check it isn't carrying the result).

Honest benchmarks (BOTH must be beaten for a real pass):
  1. SPY buy & hold (the absolute bar).
  2. Equal-weight buy & hold of the SAME basket, never timed (the null that
     isolates whether the TREND SIGNAL adds value over mere diversification —
     same test that killed the regime-allocator and emerging-selection ideas).

Plus an in/out-of-sample split (2017-2021 vs 2022-2026) on the headline cell.

Run: python backtest_tsmom.py
"""

import time
from backtest_research import fetch_daily, perf_from_daily_returns, show

CORE   = ["SPY", "TLT", "GLD", "DBC", "UUP"]
CASH   = "BIL"
SLIP   = 0.0005
TDM    = 21          # trading days per month


def closes_by_date(bars):
    return {b["d"]: b["c"] for b in bars}


def dates_union(data):
    ds = set()
    for bars in data.values():
        ds.update(b["d"] for b in bars)
    return sorted(ds)


def run_sleeve(data, dates, assets, signal="tr", lookbacks=(12,), sma_gate=False,
               timed=True):
    """Daily return series of the sleeve.
    signal='tr'  : trailing total return over each lookback (avg of ensemble) > 0
    signal='sma' : 50d SMA > 200d SMA (crossover variant)
    sma_gate     : additionally require price > 200d SMA
    timed=False  : ignore signals, always hold every asset (the null benchmark).
    Cash slots earn BIL's return."""
    cbd  = {s: closes_by_date(data[s]) for s in assets + [CASH]}
    hist = {s: [] for s in assets + [CASH]}
    prev = {}
    held = {s: False for s in assets}     # per-slot: asset (True) or cash (False)
    rets, month = [], None

    for d in dates:
        for s in assets + [CASH]:
            px = cbd[s].get(d)
            if px is not None:
                hist[s].append(px)

        # daily return: average over slots (asset return if ON, BIL if OFF)
        contribs = []
        for s in assets:
            sym = s if held[s] else CASH
            p_now, p_prev = cbd[sym].get(d), prev.get(sym)
            contribs.append(p_now / p_prev - 1 if (p_now is not None and p_prev) else 0.0)
        rets.append(sum(contribs) / len(assets))

        for s in assets + [CASH]:
            if cbd[s].get(d) is not None:
                prev[s] = cbd[s][d]

        ym = d[:7]
        if ym != month:
            month = ym
            flips = 0
            for s in assets:
                h = hist[s]
                if timed:
                    if signal == "tr":
                        need = max(lookbacks) * TDM + 1
                        if len(h) < need:
                            on = False
                        else:
                            sigs = [h[-1] / h[-1 - L * TDM] - 1 for L in lookbacks]
                            on = (sum(sigs) / len(sigs)) > 0
                    else:  # sma crossover
                        if len(h) < 200:
                            on = False
                        else:
                            on = (sum(h[-50:]) / 50) > (sum(h[-200:]) / 200)
                    if on and sma_gate and len(h) >= 200:
                        on = h[-1] > sum(h[-200:]) / 200
                else:
                    on = len(h) > 0     # null: hold as soon as data exists
                if on != held[s]:
                    flips += 1
                held[s] = on
            if flips:
                rets[-1] -= SLIP * flips / len(assets)
    return rets


def split(dates, rets, cut="2022-01-01"):
    i = next((k for k, d in enumerate(dates) if d >= cut), len(dates))
    return rets[:i], rets[i:]


if __name__ == "__main__":
    syms = CORE + [CASH]
    print(f"Fetching {len(syms)} symbols…")
    data = {}
    for s in syms:
        b = fetch_daily(s)
        if b:
            data[s] = b
        time.sleep(0.1)
    dates = dates_union({s: data[s] for s in CORE})

    spy = data["SPY"]
    spy_bh = [spy[i]["c"] / spy[i - 1]["c"] - 1 for i in range(1, len(spy))]
    bench = perf_from_daily_returns(spy_bh)
    print(f"\n{dates[0]} → {dates[-1]} ({len(dates)} days)\n")
    show("SPY buy & hold", bench)

    for assets, tag in ((CORE, "5-asset"), (CORE[:4], "4-asset (no UUP)")):
        null = perf_from_daily_returns(run_sleeve(data, dates, assets, timed=False))
        show(f"NULL {tag}: EW hold basket, no timing", null, bench)

    print("\n── Grid (each cell must beat BOTH SPY-Sharpe and its null's Sharpe) ──")
    best = None
    for assets, tag in ((CORE, "5a"), (CORE[:4], "4a")):
        for sig, lbs, name in (("tr", (3,), "tr3"), ("tr", (6,), "tr6"),
                               ("tr", (9,), "tr9"), ("tr", (12,), "tr12"),
                               ("tr", (6, 9, 12), "trEns"), ("sma", (), "sma50/200")):
            for gate in (False, True):
                m = perf_from_daily_returns(
                    run_sleeve(data, dates, assets, sig, lbs, gate))
                label = f"{tag} {name}{'+gate' if gate else ''}"
                show(f"TSMOM {label}", m, bench)
                if best is None or m["sharpe"] > best[1]["sharpe"]:
                    best = (label, m, (assets, sig, lbs, gate))

    print(f"\nBest cell: {best[0]} — Sharpe {best[1]['sharpe']:.2f}, "
          f"maxDD {best[1]['mdd']:.1f}%")

    # In/out-of-sample on the HEADLINE cell (12mo, 5-asset, no gate — the one
    # the sweep found), not the best cell (that would be selection bias).
    head = run_sleeve(data, dates, CORE, "tr", (12,), False)
    ins, oos = split(dates, head)
    spy_map = {spy[i]["d"]: spy_bh[i - 1] for i in range(1, len(spy))}
    spy_al = [spy_map.get(d, 0.0) for d in dates]
    s_in, s_out = split(dates, spy_al)
    print("\n── In/out-of-sample: headline cell (5a tr12, no gate) vs SPY ──")
    show("IS  2017-2021 TSMOM", perf_from_daily_returns(ins))
    show("IS  2017-2021 SPY", perf_from_daily_returns(s_in))
    show("OOS 2022-2026 TSMOM", perf_from_daily_returns(oos))
    show("OOS 2022-2026 SPY", perf_from_daily_returns(s_out))

    print("\nPASS requires: headline + most cells above BOTH benchmarks' Sharpe, "
          "OOS not collapsing, and the no-UUP universe holding up.")
