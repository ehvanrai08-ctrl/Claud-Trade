# Improvement Backlog

Continuously maintained by `project_optimizer.py`. Priority: P1 = high impact / low risk, P2 = medium, P3 = nice-to-have. Capped at 40 items.

_Last run: 2026-07-11 — 2 patch(es) auto-applied, 15 new idea(s) filed._

- [P1] The `agent_loop.py` optimizer step derives `strategy_name` from the backtested candidate name via lowercase+underscore, but `parameter_optimizer.py` expects a real registered strategy name — verify/whitelist so it never invokes the optimizer on a non-existent strategy.
- [P1] `broker.py` swallows all HTTP failures by returning falsy/`[]`/`None` with no logging; add a shared `_log_http_error` in `_trade`/`_data` on non-ok responses so silent API outages are diagnosable.
- [P1] Add retry-with-backoff to `broker.py` requests (currently a single `requests.request` with no retry) — transient 429/503 from Alpaca currently fail the whole bot run.
- [P1] `dca_index.py`, `backtest.py`, and others read `config["ALPACA_API_KEY"]` with `[]` indexing which raises `KeyError` if `.env` is missing a key; standardize on `.get()` with a clear startup error message.
- [P2] Refactor the four large strategy files (`tjr_strategy.py`, `sip_orb.py`, `wheel_strategy.py`, `post_market_analysis.py is protected`) — extract shared indicator math (RSI, SMA, IBS) into a single `indicators.py` module; `backtest.py` duplicates `rsi()`/`ibs()` already present elsewhere.
- [P2] `congress_disclosures.py` `_house_filings` uses `next(n for n in z.namelist() if n.endswith(".txt"))` which raises `StopIteration` on an unexpected ZIP layout — wrap and log gracefully.
- [P2] Add a unit test for `capital_allocator._raw_weight` boundary behavior (pf=0.5, 1.0, 2.0, consec-loss trigger) — pure function, high leverage, currently untested.
- [P2] `perf.read_ledger()` silently drops malformed lines with a bare `except`; count and report dropped lines so ledger corruption is visible in `performance_tracker`.
- [P2] `archive_logs.rotate()` reads entire log into memory (`f.read()`) — for a 512KB threshold this is fine, but add a guard/streaming copy in case `MAX_LOG_BYTES` is raised later.
- [P2] Add `Sharpe` denominator guard test to `backtest.summarize` — `statistics.stdev` on a single-element non-`>1` list path is handled, but add explicit test for the zero-stdev branch.
- [P3] `agent_loop.py` uses `datetime.utcnow()` (deprecated in 3.12) — migrate to `datetime.now(timezone.utc)` consistent with `perf.py`/`archive_logs.py`.
- [P3] `dca_index.py` imports `date` inside `run()` mid-function — hoist to module-level import for clarity.
- [P3] Add a `--dry-run` flag surfaced consistently across all bots (currently only `dca_index.py` reads `DRY_RUN` env) and document it.
- [P3] Cache `fetch_daily`/`get_bars` results to a local parquet/json during backtests to avoid re-downloading identical Alpaca history across the many `backtest_*.py` scripts.
- [P3] `performance_tracker.summary_text` crashes if `ranked` is non-empty but all P&L equal — add tie-break note; also guard `best`/`worst` when only one strategy exists.

---

## Run log 2026-07-11
- ✅ Auto-applied: agent_loop.py, capital_allocator.py
