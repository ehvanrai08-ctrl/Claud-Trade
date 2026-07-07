# Improvement Backlog

Continuously maintained by `project_optimizer.py`. Priority: P1 = high impact / low risk, P2 = medium, P3 = nice-to-have. Capped at 40 items.

_Last run: 2026-07-07 — 6 patch(es) auto-applied, 15 new idea(s) filed._
_(empty — queue cleared)_

---

## Run log 2026-07-07 (queue cleared by hand)
- ✅ Resolved 14 items: tests_indicators.py (17 tests, wired into the optimizer workflow as a regression gate), copy_trader idempotency + qty-fallback sells + DRY_RUN, dca weekly guard + DRY_RUN, dual_momentum batched fetch + DRY_RUN + AGG-scoring doc, agent_loop report suffix + dead param, backtest.py O(n2) fix, capital_allocator context managers + PF-clamp rationale.
- 🚫 Won't-fix (documented): broker.py centralization & headers consolidation (call sites already hardened in place — a cross-cutting refactor of live bots is risk without behavior change); tjr/sip_orb module split (both strategies PAUSED — refactoring dead code buys nothing).
- ➕ Added this session (outside the backlog): corporate-action/split reconciliation in all three basket bots (CRWD 4:1 fix).
