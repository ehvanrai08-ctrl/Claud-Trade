# Lessons Learned

Durable, distilled lessons the nightly bot accumulates across its (stateless)
runs. The post-market bot READS this file at the start of every run so it stops
repeating advice it has already given, and APPENDS new, genuinely-new lessons at
the end. Kept concise on purpose — better context beats more context.

Newest lessons are appended at the bottom with a date stamp.

---

### 2026-07-02
- The HWM in the trailing stop monitor was never updated during the tick loop — new intraday highs were silently ignored, meaning the trailing stop could never raise above where it was set at the last restart. Always update HWM in the tick body before computing stop levels.
- The wheel strategy's 200% stop-loss is too permissive for short puts on volatile underlyings like TSLA; a 100% loss (contract doubles) is a more appropriate exit before losses compound further toward assignment.

### 2026-07-03
- HWM must be updated before the trailing-stop calculation in the same tick, not after — otherwise the stop raise on the tick where a new high is set uses the prior bar's HWM, silently leaving one tick of value on the table every time price makes a new high.
- Positions showing >50% gap between avg cost and current price should be flagged as potential corporate actions (splits, spinoffs) rather than treated as realized losses; bots with no split-detection logic will misreport P&L and may trigger erroneous stop orders.
