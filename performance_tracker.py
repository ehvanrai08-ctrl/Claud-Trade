"""
Performance tracker — aggregates the shared trade ledger into per-strategy
win rates and P&L so we can decide which strategies to keep, scale, or cut.
Writes performance.json and returns a human-readable summary.
"""

import json
import os
from perf import read_ledger

BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
PERFORMANCE_FILE = os.path.join(BASE_DIR, "performance.json")


def aggregate():
    trades = read_ledger()
    by_strategy = {}

    for t in trades:
        s = t["strategy"]
        d = by_strategy.setdefault(s, {
            "trades": 0, "wins": 0, "losses": 0, "breakeven": 0,
            "total_pnl": 0.0, "gross_win": 0.0, "gross_loss": 0.0,
        })
        pnl = t["pnl"]
        d["trades"]    += 1
        d["total_pnl"] += pnl
        if pnl > 0:
            d["wins"]      += 1
            d["gross_win"] += pnl
        elif pnl < 0:
            d["losses"]     += 1
            d["gross_loss"] += pnl
        else:
            d["breakeven"] += 1

    # Derived metrics
    for s, d in by_strategy.items():
        decided = d["wins"] + d["losses"]
        d["win_rate"]   = round(d["wins"] / decided * 100, 1) if decided else None
        d["avg_win"]    = round(d["gross_win"] / d["wins"], 2) if d["wins"] else 0.0
        d["avg_loss"]   = round(d["gross_loss"] / d["losses"], 2) if d["losses"] else 0.0
        # Profit factor = gross win / abs(gross loss)
        d["profit_factor"] = (
            round(d["gross_win"] / abs(d["gross_loss"]), 2)
            if d["gross_loss"] != 0 else None
        )
        d["total_pnl"]  = round(d["total_pnl"], 2)

    result = {"by_strategy": by_strategy, "total_trades": len(trades)}
    with open(PERFORMANCE_FILE, "w") as f:
        json.dump(result, f, indent=2)
    return result


def summary_text(result=None):
    if result is None:
        result = aggregate()
    by = result["by_strategy"]
    if not by:
        return "PERFORMANCE: no realized trades recorded yet."

    lines = ["PER-STRATEGY PERFORMANCE (realized trades)", "=" * 60]
    header = f"{'Strategy':16} {'Trades':>6} {'Win%':>6} {'P&L':>10} {'AvgWin':>8} {'AvgLoss':>8} {'PF':>5}"
    lines.append(header)
    lines.append("-" * 60)
    # Rank by total P&L descending
    for s, d in sorted(by.items(), key=lambda x: -x[1]["total_pnl"]):
        wr = f"{d['win_rate']}" if d["win_rate"] is not None else "—"
        pf = f"{d['profit_factor']}" if d["profit_factor"] is not None else "—"
        lines.append(
            f"{s:16} {d['trades']:>6} {wr:>6} {d['total_pnl']:>+10.2f} "
            f"{d['avg_win']:>8.2f} {d['avg_loss']:>8.2f} {pf:>5}"
        )
    lines.append("=" * 60)

    # The call: best and worst by total P&L
    ranked = sorted(by.items(), key=lambda x: -x[1]["total_pnl"])
    best   = ranked[0]
    worst  = ranked[-1]
    lines.append(f"Best performer:  {best[0]} (${best[1]['total_pnl']:+.2f})")
    lines.append(f"Worst performer: {worst[0]} (${worst[1]['total_pnl']:+.2f})")
    return "\n".join(lines)


if __name__ == "__main__":
    print(summary_text())
