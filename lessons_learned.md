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

### 2026-07-04
- A portfolio-wide corporate-action scan (flagging any position where current price is <40% of avg_cost) should run independently of the TSLA monitor, since split-distorted positions can exist in any holding and the per-strategy warning only fires during that strategy's tick loop.
- When a short-put stop-loss threshold is breached on a market holiday, the bot cannot act until the next open; the position can gap further against you overnight. Holiday exposure on short options in volatile underlyings should be sized or hedged accordingly.

### 2026-07-06
- A portfolio-wide corporate-action scan placed only inside one strategy's tick loop will silently miss split-distorted positions in all other holdings; the scan must run at bot startup, independently of any per-strategy loop, to catch anomalies across the entire portfolio.
- Rolling a short put immediately at the 50% profit target without checking trend direction can re-enter a deteriorating position; a simple price-vs-SMA check before selling a new put avoids selling into confirmed weakness.

### 2026-07-07
- Rolling a short put immediately after an early-close trigger (same bot run) can lock in a new position at a locally unfavorable implied-volatility spike; a short cooldown window (e.g., 2 hours) between closing and re-selling gives the market time to settle and avoids compounding a bad entry.
- A corporate-action scan that silently skips positions due to null API fields will produce no log output, making it indistinguishable from "scan ran and found nothing"; always log the count of positions checked and any skipped items so scan failures are immediately visible.

### 2026-07-10
- A stop-loss exit on a short option is the highest-risk moment to re-enter — the cooldown between closing and re-selling must apply equally to stop-loss exits as to profit-taking exits, not only to the happy path.
- A degraded-gracefully API failure that repeats for days without escalation is functionally equivalent to a silent crash; consecutive-failure counting with a threshold alert is necessary to distinguish "transient blip" from "broken credential."

### 2026-07-11
- A consecutive-failure counter that is never reset on success will eventually misrepresent the severity of a new outage; always zero the counter immediately when the call succeeds.
- A 401 API failure requires human intervention (credential rotation), whereas a 5xx/timeout is self-healing via retry; error-handling code should branch on the HTTP status code and emit distinct, actionable messages for each case rather than treating all failures identically.

### 2026-07-13
- A market order placed for options may not have a `filled_avg_price` when queried within the same second; reading fill price without a short delay (or a retry loop) silently records the pre-fill bid estimate as the official sell price, causing permanent P&L tracking drift.
- A state timestamp field that is overwritten on every run (even when the guarded action did not execute) effectively disables the time-based re-evaluation it was designed to enforce; only update the "last evaluated" timestamp inside the branch where the evaluation actually runs.

### 2026-07-14
- A fill-price field marked as estimated at order time should be re-confirmed on the next bot run by re-querying the order; using an unconfirmed baseline for profit/loss threshold decisions (early close, stop loss) can trigger the wrong action at the wrong time.
- Using the same cutoff variable for both candidate scoring and trade copying silently couples two independent time windows; always name and compute them separately so changing one doesn't affect the other.

### 2026-07-15
- A stop-proximity warning that fires on every tick with no escalation path becomes log noise; consecutive-tick counters with a print-level alert after N triggers are necessary to distinguish "briefly close" from "pinned near stop for 30 minutes."
- An unconfirmed fill-price field should be re-confirmed on every bot run (at startup), not only inside the conditional branch that happens to check it — otherwise the unconfirmed estimate can persist for the entire life of a contract if the trigger condition is never met.

### 2026-07-16
- A proximity-warning escalation scheme using a fixed modulus (every N ticks) becomes log noise when a position stays pinned for hours; geometric or sparse thresholds (5, 10, 25, 50, 100, then every 100) preserve urgency without flooding output.
- A startup fill-price reconfirmation block placed *after* the early-close check defeats its own purpose — the unconfirmed baseline is already consumed before the correction can apply; always reconfirm before any decision that reads the field.
