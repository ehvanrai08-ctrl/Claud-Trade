# Improvement Backlog

Continuously maintained by `project_optimizer.py`. Priority: P1 = high impact / low risk, P2 = medium, P3 = nice-to-have. Capped at 40 items.

_Last run: 2026-07-16 — 2 patch(es) auto-applied, 3 new idea(s) filed._

- [P1] The `agent_loop.py` optimizer step derives `strategy_name` from the backtested candidate name via lowercase+underscore, but `parameter_optimizer.py` expects a real registered strategy name — verify/whitelist so it never invokes the optimizer on a non-existent strategy.
- [P1] `broker.py` swallows all HTTP failures by returning falsy/`[]`/`None` with no logging; add a shared `_log_http_error` in `_trade`/`_data` on non-ok responses so silent API outages are diagnosable.
- [P1] Add retry-with-backoff to `broker.py` requests (currently a single `requests.request` with no retry) — transient 429/503 from Alpaca currently fail the whole bot run.
- [P1] `dca_index.py`, `backtest.py`, and others read `config["ALPACA_API_KEY"]` with `[]` indexing which raises `KeyError` if `.env` is missing a key; standardize on `.get()` with a clear startup error message.
- [P1] `congress_disclosures._senate_filings` unpacks each search row with `for first, last, _filer, link, filed in rows` assuming exactly 5 columns — if the Senate eFD JSON schema adds/removes a column this raises `ValueError` and kills the whole Senate fetch; index defensively and log on shape mismatch.
- [P1] `broker.fill_price` returns `None` both when an order never fills and when polling errors out — callers can't distinguish "still pending" from "failed"; add a distinct sentinel or log the terminal order status (e.g. `rejected`/`canceled`) so partial fills and rejections are diagnosable.
- [P1] `capital_allocator.get_weight` catches bare `Exception` and returns 1.0 silently — a corrupted `capital_weights.json` would go unnoticed while every bot quietly reverts to base sizing; log the failure once so it's diagnosable.
- [P1] `capital_allocator.compute_weights` reads `t["strategy"]`/`t["pnl"]` from ledger records without guarding for missing keys — a single malformed-but-valid-JSON record aborts the whole daily allocation; use `.get()` with skip+log.
- [P2] Refactor the four large strategy files (`tjr_strategy.py`, `sip_orb.py`, `wheel_strategy.py`, `post_market_analysis.py is protected`) — extract shared indicator math (RSI, SMA, IBS) into a single `indicators.py` module; `backtest.py` duplicates `rsi()`/`ibs()` already present elsewhere.
- [P2] `congress_disclosures.py` `_house_filings` uses `next(n for n in z.namelist() if n.endswith(".txt"))` which raises `StopIteration` on an unexpected ZIP layout — wrap and log gracefully.
- [P2] Add a unit test for `capital_allocator._raw_weight` boundary behavior (pf=0.5, 1.0, 2.0, consec-loss trigger) — pure function, high leverage, currently untested.
- [P2] `perf.read_ledger()` silently drops malformed lines with a bare `except`; count and report dropped lines so ledger corruption is visible in `performance_tracker`.
- [P2] `archive_logs.rotate()` reads entire log into memory (`f.read()`) — for a 512KB threshold this is fine, but add a guard/streaming copy in case `MAX_LOG_BYTES` is raised later.
- [P2] Add `Sharpe` denominator guard test to `backtest.summarize` — `statistics.stdev` on a single-element non-`>1` list path is handled, but add explicit test for the zero-stdev branch.
- [P2] `perf.read_ledger` and `capital_allocator.compute_weights` assume every ledger record has a `"pnl"`/`"strategy"` key — a truncated/partial JSONL line that still parses (e.g. `{}`) will `KeyError` downstream; validate required keys in `read_ledger` and drop+count invalid records.
- [P2] `dca_index.load_state`/`save_state` have no exception handling around file I/O and JSON parsing — a corrupted `dca_state.json` crashes the run and could lead to a double-buy on next attempt; wrap in try/except with a safe default and log.
- [P2] Extract the duplicated Alpaca `.env` loading + header construction (present verbatim in `dca_index.py`, `backtest.py`, and others) into a shared `alpaca_config.py` helper that validates required keys once with a clear startup error.
- [P2] `backtest.summarize` computes `days = sum(len(b) for b in bars_by_symbol.values()) / len(bars_by_symbol)` — if a symbol was skipped and `bars_by_symbol` is empty this raises `ZeroDivisionError`; guard the empty-universe case up front.
- [P2] Add unit tests for `perf.record_trade`/`read_ledger` round-trip including malformed-line resilience, and for `performance_tracker.aggregate` win-rate/profit-factor math on a known fixture ledger.
- [P2] `congress_disclosures._save_cache` writes with `json.dump` directly to `CACHE_FILE` — a crash mid-write corrupts the cache; write to a temp file and `os.replace` for atomic persistence (same for `agent_loop.write_cache` and `capital_allocator` weights).
- [P2] `perf.record_trade` calls `float(pnl)` unguarded inside the record dict build — a non-numeric `pnl` raises before the try/except protecting file I/O; move the coercion inside the guarded block or validate up front.
- [P3] `agent_loop.py` uses `datetime.utcnow()` (deprecated in 3.12) — migrate to `datetime.now(timezone.utc)` consistent with `perf.py`/`archive_logs.py`.
- [P3] `dca_index.py` imports `date` inside `run()` mid-function — hoist to module-level import for clarity.
- [P3] Add a `--dry-run` flag surfaced consistently across all bots (currently only `dca_index.py` reads `DRY_RUN` env) and document it.
- [P3] Cache `fetch_daily`/`get_bars` results to a local parquet/json during backtests to avoid re-downloading identical Alpaca history across the many `backtest_*.py` scripts.
- [P3] `performance_tracker.summary_text` crashes if `ranked` is non-empty but all P&L equal — add tie-break note; also guard `best`/`worst` when only one strategy exists.
- [P3] `capital_allocator.summary_text` builds a fixed-width bar `"█" * int(w * 10)` but formats with `{bar:20}` — bars for weight 2.0 (20 chars) exactly fill and larger clamp caps are fine, but document/clamp explicitly so a future weight cap above 2.0 doesn't misalign the table.
- [P3] `broker.Broker` methods construct headers/URLs at import time from module-level `_config`; add a lightweight startup validation (`Broker().healthcheck()` hitting `/account`) callable by bots to fail fast on bad credentials rather than silently returning empty dicts all day.
- [P3] `archive_logs.rotate` appends rotated content to a dated archive file but never compresses — gzip the archive (`.log.gz`) to further cut committed repo size for large rotations.
- [P3] `agent_loop.main` limit parsing does `int(sys.argv[idx + 1])` with no validation — a non-integer arg raises `ValueError` with an unhelpful traceback; validate and print usage.
- [P3] `capital_allocator.summary_text` bar `"█" * int(w * 10)` formatted as `{bar:20}` will misalign if the weight cap is ever raised above 2.0 — clamp the bar length explicitly to keep the table aligned.

---

## Run log 2026-07-16
- ✅ Auto-applied: dca_index.py, dca_index.py
