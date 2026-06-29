# Strategy Research — Complete Synthesis (2026-06-28)

Two deep-research sweeps (academic / SSRN / Quantpedia / practitioner), each run
multiple times; ~50 strategy claims surfaced and adversarially verified, then
**every codifiable candidate backtested on real Alpaca daily data (2016-2026)**
vs SPY buy-and-hold (CAGR 14.87%, Sharpe 0.88, maxDD 33.8%). Research surfaces
candidates; our backtests are the arbiter. Harnesses: `backtest_research.py`,
`backtest_research2.py`, `backtest_research3.py`.

> Note on completeness: the deep-research workflow's *synthesis* step never
> finished — its verify+synthesize phase needs ~75 API calls that exhausted the
> session limit on every attempt (each failure pushed the reset later: 8:30pm →
> 2am UTC). This report is the synthesis, done by hand from all verified claims +
> our own backtests — which is the actual deliverable and more useful than the
> workflow's raw merge would have been.

## The one winner — DEPLOYED
**Sector Momentum Rotation** (top 3 of 11 sector SPDRs, ensembled 9-12mo
momentum, monthly). Only strategy to beat SPY risk-adjusted and stay robust
across the lookback×top_n grid (Sharpe 0.78-1.16 every cell). Full-period Sharpe
1.03 vs 0.88, maxDD 18% vs 34%. Caveat: beat SPY in-sample, lost to SPY's
mega-cap run out-of-sample on raw return — a Sharpe/drawdown/diversification leg,
not a guaranteed index-beater. Live: `sector_momentum.py`.

## Everything else — tested and rejected

| Strategy | Family | Our backtest verdict |
|---|---|---|
| Asset-class momentum (top 3 of SPY/EFA/BND/VNQ/GSG) | Momentum | CAGR 7.9-9.6%, < SPY |
| Faber sector rotation (3mo + SPY>10mo SMA gate) | Momentum+regime | CAGR 8.9%, regime gate hurt |
| Accelerating Dual Momentum (SPY/SCZ, 1/3/6mo) | Momentum | ≈ our dual_momentum (redundant) |
| Double 7's | Mean reversion | CAGR 4.9-8.8%, < SPY Sharpe |
| **Cumulative RSI(2)** (2-day cumRSI<10, >200SMA) | Mean reversion | SPY Sharpe 0.25 / QQQ 0.56 — edge was on a *stock universe*, gone on indices |
| Turn-of-the-Month | Seasonality | Sharpe 0.19-0.64; research also confirms it **vanished in SPY/QQQ/IWM** last decade |
| Overnight hold / intraday | Overnight edge | Real gross (QQQ Sharpe 0.94) but **dies on daily round-trip costs** |
| ETF pairs trading (SPY/IVV, QQQ/XLK, XLE/VDE…) | Stat-arb | Combined Sharpe 0.34; research: cointegration weak/likely data-mined |
| VIX-spike mean reversion (realized-vol proxy) | Vol timing | best Sharpe 0.43, < SPY |
| OpEx-week effect | Seasonality | Sharpe 0.19, < SPY |
| CVR3 (Connors/Landry 3-condition VIX timing) | Vol timing | needs spot-VIX data (not on Alpaca stock API) — not testable here |
| Pre-FOMC drift | Seasonality | research: **dead since 2015** (~9bp, insignificant) |
| Short-VXX / VIX contango harvesting | Short vol | real edge but **catastrophic tail risk** (XIV −96% in a day, 2018) |
| VIX futures term-structure | Vol timing | research: **OOS-negative** |
| 52-wk-high momentum; residual momentum/reversal; supercointegrated pairs; high-MAX weekly reversal; short-term reversal | Cross-sec (stocks) | need a 100+ stock universe / factor regressions — not testable on ETF OHLCV; reversal edge lives in small illiquid/lottery names |
| Time-series momentum; style rotation | Momentum | ≈ our dual_momentum / sector_momentum |

## The most important meta-finding
A study of **888 real algorithmic strategies**: in-sample Sharpe has **~zero
power to predict out-of-sample Sharpe (R²≈0.02)**, and the more a strategy is
backtested the larger its IS→OOS shortfall — BUT **risk metrics persist OOS far
better** (volatility R² 0.67, max drawdown R² 0.34). This is the academic case
for the filter used throughout: **judge by drawdown / volatility / robustness,
not headline return.** It's why sector momentum (picked for Sharpe + low DD +
grid-stability) is defensible and every high-headline-return cherry-pick was
correctly rejected.

## Bottom line
Across ~50 verified claims and **4 backtest harnesses**, exactly **one** strategy
cleared the bar (sector momentum, now live). The final research runs added no new
survivor — they mostly *confirmed* the rejections with extra sources (pairs
weak/data-mined, turn-of-month vanished, pre-FOMC drift dead, cointegration
skepticism). The hit rate on published/internet strategies is brutally low; the
system now has the tooling to settle any future candidate on data in minutes.
