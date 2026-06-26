"""
Log archiver — keep committed *.log files from growing without bound
====================================================================
Every bot appends to its own `*.log`, and those logs are committed back to the
repo each run. Over months they bloat the repo and every diff. "Context is
expensive" — archive instead of append forever.

Strategy: SIZE-BASED rotation (safe against concurrently-running bots). When a
log exceeds MAX_LOG_BYTES, its contents are moved to
`logs/archive/<name>.<DATE>.log` and the live file is truncated to a one-line
marker. Truncating (not deleting) means a bot mid-append simply continues into a
fresh empty file — no missing-file crash.

Run standalone (`python archive_logs.py`) or via post_market_analysis after the
close, when the fewest bots are active.
"""

import os
import glob
from datetime import datetime, timezone

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
ARCHIVE_DIR  = f"{BASE_DIR}/logs/archive"
MAX_LOG_BYTES = 512 * 1024   # rotate any log over 512 KB


def rotate(max_bytes=MAX_LOG_BYTES):
    """Archive every *.log over `max_bytes`. Returns a list of human-readable
    descriptions of what was archived (empty if nothing rotated)."""
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    archived = []
    for path in glob.glob(f"{BASE_DIR}/*.log"):
        try:
            if os.path.getsize(path) <= max_bytes:
                continue
            name = os.path.basename(path)[:-4]  # strip ".log"
            with open(path) as f:
                content = f.read()
            dest = f"{ARCHIVE_DIR}/{name}.{stamp}.log"
            # Append (a session may rotate the same log twice in a month — keep all).
            with open(dest, "a") as f:
                f.write(content)
            with open(path, "w") as f:
                f.write(f"# rotated {stamp} -> logs/archive/{name}.{stamp}.log\n")
            archived.append(f"{name}.log ({len(content)//1024} KB) -> archive")
        except Exception as e:
            archived.append(f"{os.path.basename(path)}: rotate failed ({e})")
    return archived


if __name__ == "__main__":
    done = rotate()
    print("\n".join(done) if done else "No logs exceeded the size threshold.")
