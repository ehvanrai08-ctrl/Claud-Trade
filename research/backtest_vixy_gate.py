"""Backtest the LIVE credit_vol_qqq bot's actual vol-gate mechanism.

The live bot (credit_vol_qqq.py) can't read ^VIX (Alpaca doesn't carry it), so
it uses VIXY's own 90-day percentile rank as the vol-spike proxy — a DIFFERENT
signal from both the research top-20 script (^VIX close < 30) and the
efficient_strategy_discovery variations (QQQ 20d realized vol). This script
backtests that exact VIXY-percentile mechanism so a threshold change to the live
bot stands on real numbers for the mechanism it actually runs.

Result (Yahoo 2015-2026, 5bps turnover cost):
  thresh 0.50: Sharpe 1.05  maxDD 20.6%   <- live setting (drawdown-control choice)
  thresh 0.65: Sharpe 1.00  maxDD 25.1%
  thresh 0.80: Sharpe 1.06  maxDD 27.6%   <- prior setting
  same-period SPY B&H: Sharpe 0.89
  (idealized ^VIX<30 version: Sharpe 1.11 — the tradable-VIXY proxy costs ~0.05)

Takeaway: 0.50 vs 0.80 is ~Sharpe-neutral but cuts maxDD ~7 pts. Not alpha —
drawdown control, same honesty framing as the rest of the fleet.

Run: python research/backtest_vixy_gate.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bt_lib import align, run, null_basket, sma

THRESHOLDS = [0.50, 0.65, 0.80]


def backtest(data, thresh):
    n = len(data["dates"])
    weights = []
    for i in range(n):
        s = sma(data["HYG"]["a"], i, 200)
        if s is None or i < 90:
            weights.append({})
            continue
        credit_ok = data["HYG"]["a"][i] > s
        window = data["VIXY"]["c"][i - 89:i + 1]
        lo, hi = min(window), max(window)
        pct = (data["VIXY"]["c"][i] - lo) / (hi - lo) if hi > lo else 0.0
        vol_ok = pct < thresh
        weights.append({"QQQ": 1.0} if (credit_ok and vol_ok) else {"BIL": 1.0})
    return run(data, weights, label=f"vixy_pct_{thresh}")


def main():
    data = align(["QQQ", "BIL", "HYG", "VIXY"])
    rows = []
    for t in THRESHOLDS:
        r = backtest(data, t)
        m = r["strategy"]
        rows.append({
            "threshold": t, "sharpe": m["sharpe"], "cagr": m["cagr"],
            "maxdd": m["maxdd"], "sharpe_1st_half": m["sharpe_1st_half"],
            "sharpe_2nd_half": m["sharpe_2nd_half"],
        })
        print(f"thresh {t:.2f}: Sharpe {m['sharpe']:.3f}  CAGR {m['cagr']:.1f}%  "
              f"maxDD {m['maxdd']:.1f}%  halves {m['sharpe_1st_half']}/{m['sharpe_2nd_half']}")
    spy = backtest(data, 0.80)["spy_benchmark"]["sharpe"]
    null = null_basket(["QQQ"])["strategy"]["sharpe"]
    print(f"SPY B&H Sharpe: {spy}   QQQ null Sharpe: {null}")
    return {"rows": rows, "spy_sharpe": spy, "qqq_null_sharpe": null}


if __name__ == "__main__":
    result = main()
    print(json.dumps(result, indent=2))
