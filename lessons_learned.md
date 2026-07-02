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
