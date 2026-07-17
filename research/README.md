# research/ — 2026-07-16 top-20 profitable-strategy sweep

Ad-hoc research sweep, NOT wired into any live bot. Full writeup:
`reports/strategy_research_top20_2026-07-16.md`.

## Reproduce

```
python research/fetch_yahoo_cache.py     # pulls 45 symbols, 11y daily bars into research/cache/
python research/top20/<id>.py            # runs one strategy, prints its JSON metrics
```

Data source is Yahoo Finance's keyless `v8/finance/chart` API (this sweep ran
in an environment with no Alpaca keys). `research/bt_lib.py` is the shared,
look-ahead-safe backtest core every script in `top20/` imports — see its
docstring for the exact contract (adjusted-close returns, 5bps turnover cost,
warm-up trimming, same-period SPY benchmark, first/second-half Sharpe split).

`research/cache/*.json` is not committed (regenerate with the fetch script) —
it's 19MB of raw bars that's fully reproducible from Yahoo on demand.
