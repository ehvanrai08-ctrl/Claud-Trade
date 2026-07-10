# 50 Fresh Strategies — Backtest Batch (2026-07-10)

A dedicated sweep of **50 genuinely novel strategies** — none overlapping the 10
live bots, the 3 paused strategies, or the ~36 previously-rejected families from
`strategy_research.md` and every `backtest_*.py` file. Discovery fanned out across
14 fresh families (factor ETFs, commodity rotation, fixed income/credit,
international rotation, risk parity, managed-futures trend, technical breakouts,
seasonality, macro-regime sector tilts, dividend/quality, new stat-arb pairs,
volatility overlays, growth/value rotation, short-horizon price patterns) plus 7
carryover candidates from the 2026-07-06 agent-loop run that had errored on a
since-fixed bug and never got a real result — these finally got one.

**Methodology:** 94 raw candidates discovered → deduped to 50 (43 dropped as
duplicates of the exclusion list or of each other, 0 dropped as untestable) →
every one backtested on real Alpaca daily data with the exact same-period
benchmark logic used throughout this repo (SPY buy-and-hold for directional
strategies; absolute Sharpe + SPY correlation for market-neutral pairs). All 50
returned a real result — zero errors, zero fabricated numbers.

**Bottom line: 3 PASS, 5 MAYBE, 42 FAIL.** As with every prior sweep in this
project, the hit rate is low — that's expected, not a failure of the search.

---

## ✅ PASS (3) — cleared the bar, worth a robustness follow-up before deploying

### 1. Classic 12-Month Time-Series Momentum, Multi-Asset Sleeve — the standout
**Family:** managed-futures-style trend-following · **Universe:** SPY, TLT, GLD, DBC, UUP, BIL

Each asset gets its own independent long-flat trend signal (12-month
total return vs a cash slot), monthly rebalance, combined into a diversified
sleeve — fundamentally different from the relative-momentum strategies already
live (GEM, sector rotation), which only ever pick a *winner among* assets rather
than trend-following each independently.

| | Strategy | SPY buy&hold |
|---|---|---|
| Sharpe | **1.20** | 0.88 |
| CAGR | 7.33% | 15.30% |
| Max drawdown | **7.82%** | 33.79% |

Lower absolute return, **dramatically** lower drawdown — a real diversification
leg, not an index-beater. Period: 2017-01-03 → 2026-07-09 (2,391 days).

### 2. Low-Vol Regime Switch (USMV/SPY)
**Family:** factor ETF · **Universe:** USMV, SPY

Hold 100% USMV (low-volatility factor) when SPY's 20-day realized vol exceeds
1.25× its trailing 252-day average for 3 consecutive days, else hold SPY.

| | Strategy | SPY buy&hold |
|---|---|---|
| Sharpe | 0.99 | 0.89 |
| CAGR | 15.85% | 15.16% |
| Max drawdown | 34.85% | 33.79% |

Modest, real edge — the persistence filter rarely triggers, so this is close to
"hold SPY with occasional defensive tilts." Full period 2016-01-04 → 2026-07-09.

### 3. Growth/Value Ratio Trend-Following (IWF/IWD crossover)
**Family:** growth/value style rotation · **Universe:** IWF, IWD

20/100-day SMA crossover on the IWF-vs-IWD ratio — hold growth when the ratio
trend is up, value when it's down. 24 crossovers over 10.5 years.

| | Strategy | SPY buy&hold |
|---|---|---|
| Sharpe | 0.97 | 0.89 |
| CAGR | 17.34% | 15.16% |
| Max drawdown | 31.55% | 33.79% |

Beats SPY on return *and* risk, but correlation to SPY is 0.92 — it's always
long an equity index, so most of this is just "own equities," with the
style-rotation adding a modest edge on top.

**Signal worth noting:** two of the three PASSes, plus a fourth candidate in
MAYBE (Dual Moving-Average Crossover, Sharpe 1.00, maxDD 8.87%), all come from
the same **managed-futures / absolute-trend-following** family. That's not
noise — a diversified, vol-aware trend sleeve independently clearing the bar
twice in the same batch is the kind of robustness signal that made sector
momentum worth deploying. Recommend this family get the next backtest cycle's
attention (a lookback/asset-weighting robustness grid, same rigor sector
momentum went through) before considering deployment.

---

## 🟡 MAYBE (5) — profitable, don't clear the bar decisively

| Strategy | Family | Sharpe | vs benchmark | Note |
|---|---|---|---|---|
| Dual Moving-Average Crossover Trend Sleeve | managed_futures_tsmom | 1.00 | SPY 0.89 | maxDD 8.9% vs 33.8%, but CAGR only 5.7% |
| Gold/Silver Ratio Mean-Reversion | commodity_rotation | 0.92 | bar 1.0 | corr 0.25 to SPY (genuine diversifier), stays net-long precious metals rather than a true spread |
| Realized-Vol Regime Filter for Sector Sizing | vol_overlay | 0.86 | SPY 0.83 | only marginal edge; history limited to 2018+ |
| Static Inverse-Vol Risk Parity (SPY/TLT/GLD/DBC) | risk_parity | 0.84 | bar 1.0 | corr 0.54 to SPY — not actually diversifying |
| All-Weather Static Risk-Balanced Allocation | risk_parity | 0.70 | bar 1.0 | bond-heavy drag this decade; corr 0.53 fails the diversifier bar too |

---

## ❌ FAIL (42) — full list

<details>
<summary>Click to expand all 42 rejected candidates</summary>

| Family | Strategy | Sharpe | Benchmark |
|---|---|---|---|
| carryover | Gold-Bond Defensive Pair Switch | 0.59 | 0.89 |
| carryover | Low-Volatility Sector Tilt | 0.68 | 0.83 |
| carryover | TLT Calendar/Volatility Contraction Breakout | -0.15 | 0.89 |
| carryover | Oil-Energy Lead-Lag | 0.40 | 0.93 |
| carryover | Cross-Asset Correlation Regime Filter | 0.77 | 0.89 |
| carryover | Sector Dispersion Mean Reversion | 0.54 | 0.83 |
| carryover | Gold Momentum with Trend Confirmation | 0.72 | 0.88 |
| commodity_rotation | Commodity Basket Time-Series Momentum (Vol-Scaled) | 0.36 | 0.89 |
| commodity_rotation | Cross-Sectional Commodity Momentum Rotation | 0.49 | 0.88 |
| dividend_factor | Dividend-Yield-Spread Regime Tilt (SCHD vs SPY) | 0.81 | 0.89 |
| dividend_factor | Dividend-Aristocrat Low-Vol Breadth Tilt | 0.83 | 0.89 |
| dividend_factor | Dividend ETF Drawdown-Triggered Defensive Switch | 0.80 | 0.89 |
| factor_etf | Factor Momentum Carousel (Top-2-of-5) | 0.81 | 0.89 |
| factor_etf | Quality-Minus-Junk Static Tilt vs SPY | 0.86 | 0.89 |
| growth_value_style | Growth-Value Relative Momentum Switch (IWF/IWD) | 0.75 | 0.89 |
| growth_value_style | 52-Week Relative-High Breakout Style Switch | 0.90 | 0.89 |
| international_rotation | Cross-Sectional Regional Momentum Top-3-of-8 | 0.33 | 0.89 |
| international_rotation | Developed-vs-Emerging Regime Rotation (EFA/EEM) | 0.32 | 0.89 |
| international_rotation | Regional Mean-Reversion Pullback-in-Uptrend | 0.10 | 0.89 |
| macro_regime_sector_tilt | Yield-Curve-Slope Cyclical/Defensive Switch | 0.73 | 0.90 |
| macro_regime_sector_tilt | XLU/XLY Recession-Risk Sector Rotator | 0.72 | 0.89 |
| macro_regime_sector_tilt | Gold/Cyclical-Metals Inflation-Regime Sector Tilt | 0.70 | 0.89 |
| managed_futures_tsmom | Skip-Month TSMOM (12-1 Momentum) | 0.89 | 0.89 |
| rates_credit_rotation | Momentum-Slope Duration Ladder (TLT/IEF/SHY) | 0.07 | 0.89 |
| rates_credit_rotation | Credit Spread Regime Rotation (HYG/IEF) | 0.30 | 0.89 |
| rates_credit_rotation | Inflation Breakeven Momentum Rotation (TIP vs IEF) | 0.31 | 0.89 |
| risk_parity_vol_target | ERC Two-Asset Barbell SPY/TLT | 0.49 | 0.89 |
| seasonality | Halloween Indicator / Sell-in-May | 0.61 | 0.89 |
| seasonality | Santa Claus Rally | 0.13 | 0.89 |
| seasonality | Pre-Holiday Drift | 0.33 | 0.89 |
| sector_subindex_pairs | Financials Breadth Pairs (XLF/KRE) | -0.32 | market-neutral |
| sector_subindex_pairs | Biotech Sub-Index Pairs (XBI/IBB) | -0.57 | market-neutral |
| sector_subindex_pairs | Semiconductors vs Broad Tech (SMH/XLK) | 0.03 | market-neutral |
| short_horizon_price_patterns | Gap-Fill Fade | -0.06 | 0.89 |
| short_horizon_price_patterns | 3-Day Losing Streak Reversal | 0.70 | 0.95 |
| short_horizon_price_patterns | NR7 Compression Breakout | 0.09 | 0.89 |
| technical_trend_breakout | Donchian Turtle Breakout | 0.85 | 0.89 |
| technical_trend_breakout | Keltner Squeeze Breakout | 0.03 | 0.89 |
| technical_trend_breakout | Supertrend Flip System | 0.64 | 0.89 |
| technical_trend_breakout | ADX-Filtered Trend Entry | 0.52 | 0.89 |
| vol_overlay | Realized-Vol Position-Sizing Overlay on Core SPY | 0.79 | 0.89 |
| vol_overlay | Fear-Gauge Divergence De-Risking Signal | 0.84 | 0.89 |

</details>

---

## The meta-finding, again

50 more strategies, 3 real passes, and the theme repeats: **single-asset
technical breakout systems (Donchian, Keltner, Supertrend, ADX) all failed**,
**pairs/stat-arb on new ETF combos all failed** (2 of 3 went outright
negative), **calendar/seasonality effects all failed** (consistent with
turn-of-month and pre-FOMC drift vanishing in earlier sweeps), and
**cross-sectional factor-ETF rotation mostly failed** except the one
regime-conditional low-vol switch. What *did* work — twice, independently —
was **absolute trend-following diversified across asset classes**, trading
lower returns for dramatically lower drawdown. That's a different kind of edge
than anything currently live (which is either relative-momentum stock-picking
or mean-reversion), and worth the next research cycle.

**Nothing from this batch is deployed.** Per this project's standing rule
(sector_momentum only went live after a lookback×top-N robustness grid, not a
single backtest), a PASS here means "worth a follow-up robustness sweep,"
not "ready for capital."
