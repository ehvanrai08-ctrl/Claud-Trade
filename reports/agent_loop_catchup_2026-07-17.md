# Agent Loop Catch-Up — Backtesting Everything the Broken Loop Skipped (2026-07-17)

**Context:** `backtest_generator.py` and `parameter_optimizer.py` had a bug
(fixed same day, commit `db9999c`) where `subprocess.run(..., cwd=BASE_DIR)`
does NOT put the repo on Python's import path — Python adds the *script's*
own directory (`/tmp`) to `sys.path[0]`, not the subprocess's working
directory. Every generated backtest crashed with `ModuleNotFoundError` since
the agent loop's first run on 2026-07-06. It never produced a single verdict.

This report backtests every candidate the loop discovered across its two
runs (`reports/agent_loop_2026-07-06.md`, `reports/agent_loop_2026-07-13.md`)
that it never actually got results for — using the same look-ahead-safe
methodology as `research/top20/` (real runs on 11y of Yahoo daily bars,
5bps turnover cost, same-period SPY benchmark, untimed-null comparison
where applicable). 20 listed candidates deduped to 15 distinct
implementations (near-duplicate ideas across the two weekly runs merged;
one — USO backwardation carry timing — is genuinely infeasible from spot
ETF price data, no futures curve available, and is not faked).

## Result: 0 of 15 show real edge

| id | Sharpe | SPY Sharpe | Null Sharpe | CAGR | MaxDD | Corr(SPY) |
|---|---|---|---|---|---|---|
| `agentloop_vol_clustering` | 0.88 | 0.84 | 0.83 (untimed SPY) | 11.7% | 22.3% | 0.95 |
| `agentloop_crossasset_corr` | 0.86 | 0.88 | — | 13.2% | 33.7% | 0.97 |
| `agentloop_52wk_high_proximity` | 0.62 | 0.88 | 0.77 | 11.0% | 36.0% | 0.66 |
| `agentloop_sector_dispersion_revert` | 0.59 | 0.90 | 0.77 | 7.3% | 32.9% | 0.34 |
| `agentloop_efa_spy_relmom` | 0.57 | 0.92 | 0.70 | 8.1% | 35.0% | 0.85 |
| `agentloop_dispersion_ew_cap` | 0.57 | 0.84 | — | 9.2% | 37.3% | 0.98 |
| `agentloop_gold_trend_confirm` | 0.56 | 0.89 | 0.77 | 6.4% | 21.2% | 0.02 |
| `agentloop_lowvol_sector` | 0.42 | 0.90 | 0.77 | 5.6% | 32.4% | 0.71 |
| `agentloop_gold_bond_flip` | 0.39 | 0.88 | 0.54 | 5.1% | 32.2% | −0.09 |
| `agentloop_turn_of_month` | 0.36 | 0.83 | — | 2.6% | 15.8% | 0.44 |
| `agentloop_gold_realrate_overlay` | 0.24 | 0.89 | 0.77 | 2.1% | 31.3% | 0.06 |
| `agentloop_oil_energy_leadlag` | 0.17 | 0.84 | 0.42 | 1.5% | 39.4% | 0.30 |
| `agentloop_duration_ladder` | 0.06 | 0.92 | 0.22 | 0.1% | 27.1% | −0.24 |
| `agentloop_overnight_drift` | **−0.45** | 0.83 | — | −5.8% | 52.2% | 0.66 |
| `agentloop_tlt_nr7` | **−0.69** | 0.84 | — | −4.8% | 44.7% | −0.07 |
| ~~`agentloop_uso_backwardation`~~ | — | — | — | — | — | **infeasible: needs futures curve, not available from spot ETF price** |

No candidate beats both its own SPY benchmark and its null (where one
applies). The two closest —`vol_clustering` (0.88 vs SPY 0.84) and
`crossasset_corr` (0.86 vs SPY 0.88) — sit within noise of plain SPY
buy-and-hold and correlate with it at 0.95+, meaning they're not adding a
timing signal, they're just "fully invested in something SPY-like most of
the time." Two candidates are outright negative:

- **`agentloop_turn_of_month`** and its dead cousin **`agentloop_tlt_nr7`**
  reconfirm two families `CLAUDE.md` already flagged as proven dead from
  prior sweeps — calendar/seasonality effects and NR7 volatility-contraction
  breakouts. Fresh data, same verdict.
- **`agentloop_overnight_drift`** (hold SPY overnight only, flat intraday)
  came back sharply negative — the well-documented academic overnight-drift
  anomaly did not survive this simple implementation (raw open/close,
  5bps round-trip cost charged nightly). Worth noting this is the one
  candidate that bypasses `bt_lib.run()`'s weight-vector convention
  (overnight-only exposure isn't expressible as a daily weight), so it's
  flagged informational rather than run through the shared harness.

## Why the discovery agent's ideas underperformed the earlier sweep

The 2026-07-16 top-20 sweep (`research/top20/`) was curated *before*
backtesting to exclude families this repo had already proven dead
(pairs/stat-arb spreads, seasonality, single-asset technical breakouts,
gap fades, NR7 compression) — see its candidate-curation step. The agent
loop's discovery agent doesn't have that filter; it pulls broadly from
SSRN/Quantpedia/practitioner sources regardless of family, so a chunk of
what it proposes each week is on already-known-dead ground. That's not a
discovery-agent bug — it's a missing filter that could be added to
`strategy_discovery.py`'s prompt (worth a P2 backlog entry) now that the
downstream backtest step actually runs.

## Reproducibility

`research/agentloop_catchup/*.py`, same shared library
(`research/bt_lib.py`) and data (`research/fetch_yahoo_cache.py`) as the
top-20 sweep. All 15 scripts `py_compile` + `pyflakes` clean and were
spot-reproduced from their relocated repo path before this report was
written.
