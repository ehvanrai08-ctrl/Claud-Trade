# Improvement Backlog

Continuously maintained by `project_optimizer.py`. Priority: P1 = high impact / low risk, P2 = medium, P3 = nice-to-have. Capped at 40 items.

_Last run: 2026-07-07 — 6 patch(es) auto-applied, 15 new idea(s) filed._
- [P1] Add a unit-test harness for indicator functions (rsi, ibs, momentum_score) with known fixtures to guard against silent regressions in the backtest/strategy math.
- [P1] Centralize all bots on `broker.py` (copy_trader, dca_index, dual_momentum duplicate clock/order/price logic) — kill the `class` vs `asset_class` drift and get consistent timeouts/error handling for free.
- [P1] `agent_loop` writes `report_file` keyed only by date, so a same-day re-run silently overwrites the prior report — append a timestamp or a run counter to preserve history.
- [P1] `copy_trader.place_order` returns `None` on failure but callers append `trade_id` to `copied` only on success in some paths — audit the copied/idempotency bookkeeping so a transient order failure isn't permanently marked as "copied" and skipped forever.
- [P1] Add a `close_position`/qty-based liquidation fallback in `copy_trader` for fractional-share positions where notional sells are rejected by Alpaca, preventing permanently-stuck sell signals.
- [P2] `backtest.py` recomputes `closes = [b["c"] for b in bars[:i+1]]` every iteration (O(n²)); maintain a rolling closes list and rolling SMA sum for large-window backtests.
- [P2] Refactor the three large files (post_market_analysis is protected) — split tjr_strategy.py and sip_orb.py into indicator/signal/execution modules to ease testing.
- [P2] `capital_allocator._profit_factor` returns 2.0 when gross_loss==0 but gross_win>0 — cap could understate a flawless strategy; consider distinguishing "no losses" vs "PF=2" and document the clamp.
- [P2] `dual_momentum.decide_target()` fetches each symbol's bars serially; use `broker.get_bars_multi` to batch the SPY/EFA/BIL/AGG fetch into one call.
- [P2] `copy_trader` sizes sells by notional but a partial-share position may not support notional sell — verify and fall back to qty-based close via `broker.close_position`.
- [P2] `dual_momentum.decide_target()` fetches SPY/EFA/BIL/AGG serially — batch via `broker.get_bars_multi` to cut 4 round-trips to 1 and reduce partial-failure windows.
- [P2] `capital_allocator._profit_factor` clamps to 2.0 when `gross_loss==0 and gross_win>0` — a flawless strategy is indistinguishable from a mediocre PF=2; return a distinct sentinel or document the clamp clearly and cap the resulting weight explicitly.
- [P2] Add `--dry-run` flag to copy_trader/dca_index/dual_momentum to log intended orders without placing them, enabling safe CI smoke tests of the decision path.
- [P2] `agent_loop.run_backtest` accepts a `limit` parameter that is never used — either wire it through to `backtest_generator.py` or drop it to avoid confusion.
- [P2] `dca_index.run()` doesn't check the Monday/holiday schedule described in its docstring — it buys on any market-open day the workflow fires; add an explicit weekday/schedule guard or update the docstring to match actual behavior.
- [P2] Consolidate `DATA_HEADERS`/`HEADERS` construction (repeated verbatim in backtest.py, copy_trader.py, dca_index.py, dual_momentum.py) into `broker.py` exports to eliminate config-key drift.
- [P2] `capital_allocator.get_weight` and `compute_weights` both `json.load(open(...))` without closing the file handle — use `with open(...)` context managers to avoid leaking descriptors under frequent calls.
- [P2] `dual_momentum.get_adjusted_closes` issues `requests.get` without a `timeout`, unlike the rest of the module (15s/30s elsewhere) — add a timeout so a hung data endpoint can't stall the whole run.
- [P2] Cache the `momentum_score` inputs: `decide_target` fetches SPY/EFA/BIL closes but the docstring/AGG logic implies AGG could also be scored — verify AGG is intentionally unscored and document, or include it for consistency.

---

## Run log 2026-07-07 (manual maintenance)
- ✅ Resolved: Quiver crash guard (copy_trader), agent-loop ERROR rendering + safe metric formatting, portfolio circuit breaker in risk_guard.can_enter (-15% from peak), fuzzy backlog dedup.
- ⏭️  Dropped as stale: broker market_is_open consolidation (the optimizer already hardened all three call sites in place).
- 🧹 Collapsed near-duplicates: 40 items → 19 remain.
