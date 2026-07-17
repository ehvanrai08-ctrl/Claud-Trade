# Strategy Research — Top 20 Profitable Candidates (2026-07-16)

**Not deployed.** Research only, run in an environment with no Alpaca keys —
data came from Yahoo Finance's keyless chart API (11 years, 45 symbols, daily
bars, 2015-07 → 2026-07-15). Methodology, code, and honesty framing follow the
existing `backtest_*.py` convention (`backtest_tsmom.py` etc.): every result
below is a **real run**, not an estimate.

## Method

1. Built a shared, look-ahead-safe backtest core (`research/bt_lib.py`):
   weights decided at bar *i*'s close may only see data through *i*; they earn
   the *i→i+1* **adjusted**-close return; 5bps turnover cost; indicator
   warm-up trimmed; every result auto-includes the same-period SPY
   buy-and-hold benchmark, correlation to SPY, and a first-half/second-half
   Sharpe split (cheap regime-robustness check).
2. Curated 54 candidate specs biased toward the families this repo's *prior*
   50-strategy sweep proved out (absolute trend-following with cash parking,
   ETF mean reversion above a 200d gate, vol-regime switches, vol targeting,
   diversified multi-asset gating) and away from families it proved dead
   (pairs/stat-arb, seasonality, single-asset breakouts, gap fades).
3. Two independent agents proposed 15 more novel specs from the same
   playbook, filtered against duplicates.
4. **69 candidates backtested** as literal, unparameterized implementations
   of their spec (implementer is instructed not to tune).
5. Every profitable result (Sharpe ≥ 0.5, CAGR > 0) went through **two-lens
   adversarial verification** — one auditor re-running the script hunting
   look-ahead/mechanics bugs, one checking spec fidelity and regime
   robustness (auto-reject if either backtest half has Sharpe < −0.2).
   Disagreements went to a tie-breaker judge.

**Result: 53 of 69 candidates profitable; all 53 survived adversarial audit
(0 refuted).** That high survival rate reflects the curation bias step (3),
not a claim that arbitrary strategies work — the dead families were filtered
out before backtesting, not after.

## How to read this

Genuine alpha (beating SPY's own risk-adjusted return, out of sample) is rare
and this sweep found some plausible candidates — but the bulk of what
"profitable" means here, same as the live `tsmom_sleeve`, is **diversification
and drawdown control**, not return enhancement. Where a strategy's own
untimed buy-and-hold basket (`null_sharpe`) scores as well or better, the
timing signal is adding ~0 — flagged explicitly below, exactly as
`tsmom_sleeve.py`'s docstring already does for the live bot.

---

## Tier 1 — Beat SPY on both Sharpe AND max drawdown

The most credible subset: not just a higher Sharpe than SPY, but achieved
with materially less pain.

| id | Sharpe | SPY Sharpe | CAGR | MaxDD | Corr(SPY) | Note |
|---|---|---|---|---|---|---|
| `tsmom_voltgt` | 1.12 | 0.88 | 5.4% | **6.8%** | 0.45 | Inverse-vol-weighted 12m TSMOM, 5 assets. Beats null (1.07) only marginally — value is the 6.8% maxDD. |
| `tsmom12_5a` | 1.03 | 0.88 | 6.2% | 7.5% | 0.51 | **This is the same family as the live `tsmom_sleeve.py` bot** — cross-validates its deployed design on an independent (Yahoo) data source. |
| `tsmom_dualfil` | 1.06 | 0.88 | 5.9% | 7.5% | 0.46 | Adds a 200d-SMA AND-gate on top of the 12m return filter — no better than plain TSMOM. |
| `tsmom6_5a` | 1.09 | 0.92 | 6.3% | 7.5% | 0.40 | 6-month lookback variant; lowest SPY-correlation of the TSMOM family. |
| `tsmom_12_1` | 1.01 | 0.88 | 6.2% | 8.5% | 0.54 | Skip-most-recent-month (12-1) momentum, standard academic convention. |
| `tsmom_ens_4a` | 0.89 | 0.88 | 6.5% | 10.5% | 0.50 | 4-asset (no UUP), ensembled 6/9/12m signal. |
| `riskpar4_iv` | 0.97 | 0.86 | 8.7% | 19.9% | 0.55 | Inverse-vol risk parity across SPY/TLT/GLD/DBC, monthly rebalance — no trend timing at all, pure allocation. |
| `golden_cross3` | 0.93 | 0.89 | 7.4% | 13.8% | 0.47 | 50d>200d SMA cross gates SPY/TLT/GLD, each parking in BIL when off. |
| `ibsrsi_qqq` | 0.93 | 0.90 | 7.6% | **8.0%** | **0.31** | IBS<0.20 AND RSI(2)<25 mean-reversion on QQQ above its 200d SMA — best Sharpe/maxDD/correlation combo of any mean-reversion candidate. |
| `ew3_gated` | 0.89 | 0.90 (≈tie) | 6.5% | 11.9% | **0.29** | Equal-thirds SPY/TLT/GLD, each parks in BIL below its 210d SMA — very close to SPY's own Sharpe at less than a third the correlation. |

**Best raw numbers in the whole sweep** (discovered, not curated — flagged
because it's the least like anything already proven in this repo):

| id | Sharpe | CAGR | MaxDD | Note |
|---|---|---|---|---|
| `credit_vol_qqq` | **1.13** | **18.1%** | 19.3% | Hold QQQ only when HYG > its 200d SMA (credit regime OK) AND VIX < 30 (no vol spike); else BIL. Beats its own untimed QQQ null (Sharpe 0.91) convincingly — this is the one candidate in the sweep with a real claim to *timing* alpha, not just diversification. maxDD (19.3%) is still real, so this is not "free lunch" — treat as the most interesting single follow-up, not a slam-dunk. |
| `sma_hysteresis` | 1.05 | 12.2% | 15.2% | SPY 200d-SMA switch with an asymmetric re-entry rule (must reclaim BOTH the 50d and 200d SMA to re-enter) — cuts the classic SMA-switch's whipsaw cost. |
| `vix_crush_rebound` (discovered) | 1.02 | 12.7% | 24.8% | Below-200d-SMA SPY normally sits in BIL; a VIX spike-then-crush pattern (VIX≥30 then drops 20% off its 20d high) triggers 3 weeks of half-SPY/half-BIL to catch the rebound. Beats its null (0.83) meaningfully — a real, if narrow, timing signal. |
| `qqq_voltgt12` | 1.05 | 13.2% | 17.3% | 12%-vol-targeted QQQ (scale exposure down when realized vol is high). Corr to SPY is high (0.81) — mechanically it's closer to "de-levered QQQ" than a diversifier. |
| `qqq_10m_sma` | 1.00 | 16.5% | 24.3% | Classic Faber 10-month-SMA timing, applied to QQQ instead of SPY. |
| `spy_10m_sma` | 1.00 | 11.9% | 19.1% | The original Faber GTAA single-asset rule, on SPY. |
| `spy_voltgt10` | 0.98 | 9.9% | 12.8% | 10%-vol-targeted SPY — high correlation (0.88), same caveat as `qqq_voltgt12`. |

## Tier 2 — Genuine diversifiers (low correlation, don't need to beat SPY)

Sharpe below SPY's but correlation is low enough that a small sleeve
meaningfully improves a blended book's Sharpe — the same logic that justifies
`tsmom_sleeve.py`, `sector_momentum.py`, and `dual_momentum.py` already being
live despite none of them individually "beating the index."

| id | Sharpe | MaxDD | Corr(SPY) | Note |
|---|---|---|---|---|
| `book_trend_mr` | 0.79 | 9.3% | 0.59 | 50/50 blend of the TSMOM sleeve and IBS-SPY mean reversion — an ensemble-of-ensembles; low maxDD from combining two uncorrelated signal types. |
| `dbl7_qqq` | 0.75 | 14.1% | 0.47 | Connors "Double-7s": buy QQQ at a 7-day-low close (above 200d SMA), sell at a 7-day-high close. |
| `streak3_qqq` | 0.74 | **7.0%** | **0.18** | Buy after 3 consecutive down closes (above 200d SMA), exit on the first up close or after 5 days. Lowest correlation of any equity-based candidate. |
| `gld_10m_sma` | 0.66 | 28.5% | **0.07** | Gold with Faber's 10-month-SMA switch — near-zero equity correlation, but the raw maxDD (gold's own volatility) is real. |
| `dbc_gate` | 0.56 | 25.3% | **0.15** | Broad commodities (DBC), single 12-month trend gate, else BIL. |

---

## What this does NOT show

- **No out-of-sample holdout beyond the built-in half-split check** — every
  number is in-sample over 2016–2026, same limitation as every prior sweep in
  this repo. The half-split Sharpes are reported in the full JSON for anyone
  who wants to look closer before considering deployment.
- **5bps turnover cost is a simplification** — real Alpaca paper fills will
  differ, especially on monthly-rebalance strategies with many legs.
- **`credit_vol_qqq` and `vix_crush_rebound` are the only two candidates with
  a real claim to timing alpha** (beating their own untimed null by a
  meaningful margin) — everything else in Tier 1 earns its Sharpe primarily
  through **drawdown control**, exactly like the already-deployed
  `tsmom_sleeve`. That's a legitimate portfolio-construction reason to run
  them as small sleeves, but it is not "alpha" and should not be pitched as
  such if any of these move toward live deployment.
- This sweep did not re-test anything already live or already proven dead in
  this repo (ORB, SIP-ORB, TJR, pairs, seasonality) — see `CLAUDE.md`'s
  strategy table for that history.

## Reproducibility

Full code for all 20: `research/top20/*.py`, shared library
`research/bt_lib.py`, data fetcher `research/fetch_yahoo_cache.py`. Every
number in this report was obtained by actually running these scripts (spot
re-verified during the adversarial audit pass) — see `research/README.md` to
reproduce.
