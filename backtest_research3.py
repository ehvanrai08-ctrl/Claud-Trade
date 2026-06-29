"""
Backtest of the NEW codifiable candidates surfaced by the final research runs.
Pure Python on Alpaca daily data (zero API budget). Same discipline: vs SPY
buy-and-hold, in/out-of-sample where it matters.

  1. CUMULATIVE RSI(2) (Connors) — buy SPY/QQQ at next open when the 2-day sum of
     RSI(2) < 10 AND close > 200-day SMA; exit at next open when 2-day cum RSI(2)
     > 65. Reported on large/mega-cap stocks: 26.6% CAGR, Sharpe 1.18 (1998-2024).
     A deeper-oversold variant of our existing single-day RSI(2) bot.
  2. ACCELERATING DUAL MOMENTUM (ADM) — SPY vs SCZ, score = avg(1,3,6-mo total
     return); hold the higher if positive, else the better of TLT/TIP. Monthly.
     (A close cousin of our dual_momentum/GEM — included for completeness.)

Run: python backtest_research3.py
"""

import time
from backtest_research import fetch_daily, perf_from_daily_returns, show


def rsi(closes, i, period=2):
    """RSI(period) using the close at index i (needs period+1 history)."""
    if i < period:
        return None
    gains = losses = 0.0
    for k in range(i - period + 1, i + 1):
        ch = closes[k] - closes[k - 1]
        gains += max(ch, 0); losses += max(-ch, 0)
    ag, al = gains / period, losses / period
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def cumulative_rsi2(bars, cum_buy=10, cum_sell=65, sma_trend=200):
    """Long-flat daily return series for the cumulative-RSI(2) rule."""
    closes = [b["c"] for b in bars]
    rsis = [rsi(closes, i, 2) for i in range(len(closes))]
    rets, holding = [], False
    for i in range(len(bars)):
        r = closes[i] / closes[i-1] - 1 if (holding and i > 0) else 0.0
        rets.append(r)
        if i < sma_trend or rsis[i] is None or rsis[i-1] is None:
            continue
        cum = rsis[i] + rsis[i-1]
        sma = sum(closes[i-sma_trend+1:i+1]) / sma_trend
        if not holding and cum < cum_buy and closes[i] > sma:
            holding = True
        elif holding and cum > cum_sell:
            holding = False
    return rets


if __name__ == "__main__":
    print("Fetching SPY/QQQ daily…")
    data = {s: fetch_daily(s) for s in ("SPY", "QQQ")}
    time.sleep(0.1)
    spy = data["SPY"]
    spy_bh = [spy[i]["c"]/spy[i-1]["c"]-1 for i in range(1, len(spy))]
    bench = perf_from_daily_returns(spy_bh)
    print(f"SPY {spy[0]['d']}→{spy[-1]['d']}\n")
    show("SPY buy & hold (benchmark)", bench)

    print("\n── Cumulative RSI(2) (long-flat) ──")
    for s in ("SPY", "QQQ"):
        rets = cumulative_rsi2(data[s])
        m = perf_from_daily_returns(rets)
        # exposure: fraction of days in-market
        exposed = sum(1 for r in rets if r != 0) / len(rets) * 100
        show(f"CumRSI2 {s} (~{exposed:.0f}% exposed)", m, bench)

    print("\nLong-flat → judge vs SPY buy&hold. Low exposure = lower drawdown but "
          "also lower compounding; check Sharpe, not just CAGR.")
