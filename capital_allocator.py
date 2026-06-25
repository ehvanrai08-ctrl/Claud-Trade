"""
capital_allocator.py — dynamic notional weights per strategy.

Reads trades_ledger.jsonl, computes a rolling profit factor for each
strategy over the last LOOKBACK trades, then converts that into a
notional multiplier (0.5× to 2.0×) relative to the strategy's base.

Writes capital_weights.json — each bot reads this at startup.
Called by post_market_analysis.py at end of every trading day.

Design:
  - New / insufficient data → weight 1.0 (no change from base)
  - PF > 2.0 → weight 2.0 (double up on proven winners)
  - PF 1.0–2.0 → linearly scale 1.0–2.0
  - PF 0.5–1.0 → linearly scale 0.5–1.0 (cut losers)
  - PF < 0.5 or consecutive losses ≥ MAX_CONSEC_LOSSES → weight 0.25 (near-pause)

The multiplier is smoothed: new_weight = 0.7 * old + 0.3 * raw
so a single bad day doesn't whipsaw allocations.
"""

import json
import os
from perf import read_ledger

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
WEIGHTS_FILE = os.path.join(BASE_DIR, "capital_weights.json")

LOOKBACK           = 30   # rolling window of trades per strategy
MIN_TRADES         = 5    # need at least this many before adjusting weight
MAX_CONSEC_LOSSES  = 5    # risk-off trigger: pause a strategy on a bad streak
SMOOTH             = 0.7  # EMA weight for prior weight

STRATEGIES = [
    "tsla_trailing",
    "trend_basket",
    "wheel",
    "copy_trader",
    "tjr",
    "mean_reversion",
    "dual_momentum",
    "ibs",
    "rsi2",
    "dca",
]


def _profit_factor(trades):
    gross_win  = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss = sum(t["pnl"] for t in trades if t["pnl"] < 0)
    if gross_loss == 0:
        return 2.0 if gross_win > 0 else 1.0
    return gross_win / abs(gross_loss)


def _consec_losses(trades):
    count = 0
    for t in reversed(trades):
        if t["pnl"] < 0:
            count += 1
        else:
            break
    return count


def _raw_weight(pf, consec_losses):
    if consec_losses >= MAX_CONSEC_LOSSES:
        return 0.25
    if pf < 0.5:
        return 0.25
    if pf < 1.0:
        # linear 0.5–1.0 → weight 0.5–1.0
        return 0.5 + (pf - 0.5)
    if pf <= 2.0:
        # linear 1.0–2.0 → weight 1.0–2.0
        return pf
    return 2.0


def compute_weights():
    """Return dict of strategy → weight multiplier and write capital_weights.json."""
    ledger = read_ledger()

    # Group by strategy, keep only last LOOKBACK trades each
    by_strategy = {}
    for t in ledger:
        by_strategy.setdefault(t["strategy"], []).append(t)
    for s in by_strategy:
        by_strategy[s] = by_strategy[s][-LOOKBACK:]

    # Load prior weights for smoothing
    prior = {}
    if os.path.exists(WEIGHTS_FILE):
        try:
            prior = json.load(open(WEIGHTS_FILE)).get("weights", {})
        except Exception:
            pass

    weights = {}
    notes   = {}
    for strat in STRATEGIES:
        trades = by_strategy.get(strat, [])
        if len(trades) < MIN_TRADES:
            raw = 1.0
            note = f"insufficient data ({len(trades)}/{MIN_TRADES} trades) — default 1.0"
        else:
            pf    = _profit_factor(trades)
            cl    = _consec_losses(trades)
            raw   = _raw_weight(pf, cl)
            note  = f"PF={pf:.2f} over {len(trades)} trades, consec_losses={cl} → raw {raw:.2f}"

        old = prior.get(strat, 1.0)
        smoothed = round(SMOOTH * old + (1 - SMOOTH) * raw, 3)
        weights[strat] = smoothed
        notes[strat]   = note

    out = {"weights": weights, "notes": notes}
    with open(WEIGHTS_FILE, "w") as f:
        json.dump(out, f, indent=2)
    return out


def get_weight(strategy):
    """Read current weight for a strategy. Returns 1.0 if file missing or strategy unknown."""
    try:
        data = json.load(open(WEIGHTS_FILE))
        return data["weights"].get(strategy, 1.0)
    except Exception:
        return 1.0


def summary_text(result=None):
    if result is None:
        result = compute_weights()
    lines = ["CAPITAL WEIGHTS (dynamic allocation multipliers)", "=" * 50]
    for strat, w in result["weights"].items():
        bar  = "█" * int(w * 10)
        note = result["notes"].get(strat, "")
        lines.append(f"  {strat:16} {w:.2f}× {bar:20}  {note}")
    lines.append("=" * 50)
    return "\n".join(lines)


if __name__ == "__main__":
    print(summary_text())
