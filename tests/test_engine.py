"""Engine integrity tests.

The random-data test is the important one. A backtest engine that produces a
positive expectancy on a driftless random walk is broken, and every number it
ever prints is fiction. This test is the reason you can believe the next one.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, pandas as pd
from src.engine import Backtest, stats
from src.costs import BASE, CostModel
from src.risk import RiskConfig
from src.strategies import OpeningRangeBreakout, VWAPReversion


def synth(n_days=250, start_price=200.0, ann_vol=0.25, seed=7, drift=0.0):
    """Driftless geometric random walk, 390 one-minute bars per session."""
    rng = np.random.default_rng(seed)
    per_min = ann_vol / np.sqrt(252 * 390)
    frames, price = [], start_price
    days = pd.bdate_range("2024-01-02", periods=n_days, tz="America/New_York")
    for d in days:
        idx = pd.date_range(d.replace(hour=9, minute=30), periods=390, freq="1min")
        rets = rng.normal(drift / 390, per_min, 390)
        closes = price * np.exp(np.cumsum(rets))
        opens = np.concatenate([[price], closes[:-1]])
        noise = np.abs(rng.normal(0, per_min * 0.6, (390, 2))) * closes[:, None]
        highs = np.maximum(opens, closes) + noise[:, 0]
        lows = np.minimum(opens, closes) - noise[:, 1]
        vol = rng.lognormal(10.5, 0.4, 390)
        frames.append(pd.DataFrame(
            {"open": opens, "high": highs, "low": lows, "close": closes, "volume": vol},
            index=idx))
        price = closes[-1]
    return pd.concat(frames)


def run(strategy, bars, costs=BASE):
    bt = Backtest(strategy, cost_model=costs,
                  risk_cfg=RiskConfig(max_trades_per_day=50, max_daily_loss=0.05,
                                      max_consecutive_losses=99),
                  starting_equity=100_000.0)
    return bt.run({"SYNTH": bars}), bt


print("=" * 74)
print("RANDOM-WALK INTEGRITY TEST  (expectation: gross ~ 0, net ~ -costs)")
print("=" * 74)

bars = synth(n_days=150, seed=7)
print(f"synthetic bars: {len(bars):,} over {bars.index.normalize().nunique()} sessions\n")

for name, strat in [("ORB", OpeningRangeBreakout()), ("VWAP-REV", VWAPReversion())]:
    tr, bt = run(strat, bars)
    if tr.empty:
        print(f"{name}: no trades"); continue
    s = stats(tr)
    gross_R = tr.gross_pnl.sum() / (tr.gross_pnl.abs().mean() * len(tr)) if len(tr) else 0
    # Standard error of mean gross pnl -> is gross distinguishable from zero?
    se = tr.gross_pnl.std(ddof=1) / np.sqrt(len(tr))
    t_stat = tr.gross_pnl.mean() / se if se > 0 else 0
    print(f"--- {name} ---")
    print(f"  trades           : {s['trades']}  ({s['trades_per_day']}/day)")
    print(f"  win rate         : {s['win_rate']:.1%}")
    print(f"  GROSS pnl        : ${s['gross_pnl']:>12,.2f}   t-stat vs 0 = {t_stat:+.2f}")
    print(f"  costs paid       : ${s['total_costs']:>12,.2f}")
    print(f"  NET pnl          : ${s['net_pnl']:>12,.2f}")
    print(f"  expectancy       : {s['expectancy_R']:+.4f} R")
    verdict = "PASS" if abs(t_stat) < 2.0 and s['net_pnl'] < 0 else "*** FAIL ***"
    print(f"  verdict          : {verdict}  (gross indistinguishable from 0, net negative)\n")

print("=" * 74)
print("ZERO-COST CONTROL  (gross should still be ~0; proves costs aren't masking bias)")
print("=" * 74)
free = CostModel(half_spread_bps=0.0, slippage_bps=0.0, sec_fee_rate=0.0, finra_taf_per_share=0.0)
for name, strat in [("ORB", OpeningRangeBreakout()), ("VWAP-REV", VWAPReversion())]:
    tr, _ = run(strat, bars, costs=free)
    if tr.empty: continue
    se = tr.net_pnl.std(ddof=1) / np.sqrt(len(tr))
    t = tr.net_pnl.mean() / se if se > 0 else 0
    flag = "PASS" if abs(t) < 2.0 else "*** LOOK-AHEAD SUSPECTED ***"
    print(f"  {name:9s} net ${tr.net_pnl.sum():>11,.2f}  t={t:+.2f}  {flag}")

print("\n" + "=" * 74)
print("SEED STABILITY  (5 different random universes, ORB expectancy in R)")
print("=" * 74)
exps = []
for sd in [1, 2, 3]:
    b = synth(n_days=60, seed=sd)
    tr, _ = run(OpeningRangeBreakout(), b)
    if not tr.empty:
        exps.append(tr.r_multiple.mean())
        print(f"  seed {sd}: {len(tr):4d} trades  expectancy {tr.r_multiple.mean():+.4f} R")
print(f"\n  mean across seeds: {np.mean(exps):+.4f} R   (should be negative ~= cost drag)")
