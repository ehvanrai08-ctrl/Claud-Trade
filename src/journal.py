"""Trade journal in R-multiples.

A P&L number tells you almost nothing. R-multiples plus a deviation flag tell
you whether the STRATEGY lost or whether YOU did -- and those require opposite
responses. Most traders never separate the two and so learn nothing from
either.
"""
import os, csv
from datetime import datetime

FIELDS = ["timestamp", "symbol", "side", "tag", "qty", "ref_price", "stop",
          "target", "risk_dollars", "order_id", "exit_price", "exit_reason",
          "net_pnl", "r_multiple", "deviation_flag", "note"]


class Journal:
    def __init__(self, path="results/live_journal.csv"):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if not os.path.exists(path):
            with open(path, "w", newline="") as f:
                csv.DictWriter(f, FIELDS).writeheader()

    def _write(self, row):
        with open(self.path, "a", newline="") as f:
            csv.DictWriter(f, FIELDS).writerow({k: row.get(k, "") for k in FIELDS})

    def log_entry(self, symbol, sig, qty, ref_price, order_id):
        self._write({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "symbol": symbol, "side": sig.side, "tag": sig.tag, "qty": qty,
            "ref_price": round(ref_price, 4), "stop": round(sig.stop, 4),
            "target": round(sig.target, 4),
            "risk_dollars": round(abs(ref_price - sig.stop) * qty, 2),
            "order_id": str(order_id), "deviation_flag": "0",
            "note": "auto"})
