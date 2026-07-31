# Claud-Trade

Automated intraday equity trading on Alpaca. Backtest-first, paper-first.

## What this is

A replacement for the previous repo, built around one principle: **the backtest
engine is only useful if it can be proven not to lie.** Everything else is
downstream of that.

The engine is verified against synthetic random-walk data. On a driftless
random walk, both bundled strategies produce gross P&L statistically
indistinguishable from zero (t = −0.40 and −0.44), and a zero-cost control run
confirms costs aren't masking bias. An engine that shows profit on random data
has look-ahead bias; this one doesn't.

Run `python tests/test_engine.py` any time you change the engine. If the
random-walk test starts passing *profitably*, you have introduced a bug that
will otherwise cost you real money.

## Structure

```
src/costs.py       cost model -- spread, slippage, reg fees. Pessimistic by default.
src/risk.py        risk layer. Sits ABOVE strategies. Sizing + halts + kill switch.
src/engine.py      event-driven backtest. No look-ahead, pessimistic intrabar fills.
src/data.py        Alpaca bars -> parquet cache. Chronological train/test split.
src/validate.py    cost sensitivity, Monte Carlo, expectancy confidence intervals.
src/executor.py    Alpaca PAPER execution. Bracket orders only. Paper-gated.
src/journal.py     R-multiple trade log with deviation flags.
src/strategies/    orb.py, vwap_reversion.py -- mechanically defined hypotheses.
tests/             engine integrity tests. Run these.
```

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env      # PAPER KEYS ONLY
export $(cat .env | xargs)
```

## Use

```bash
python tests/test_engine.py                       # verify engine integrity first
python scripts/run_backtest.py --strategy orb \
    --symbols SPY,QQQ,AAPL,MSFT,NVDA \
    --start 2024-01-01 --end 2025-06-01           # backtest w/ OOS split + cost sweep
python scripts/run_paper.py --strategy orb        # paper session
```

## Things that will bite you

**IEX vs SIP data.** Alpaca's free tier is the IEX feed — one exchange, a low
single-digit share of consolidated volume. Volume-based filters (`rvol`) are
therefore measuring IEX participation, not market participation, and bar
highs/lows can miss prints that happened elsewhere. Backtest results on IEX
data are indicative, not authoritative. Set `--feed sip` once you have the
subscription.

**Paper fills are fake in a specific way.** Alpaca paper fills you at the quote
with no queue position, no partial fills, no adverse selection, and no market
impact. Live is worse. That gap is exactly what `slippage_bps` in `costs.py`
exists to model — do not set it to zero because it makes the numbers prettier.

**Frequency is not free.** Costs scale linearly with trade count; edge does
not. On the random-walk test, the 4-trades/day strategy burned $30,600 in
costs over 150 sessions on $100k while the 1-trade/day strategy burned $3,514.
Any high-frequency strategy has to clear a much higher bar before it makes a
dollar.

**Alpaca API change, July 6 2026.** `pattern_day_trader`, `daytrade_count`,
`last_daytrade_count`, `daytrading_buying_power` and `last_daytrading_buying_power`
are being removed following FINRA's retirement of the PDT rule (Alpaca's
Intraday Margin Framework went live June 4 2026). Use `buying_power`. If the
old repo referenced those fields, that code is dead.

**The GitHub Actions cron is in UTC.** `35 13 * * 1-5` is 09:35 ET during EDT
and 08:35 ET during EST. Adjust at the DST boundaries or the bot starts an hour
early half the year.

## Going live

Don't, yet. The gate is: out-of-sample expectancy with a 95% confidence
interval that excludes zero, over 200+ trades, at *pessimistic* cost settings,
plus a paper run whose realised expectancy lands within one standard error of
backtest. `src/executor.py` refuses to run against a non-paper endpoint by
design — that check is load-bearing, don't remove it.
