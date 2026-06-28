# Strategy Research — Consolidated Findings (2026-06-28)

Two deep-research runs (academic / SSRN / Quantpedia / practitioner sources),
adversarially verified, then **every codifiable candidate backtested on real
Alpaca daily data (2016-2026) vs the SPY buy-and-hold bar** (CAGR 14.87%,
Sharpe 0.88, maxDD 33.8%). The research surfaces candidates; our backtests are
the arbiter. Harnesses: `backtest_research.py`, `backtest_research2.py`.

## Verdict table

| Strategy | Family | Verified? | Our backtest verdict | Action |
|---|---|---|---|---|
| **Sector momentum** (top 3 of 11 SPDRs, 12mo, monthly) | Cross-sec momentum | ✅ 3-0 | **Sharpe 1.03 vs 0.88, maxDD 18% vs 34%, robust across grid** | **DEPLOYED** (`sector_momentum.py`) |
| Asset-class momentum (top 3 of SPY/EFA/BND/VNQ/GSG…) | Cross-sec momentum | ✅ 3-0 | CAGR 7.9-9.6%, underperforms SPY | reject |
| Faber sector rotation (top 3 3mo + SPY>10mo SMA) | Momentum + regime | ✅ 3-0 | CAGR 8.9%, regime gate hurt | reject |
| Double 7's (close>200SMA & 7-day low → buy) | Mean reversion | ✅ | CAGR 4.9-8.8%, below SPY Sharpe | reject |
| Turn-of-the-Month (long −1/+3 window) | Seasonality | ✅ | Sharpe 0.19-0.64, underperforms | reject |
| Overnight hold / intraday (SPY/QQQ) | Overnight edge | ✅ | QQQ overnight Sharpe 0.94 gross but dies on daily costs | reject |
| ETF pairs trading (SPY/IVV, QQQ/XLK, XLE/VDE…) | Stat-arb | ✅ 3-0 | Combined Sharpe 0.34, low SPY corr but too weak | reject |
| VIX-spike mean reversion (realized-vol proxy) | Vol timing | ✅ | Best Sharpe 0.43, underperforms | reject |
| OpEx-week effect (long SPY expiry week) | Seasonality | ✅ | Sharpe 0.19, underperforms | reject |
| Pre-FOMC drift | Seasonality | ✅ 2-1 | Research-flagged **dead since 2016** (~9bp, insignificant) | skip |
| Short-VXX contango harvesting | Short vol | partial | Real edge but **catastrophic tail risk** (XIV −96% in a day, 2018) | skip |
| 52-week-high momentum; residual momentum/reversal; weekly short-term reversal | Cross-sec (stocks) | partial | Need a 100+ stock universe + factor regressions — not testable on ETF OHLCV; reversal edge concentrated in small illiquid names | not pursued |
| VIX futures term-structure | Vol timing | ✅ | Research-flagged **OOS-negative** | skip |
| Time-series momentum; style rotation | Momentum | ✅ | ≈ our dual_momentum / sector_momentum | redundant |

## The one winner
**Sector Momentum Rotation** — the only strategy to beat SPY buy-and-hold on a
risk-adjusted basis and stay robust across the lookback×top_n grid (Sharpe
0.78-1.16 every cell). Honest caveat: it beat SPY in-sample but SPY's mega-cap
run beat it out-of-sample on **raw** return (14.95% vs 21.79%) — so it's a
Sharpe / drawdown / diversification leg, not a guaranteed index-beater. Deployed
monthly, sized modestly, capital-weighted, risk-guarded.

## The most important meta-finding
A study of **888 real algorithmic strategies** found in-sample Sharpe has
**essentially zero power to predict out-of-sample Sharpe (R² ≈ 0.02)** — and the
*more* a strategy is backtested, the *larger* its IS→OOS shortfall. But **risk
metrics persist OOS far better than returns**: volatility R² 0.67, max drawdown
R² 0.34. (Source: portfolio123 888-strategy study.)

This validates the entire approach used here: **judge by drawdown / volatility /
robustness, not headline return.** It's exactly why sector momentum (chosen for
Sharpe + low DD + grid-stability) is the defensible pick, and why every
high-headline-return single-cell cherry-pick was correctly rejected.

## Bottom line
Across ~25 verified claims and 4 backtest harnesses, **exactly one** strategy
cleared the bar — same brutal hit rate as the YouTube strategies before it
(TJR, gold, ORB, SIP-ORB all rejected). The system now has the tooling to settle
any future candidate on data in minutes.
