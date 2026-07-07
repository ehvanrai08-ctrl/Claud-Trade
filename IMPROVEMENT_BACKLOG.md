# Improvement Backlog

Continuously maintained by `project_optimizer.py`. Priority: P1 = high impact / low risk, P2 = medium, P3 = nice-to-have. Capped at 40 items.

_Last run: 2026-07-07 — 6 patch(es) auto-applied, 15 new idea(s) filed._

- [P1] `copy_trader.get_congress_trades()` can raise (raise_for_status) but `run()` doesn't catch it — wrap the Quiver fetch in try/except so a bad API response doesn't crash the hourly job.
- [P1] Centralize all bots on `broker.py` instead of duplicating raw `requests` calls + headers (copy_trader, dca_index, dual_momentum each re-implement market_is_open/order placement) — reduces drift and the `class` vs `asset_class` bug class.
- [P1] `market_is_open()` in copy_trader/dca_index/dual_momentum calls `r.json()["is_open"]` with no `.ok` check or KeyError guard — a clock API hiccup throws an unhandled exception; mirror broker's safe `.get("is_open", False)`.
- [P1] Add a unit-test harness for indicator functions (rsi, ibs, momentum_score) with known fixtures to guard against silent regressions in the backtest/strategy math.
- [P1] `copy_trader.run()` calls `get_congress_trades()` which raises on bad Quiver response — wrap in try/except so the hourly job degrades gracefully instead of crashing (also flagged; ensure caught in `run()` not just helper).
- [P1] Centralize all bots on `broker.py` (copy_trader, dca_index, dual_momentum duplicate clock/order/price logic) — kill the `class` vs `asset_class` drift and get consistent timeouts/error handling for free.
- [P1] Add a unit-test harness with known fixtures for `rsi`, `ibs`, `momentum_score`, and `_profit_factor` to lock in indicator/math correctness before any refactor.
- [P1] Add a global kill-switch: a shared `risk_guard.check_ok()` called at top of each bot `run()` that pauses all trading when account equity drawdown exceeds a threshold (portfolio-level circuit breaker vs per-strategy weight cuts).
- [P1] `agent_loop.build_report` calls `f"{perf.get('sharpe', '?'):.2f}"` — when the value is the string `'?'` (a genuine missing metric) the format spec raises `ValueError`; format defensively (coerce to float or use a helper) so a partial backtest result doesn't crash report generation.
- [P1] Add a shared `market_is_open()` in `broker.py` and have copy_trader/dca_index/dual_momentum import it — removes the three duplicated unguarded `r.json()["is_open"]` implementations and gives consistent `.ok`/exception safety.
- [P1] `agent_loop` writes `report_file` keyed only by date, so a same-day re-run silently overwrites the prior report — append a timestamp or a run counter to preserve history.
- [P1] `copy_trader.place_order` returns `None` on failure but callers append `trade_id` to `copied` only on success in some paths — audit the copied/idempotency bookkeeping so a transient order failure isn't permanently marked as "copied" and skipped forever.
- [P1] `agent_loop.build_report` formats `perf.get('sharpe','?')` with `:.2f`; add a `_fmt(v)` helper that returns `'?'` unchanged and only float-formats numbers, so partial backtest results don't crash report generation with a ValueError.
- [P1] Add a `close_position`/qty-based liquidation fallback in `copy_trader` for fractional-share positions where notional sells are rejected by Alpaca, preventing permanently-stuck sell signals.
- [P1] Wrap `copy_trader.get_congress_trades()` in try/except within `run()` so a Quiver `raise_for_status()` or timeout degrades gracefully instead of crashing the hourly job.
- [P1] Introduce a shared `broker.market_is_open()` and migrate copy_trader/dca_index/dual_momentum off their duplicated unguarded `r.json()["is_open"]` implementations.
- [P1] Add a `risk_guard.check_ok()` portfolio-level kill-switch (equity drawdown threshold) invoked at the top of each bot `run()` to pause all trading during cascading losses.
- [P2] `backtest.py` recomputes `closes = [b["c"] for b in bars[:i+1]]` every iteration (O(n²)); maintain a rolling closes list and rolling SMA sum for large-window backtests.
- [P2] Refactor the three large files (post_market_analysis is protected) — split tjr_strategy.py and sip_orb.py into indicator/signal/execution modules to ease testing.
- [P2] `capital_allocator._profit_factor` returns 2.0 when gross_loss==0 but gross_win>0 — cap could understate a flawless strategy; consider distinguishing "no losses" vs "PF=2" and document the clamp.
- [P2] `dual_momentum.decide_target()` fetches each symbol's bars serially; use `broker.get_bars_multi` to batch the SPY/EFA/BIL/AGG fetch into one call.
- [P2] Add a global kill-switch / risk_guard check at the top of every bot's `run()` (equity drawdown threshold) so a cascade of losses pauses all trading, not just per-strategy weight cuts.
- [P2] `copy_trader` sizes sells by notional but a partial-share position may not support notional sell — verify and fall back to qty-based close via `broker.close_position`.
- [P2] `backtest.py` rebuilds `closes = [b["c"] for b in bars[:i+1]]` each iteration (O(n²)); maintain a rolling window + running SMA sum for large lookbacks.
- [P2] `dual_momentum.decide_target()` fetches SPY/EFA/BIL/AGG serially — batch via `broker.get_bars_multi` to cut 4 round-trips to 1 and reduce partial-failure windows.
- [P2] `copy_trader` notional sells may fail on fractional-share positions — fall back to qty-based `close_position` / liquidate when notional close is unsupported.
- [P2] Split large modules (`tjr_strategy.py`, `sip_orb.py`) into indicator/signal/execution submodules to enable targeted unit tests (post_market_analysis is protected — leave it).
- [P2] `capital_allocator._profit_factor` clamps to 2.0 when `gross_loss==0 and gross_win>0` — a flawless strategy is indistinguishable from a mediocre PF=2; return a distinct sentinel or document the clamp clearly and cap the resulting weight explicitly.
- [P2] Add `--dry-run` flag to copy_trader/dca_index/dual_momentum to log intended orders without placing them, enabling safe CI smoke tests of the decision path.
- [P2] `agent_loop.run_backtest` accepts a `limit` parameter that is never used — either wire it through to `backtest_generator.py` or drop it to avoid confusion.
- [P2] `dca_index.run()` doesn't check the Monday/holiday schedule described in its docstring — it buys on any market-open day the workflow fires; add an explicit weekday/schedule guard or update the docstring to match actual behavior.
- [P2] Consolidate `DATA_HEADERS`/`HEADERS` construction (repeated verbatim in backtest.py, copy_trader.py, dca_index.py, dual_momentum.py) into `broker.py` exports to eliminate config-key drift.
- [P2] `capital_allocator.get_weight` and `compute_weights` both `json.load(open(...))` without closing the file handle — use `with open(...)` context managers to avoid leaking descriptors under frequent calls.
- [P2] Add a `--dry-run` flag to dual_momentum/copy_trader/dca_index that logs the intended `buy_notional`/`place_order` calls without POSTing, enabling CI smoke tests of the full decision path.
- [P2] `dual_momentum.get_adjusted_closes` issues `requests.get` without a `timeout`, unlike the rest of the module (15s/30s elsewhere) — add a timeout so a hung data endpoint can't stall the whole run.
- [P2] Cache the `momentum_score` inputs: `decide_target` fetches SPY/EFA/BIL closes but the docstring/AGG logic implies AGG could also be scored — verify AGG is intentionally unscored and document, or include it for consistency.
- [P2] Make `capital_allocator.get_weight`/`compute_weights` use `with open(...)` context managers instead of bare `json.load(open(...))` to avoid leaking file descriptors.
- [P2] Batch `dual_momentum.decide_target()` SPY/EFA/BIL fetches via `broker.get_bars_multi` to cut 3–4 serial round-trips to one and shrink the partial-failure window.
- [P2] Add `--dry-run` flags to copy_trader/dca_index/dual_momentum that log intended orders without POSTing, enabling CI smoke tests of the decision path.
- [P2] Wire the unused `limit` parameter in `agent_loop.run_backtest` through to `backtest_generator.py`, or remove it to eliminate confusion.

---

## Run log 2026-07-07
- ✅ Auto-applied: dual_momentum.py, dual_momentum.py, copy_trader.py, dual_momentum.py, copy_trader.py, copy_trader.py
