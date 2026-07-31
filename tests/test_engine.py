"""Engine integrity tests.

The random-data test is the important one. A backtest engine that produces a
positive expectancy on a driftless random walk is broken, and every number it
ever prints is fiction. This test is the reason you can believe the next one.
"""
import sys, os
from math import comb
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


failures = []   # every check appends here; non-empty => exit 1 (see bottom)

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
    ok = abs(t_stat) < 2.0 and s['net_pnl'] < 0
    if not ok:
        failures.append(
            f"{name}: random-walk gross t={t_stat:+.2f} (want |t|<2), "
            f"net ${s['net_pnl']:,.2f} (want < 0)")
    verdict = "PASS" if ok else "*** FAIL ***"
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
    # With zero costs the expectation is exactly 0, so net is positive half the
    # time by chance -- |t| is the right criterion here, NOT the sign of net.
    ok = abs(t) < 2.0
    if not ok:
        failures.append(f"{name}: zero-cost control t={t:+.2f} (want |t|<2)")
    flag = "PASS" if ok else "*** LOOK-AHEAD SUSPECTED ***"
    print(f"  {name:9s} net ${tr.net_pnl.sum():>11,.2f}  t={t:+.2f}  {flag}")

# 3 seeds x ~30 trades had too little power to separate "no edge" from "small
# look-ahead edge" -- a wrong-signed mean sat well inside |t|<2. More universes.
SEEDS = list(range(1, 11))

print("\n" + "=" * 74)
print(f"SEED STABILITY  ({len(SEEDS)} different random universes, ORB expectancy in R)")
print("=" * 74)
exps, pooled, gross_signs = [], [], []
for sd in SEEDS:
    b = synth(n_days=60, seed=sd)
    tr, _ = run(OpeningRangeBreakout(), b)
    if not tr.empty:
        exps.append(tr.r_multiple.mean())
        pooled.append(tr.r_multiple)
        gross_signs.append(tr.gross_pnl.mean() > 0)
        print(f"  seed {sd}: {len(tr):4d} trades  expectancy {tr.r_multiple.mean():+.4f} R")

mean_exp = float(np.mean(exps))
n_pos = sum(e > 0 for e in exps)
print(f"\n  mean across seeds: {mean_exp:+.4f} R   (should be negative ~= cost drag)")
print(f"  seeds with positive expectancy: {n_pos}/{len(exps)}")

# Pooled across every universe this is the highest-power look at the sign.
allr = pd.concat(pooled)
se = allr.std(ddof=1) / np.sqrt(len(allr))
t_pool = allr.mean() / se if se > 0 else 0.0
print(f"  pooled: {len(allr)} trades  {allr.mean():+.4f} R  t={t_pool:+.2f}")

# SIGN TEST on gross, per universe. Trade P&L is heavy-tailed, so a few large
# winners inflate the standard error and the t-test loses power: a candidate
# ORB that was clearly biased still only reached t=+1.44 over 2,950 trades,
# while the sign test on the same run gave 22/30 universes positive, p=0.008.
# Under the null (true gross = 0) each universe is a fair coin, so this is
# distribution-free. It needs ~30 universes to bite; at 10 it is a diagnostic.
k, n = sum(gross_signs), len(gross_signs)
p_sign = sum(comb(n, i) for i in range(k, n + 1)) / 2 ** n if n else 1.0
print(f"  gross sign test: {k}/{n} universes positive  (one-tailed p={p_sign:.4f})")
if n >= 20 and p_sign < 0.05:
    failures.append(
        f"seed stability: {k}/{n} universes have positive mean GROSS "
        f"(p={p_sign:.4f}) -- on random data the sign should be a coin flip")

if mean_exp >= 0:
    failures.append(
        f"seed stability: mean expectancy {mean_exp:+.4f} R across {len(exps)} "
        f"universes is not negative ({n_pos} positive) -- random data must lose "
        f"to cost drag")

# The sharp check. On random data expectancy should sit at MINUS THE COST DRAG,
# not at zero -- so the pooled t must be decisively negative. A strategy that
# lands on ~0.00 R has found roughly +cost_drag of gross edge in pure noise,
# which is the look-ahead signature. Merely "mean < 0" is too weak to catch it.
if t_pool >= -2.0:
    failures.append(
        f"seed stability: pooled expectancy {allr.mean():+.4f} R (t={t_pool:+.2f}) "
        f"is not decisively below zero -- random data should lose to costs, so "
        f"landing near 0.00 R means gross edge is cancelling the drag")

print("\n" + "=" * 74)
if failures:
    print(f"INTEGRITY: *** FAILED *** ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    print("=" * 74)
    print("\nThe engine reports an edge on data that has none. Every downstream\n"
          "number is fiction until this is fixed. Refusing to pass.")
    sys.exit(1)
print("INTEGRITY: ALL CHECKS PASSED")
print("=" * 74)
