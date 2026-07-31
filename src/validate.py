"""Validation: the part that tries to kill your strategy.

Three attacks:
  1. Cost sensitivity -- does the edge survive pessimistic fills?
  2. Monte Carlo     -- what does a NORMAL bad run look like? You need this
                        number before you trade, so a routine drawdown doesn't
                        read as failure and make you quit at the worst moment.
  3. Out-of-sample   -- does it work on data the rules were never fitted to?

A strategy that passes none of these is not a strategy. A strategy that passes
all three is a candidate, not a certainty.
"""
import numpy as np
import pandas as pd
from .engine import Backtest, stats
from .costs import OPTIMISTIC, BASE, PESSIMISTIC
from .risk import RiskConfig


def cost_sensitivity(strategy_factory, bars, risk_cfg=None, equity=100_000.0):
    rows = []
    for label, cm in [("optimistic", OPTIMISTIC), ("base", BASE), ("pessimistic", PESSIMISTIC)]:
        bt = Backtest(strategy_factory(), cost_model=cm,
                      risk_cfg=risk_cfg or RiskConfig(), starting_equity=equity)
        tr = bt.run(bars)
        s = stats(tr, equity)
        s["cost_scenario"] = label
        s["round_turn_bps"] = cm.round_turn_bps()
        rows.append(s)
    return pd.DataFrame(rows).set_index("cost_scenario")


def monte_carlo(trades: pd.DataFrame, n_sims=10_000, equity=100_000.0, seed=0):
    """Reshuffle trade ORDER (not outcomes). Same trades, different sequence.
    Shows the distribution of drawdowns your edge can produce by luck alone."""
    if trades.empty:
        return {}
    pnl = trades["net_pnl"].to_numpy()
    rng = np.random.default_rng(seed)
    finals, dds = np.empty(n_sims), np.empty(n_sims)
    for k in range(n_sims):
        p = rng.permutation(pnl)
        eq = equity + np.cumsum(p)
        peak = np.maximum.accumulate(np.concatenate([[equity], eq]))[1:]
        dds[k] = ((eq - peak) / peak).min()
        finals[k] = eq[-1]
    return {
        "median_final": round(float(np.median(finals)), 2),
        "p05_final": round(float(np.percentile(finals, 5)), 2),
        "p95_final": round(float(np.percentile(finals, 95)), 2),
        "prob_losing": round(float((finals < equity).mean()), 4),
        "median_max_dd_pct": round(float(np.median(dds)) * 100, 2),
        "p95_max_dd_pct": round(float(np.percentile(dds, 5)) * 100, 2),
        "worst_max_dd_pct": round(float(dds.min()) * 100, 2),
    }


def expectancy_ci(trades: pd.DataFrame, conf=0.95):
    """Confidence interval on expectancy. If this straddles zero, you do not
    have evidence of an edge -- you have a sample that failed to disprove one."""
    if trades.empty:
        return {}
    r = trades["r_multiple"].to_numpy()
    n = len(r)
    se = r.std(ddof=1) / np.sqrt(n)
    z = 1.959964 if conf == 0.95 else 2.575829
    lo, hi = r.mean() - z * se, r.mean() + z * se
    return {"n": n, "expectancy_R": round(float(r.mean()), 4),
            "ci_low": round(float(lo), 4), "ci_high": round(float(hi), 4),
            "significant": bool(lo > 0),
            "t_stat": round(float(r.mean() / se), 2) if se > 0 else 0.0}


def report(name, trades, equity=100_000.0):
    s, ci, mc = stats(trades, equity), expectancy_ci(trades), monte_carlo(trades, 5000, equity)
    print(f"\n{'='*68}\n{name}\n{'='*68}")
    if not s.get("trades"):
        print("  no trades"); return
    print(f"  trades {s['trades']} over {s['trading_days']}d ({s['trades_per_day']}/day)")
    print(f"  win rate {s['win_rate']:.1%} | avg win {s['avg_win_R']:+.2f}R | avg loss {s['avg_loss_R']:+.2f}R")
    print(f"  expectancy {ci['expectancy_R']:+.4f}R  95% CI [{ci['ci_low']:+.4f}, {ci['ci_high']:+.4f}]  t={ci['t_stat']}")
    print(f"  EDGE SIGNIFICANT: {ci['significant']}")
    print(f"  gross ${s['gross_pnl']:,.0f} - costs ${s['total_costs']:,.0f} = NET ${s['net_pnl']:,.0f}")
    print(f"  net per day ${s['pnl_per_day']:,.2f} | return {s['return_pct']:.2f}% | maxDD {s['max_drawdown_pct']:.2f}%")
    if mc:
        print(f"  monte carlo: median maxDD {mc['median_max_dd_pct']:.1f}% | 5th-pct {mc['p95_max_dd_pct']:.1f}% | P(lose) {mc['prob_losing']:.1%}")
    return s
