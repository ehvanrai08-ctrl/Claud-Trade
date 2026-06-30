# Improvement Backlog

Continuously maintained by `project_optimizer.py`. Priority: P1 = high impact / low risk, P2 = medium, P3 = nice-to-have. Capped at 40 items.

_Last run: 2026-06-30 — 4 patch(es) auto-applied, 15 new idea(s) filed._

- [P1] `copy_trader.get_congress_trades()` can raise (raise_for_status) but `run()` doesn't catch it — wrap the Quiver fetch in try/except so a bad API response doesn't crash the hourly job.
- [P1] Centralize all bots on `broker.py` instead of duplicating raw `requests` calls + headers (copy_trader, dca_index, dual_momentum each re-implement market_is_open/order placement) — reduces drift and the `class` vs `asset_class` bug class.
- [P1] `market_is_open()` in copy_trader/dca_index/dual_momentum calls `r.json()["is_open"]` with no `.ok` check or KeyError guard — a clock API hiccup throws an unhandled exception; mirror broker's safe `.get("is_open", False)`.
- [P1] Add a unit-test harness for indicator functions (rsi, ibs, momentum_score) with known fixtures to guard against silent regressions in the backtest/strategy math.
- [P2] `backtest.py` recomputes `closes = [b["c"] for b in bars[:i+1]]` every iteration (O(n²)); maintain a rolling closes list and rolling SMA sum for large-window backtests.
- [P2] Refactor the three large files (post_market_analysis is protected) — split tjr_strategy.py and sip_orb.py into indicator/signal/execution modules to ease testing.
- [P2] `capital_allocator._profit_factor` returns 2.0 when gross_loss==0 but gross_win>0 — cap could understate a flawless strategy; consider distinguishing "no losses" vs "PF=2" and document the clamp.
- [P2] `dual_momentum.decide_target()` fetches each symbol's bars serially; use `broker.get_bars_multi` to batch the SPY/EFA/BIL/AGG fetch into one call.
- [P2] Add a global kill-switch / risk_guard check at the top of every bot's `run()` (equity drawdown threshold) so a cascade of losses pauses all trading, not just per-strategy weight cuts.
- [P2] `copy_trader` sizes sells by notional but a partial-share position may not support notional sell — verify and fall back to qty-based close via `broker.close_position`.
- [P3] `archive_logs.rotate()` reads entire log into memory before truncating; stream/copy in chunks for very large logs to bound memory.
- [P3] `agent_loop` uses `datetime.utcnow()` (deprecated) — migrate to `datetime.now(timezone.utc)` consistent with newer files.
- [P3] Add `--dry-run` flag to copy_trader/dca_index/dual_momentum to log intended orders without placing them, easing CI validation.
- [P3] Document the `ALLOC` recycling assumption in backtest.py summary more prominently (single unit recycled vs concurrent positions) to avoid misreading CAGR.
- [P3] `perf.record_trade` swallows all exceptions silently — at least emit to stderr once so a broken ledger path is discoverable.

---

## Run log 2026-06-30
- ✅ Auto-applied: agent_loop.py, agent_loop.py, agent_loop.py, agent_loop.py
