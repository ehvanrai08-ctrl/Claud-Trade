"""
Shared performance ledger.
Every strategy calls record_trade() when it realizes a gain or loss.
Records append to trades_ledger.jsonl — one JSON object per realized trade.
performance_tracker.py aggregates this into per-strategy win rates and P&L.
"""

import json
import os
from datetime import datetime, timezone

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
LEDGER_FILE = os.path.join(BASE_DIR, "trades_ledger.jsonl")


def record_trade(strategy, symbol, pnl, note=""):
    """Append a realized-trade record to the shared ledger."""
    record = {
        "ts":       datetime.now(timezone.utc).isoformat(),
        "strategy": strategy,
        "symbol":   symbol,
        "pnl":      round(float(pnl), 2),
        "note":     note,
    }
    try:
        with open(LEDGER_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        # never let logging break a trade, but surface the failure once
        import sys
        print(f"[perf] failed to record trade to {LEDGER_FILE}: {e}", file=sys.stderr)
    return record


def read_ledger():
    """Return all recorded trades as a list of dicts."""
    if not os.path.exists(LEDGER_FILE):
        return []
    out = []
    with open(LEDGER_FILE) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    return out
