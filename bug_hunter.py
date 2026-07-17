"""
Bug Hunter — behavioral/data anomaly detection + gated auto-fix.
==================================================================
`project_optimizer.py` reviews CODE for mechanical smells (unused imports,
missing error handling); `post_market_analysis.py` reviews TODAY's trading
outcome. Neither looks for the class of bug found manually on 2026-07-17:
a strategy re-logging the same trade every tick for hours (282 duplicate
ledger entries from one real event), and a report mislabeled with the wrong
calendar date that then silently blocked the real report from ever being
written. Those bugs are invisible to py_compile/pyflakes and don't show up
as "the code looks wrong" — only as "the DATA looks wrong."

This script runs deterministic checks against the live ledger, reports, and
performance files (no API needed, always useful — same philosophy as
`local_analysis()` and `survey_codebase()`), then:

1. Mechanical anomalies (duplicate ledger entries, stale performance.json)
   get auto-repaired directly — no LLM needed, same operation done by hand
   on 2026-07-17: dedupe, then regenerate performance.json/capital_weights.json
   from the corrected ledger (the source of truth).
2. Anomalies that trace to a specific strategy file's logic get a Claude
   diagnosis + patch, applied behind the SAME two gates project_optimizer.py
   uses (py_compile, pyflakes "undefined name"), with the same protected-file
   boundary — this script can never patch itself, post_market_analysis.py,
   or project_optimizer.py. Report-mislabeling and missing-report issues
   trace to post_market_analysis.py (protected) and are always just flagged,
   never auto-patched — exactly like a human had to fix it today.

Runs daily via GitHub Actions, after the post-market report.
"""

import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import dotenv_values

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
config   = dotenv_values(f"{BASE_DIR}/.env")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TODAY = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

LEDGER_FILE      = f"{BASE_DIR}/trades_ledger.jsonl"
PERFORMANCE_FILE = f"{BASE_DIR}/performance.json"
REPORTS_DIR      = f"{BASE_DIR}/reports"
LESSONS_FILE     = f"{BASE_DIR}/lessons_learned.md"

# This script must never patch itself or the other two protected engines —
# same recoverability guarantee project_optimizer.py already relies on.
PROTECTED       = {"bug_hunter.py", "project_optimizer.py", "post_market_analysis.py"}
NEVER_AUTOPATCH = PROTECTED | {"requirements.txt"}

# Duplicate-trade detection: N+ identical (strategy, symbol, note, pnl)
# records within this many minutes of each other is not "a busy bot," it's
# a re-recording bug — a strategy's own tick interval is 60s (market_monitor)
# or slower (everything else), so 3+ IDENTICAL trades within 15 minutes has
# no legitimate explanation.
DUP_WINDOW_MIN   = 15
DUP_MIN_COUNT    = 3

# Strategy name -> file that calls record_trade() for it. Used to fetch the
# right source for Claude's diagnosis. Kept in sync manually with CLAUDE.md's
# strategy table (no reflection trick — explicit is safer for something that
# decides which file gets patched).
STRATEGY_FILE = {
    "tsla_trailing":   "market_monitor.py",
    "trend_basket":    "trend_basket.py",
    "wheel":           "wheel_strategy.py",
    "copy_trader":     "copy_trader.py",
    "tjr":             "tjr_strategy.py",
    "mean_reversion":  "mean_reversion.py",
    "dual_momentum":   "dual_momentum.py",
    "ibs":             "ibs_strategy.py",
    "rsi2":            "rsi2_strategy.py",
    "dca":             "dca_index.py",
    "sector_momentum": "sector_momentum.py",
    "superinvestor":   "superinvestor_copy.py",
    "emerging_growth": "emerging_growth.py",
    "tsmom":           "tsmom_sleeve.py",
    "credit_vol_qqq":  "credit_vol_qqq.py",
    "orb":             "orb_strategy.py",
    "sip_orb":         "sip_orb.py",
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def read_file(path):
    with open(path) as f:
        return f.read()


def read_ledger():
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


def _parse_ts(ts):
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None


# ── Detector 1: duplicate trade recording ───────────────────────────────────

def detect_duplicate_trades():
    """Group ledger entries by (strategy, symbol, note, pnl); within each
    group, find runs of >=DUP_MIN_COUNT entries all within DUP_WINDOW_MIN of
    each other. Returns a list of anomaly dicts, each with the exact ledger
    line indices to remove (all but the first) for a safe auto-repair."""
    trades = read_ledger()
    groups = defaultdict(list)
    for i, t in enumerate(trades):
        key = (t.get("strategy"), t.get("symbol"), t.get("note"), t.get("pnl"))
        groups[key].append((i, t))

    anomalies = []
    for key, entries in groups.items():
        if len(entries) < DUP_MIN_COUNT:
            continue
        entries.sort(key=lambda e: e[1].get("ts", ""))
        run, runs = [entries[0]], []
        for idx, rec in entries[1:]:
            prev_ts = _parse_ts(run[-1][1].get("ts", ""))
            cur_ts  = _parse_ts(rec.get("ts", ""))
            if prev_ts and cur_ts and (cur_ts - prev_ts) <= timedelta(minutes=DUP_WINDOW_MIN):
                run.append((idx, rec))
            else:
                if len(run) >= DUP_MIN_COUNT:
                    runs.append(run)
                run = [(idx, rec)]
        if len(run) >= DUP_MIN_COUNT:
            runs.append(run)

        for run in runs:
            strategy, symbol, note, pnl = key
            anomalies.append({
                "type":        "duplicate_trades",
                "strategy":    strategy,
                "symbol":      symbol,
                "note":        note,
                "pnl":         pnl,
                "count":       len(run),
                "first_ts":    run[0][1].get("ts"),
                "last_ts":     run[-1][1].get("ts"),
                "remove_idx":  [idx for idx, _ in run[1:]],  # keep the first
            })
    return anomalies


def repair_duplicate_trades(anomalies):
    """Deterministic, mechanical repair: drop the flagged duplicate lines,
    keep the first (real) occurrence of each run, regenerate
    performance.json and capital_weights.json from the corrected ledger —
    same three steps done by hand on 2026-07-17."""
    if not anomalies:
        return False
    drop = set()
    for a in anomalies:
        drop.update(a["remove_idx"])
    if not drop:
        return False

    with open(LEDGER_FILE) as f:
        lines = f.readlines()
    kept = [ln for i, ln in enumerate(lines) if i not in drop]
    with open(LEDGER_FILE, "w") as f:
        f.writelines(kept)

    from performance_tracker import aggregate
    aggregate()  # writes performance.json
    try:
        from capital_allocator import compute_weights
        compute_weights()  # writes capital_weights.json
    except Exception:
        pass  # non-fatal — performance.json is the important one
    return True


# ── Detector 2: report generated before that trading day's session ended ───
#
# NOTE: comparing the filename date to a DATE: line INSIDE the report doesn't
# work — both are written from the same TODAY variable in
# post_market_analysis.py, so they can never disagree; that was this
# detector's original (broken) design, caught by testing it against the very
# incident it was meant to detect. The real signature of a mislabeled report
# is a generation timestamp that couldn't possibly reflect a completed
# session for the date it claims — which needs the self-contained
# "_Generated: <UTC ISO>_" line post_market_analysis.py now writes into every
# report. Older reports predating that line are skipped (unverifiable, not
# assumed guilty).

GENERATED_RE = re.compile(r"_Generated:\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z UTC_")
MARKET_CLOSE_ET_HOUR = 16   # 4 PM ET


def detect_report_date_mismatches():
    if not os.path.isdir(REPORTS_DIR):
        return []
    anomalies = []
    for fname in sorted(os.listdir(REPORTS_DIR)):
        if not re.match(r"^\d{4}-\d{2}-\d{2}\.md$", fname):
            continue
        file_date = fname[:-3]
        text = read_file(f"{REPORTS_DIR}/{fname}")
        m = GENERATED_RE.search(text)
        if not m:
            continue   # pre-fix report, no self-contained timestamp to check
        generated_utc = datetime.fromisoformat(m.group(1)).replace(tzinfo=ZoneInfo("UTC"))
        generated_et  = generated_utc.astimezone(ZoneInfo("America/New_York"))
        y, mo, d = (int(x) for x in file_date.split("-"))
        market_close = datetime(y, mo, d, MARKET_CLOSE_ET_HOUR, 0, 0, tzinfo=ZoneInfo("America/New_York"))
        if generated_et < market_close:
            anomalies.append({
                "type":           "report_date_mismatch",
                "file":           fname,
                "filename_date":  file_date,
                "generated_et":   generated_et.strftime("%Y-%m-%d %H:%M %Z"),
            })
    return anomalies


# ── Detector 3: missing report for a completed prior trading day ───────────

def detect_missing_reports(lookback_days=10):
    """Weekday-only heuristic (no holiday calendar) over the last
    `lookback_days` calendar days, excluding today (which may legitimately
    not have run yet). Flags gaps — the exact failure mode of the silent
    3-day outage this repo already hit once (fixed 2026-07-10)."""
    today = datetime.now(ZoneInfo("America/New_York")).date()
    missing = []
    for d in range(1, lookback_days + 1):
        day = today - timedelta(days=d)
        if day.weekday() >= 5:   # Sat/Sun
            continue
        fname = f"{REPORTS_DIR}/{day.isoformat()}.md"
        if not os.path.exists(fname):
            missing.append(day.isoformat())
    if missing:
        return [{"type": "missing_reports", "dates": missing}]
    return []


# ── Detector 4: performance.json drifted from the ledger's true aggregate ──

def detect_performance_drift():
    """performance.json should always equal aggregate(trades_ledger.jsonl).
    A mismatch means it went stale (a bot wrote it from a partial ledger
    view, or it was hand-edited) — a purely mechanical fix: just regenerate."""
    if not os.path.exists(PERFORMANCE_FILE):
        return []
    from performance_tracker import aggregate
    committed = json.loads(read_file(PERFORMANCE_FILE))
    fresh = aggregate()  # NOTE: this overwrites performance.json as a side
    # effect of calling it — restore the committed version so the diff below
    # is against what was actually on disk before this detector ran.
    with open(PERFORMANCE_FILE, "w") as f:
        json.dump(committed, f, indent=2)

    drift = []
    for strat, fresh_d in fresh.get("by_strategy", {}).items():
        committed_d = committed.get("by_strategy", {}).get(strat, {})
        if abs(fresh_d.get("total_pnl", 0) - committed_d.get("total_pnl", 0)) > 0.01:
            drift.append({
                "strategy": strat,
                "committed_total_pnl": committed_d.get("total_pnl"),
                "true_total_pnl":      fresh_d.get("total_pnl"),
            })
    if drift:
        return [{"type": "performance_drift", "strategies": drift}]
    return []


def repair_performance_drift():
    from performance_tracker import aggregate
    aggregate()
    try:
        from capital_allocator import compute_weights
        compute_weights()
    except Exception:
        pass


# ── Claude diagnosis for strategy-code root causes ──────────────────────────

def diagnose_and_patch(anomaly):
    """Only called for duplicate_trades anomalies whose strategy maps to a
    non-protected file — asks Claude for the root cause + a patch in the
    same FILE:/OLD:/NEW: contract project_optimizer.py uses, so the same
    gate logic applies."""
    if not ANTHROPIC_KEY:
        return None, "ANTHROPIC_API_KEY not set"

    fname = STRATEGY_FILE.get(anomaly["strategy"])
    if not fname:
        return None, f"no known source file for strategy '{anomaly['strategy']}'"
    if fname in PROTECTED:
        return None, f"{fname} is protected — flagging for human review, not auto-patching"

    filepath = f"{BASE_DIR}/{fname}"
    if not os.path.exists(filepath):
        return None, f"{fname} not found"
    src = read_file(filepath)

    prompt = f"""You are debugging a live automated trading bot. A deterministic scan of
this project's shared trade ledger found {anomaly['count']} IDENTICAL trade
records for strategy "{anomaly['strategy']}", symbol {anomaly['symbol']}, all
with note="{anomaly['note']}" and pnl={anomaly['pnl']}, logged between
{anomaly['first_ts']} and {anomaly['last_ts']} — one real trade re-recorded
many times, almost certainly because the bot's per-tick loop keeps
re-detecting the same already-handled event without updating its state to
mark it as handled (this exact bug was found and fixed in market_monitor.py
on 2026-07-17: a stop-hit branch recorded the trade but only cleared/updated
state inside a conditional re-entry branch, so when re-entry didn't fire,
the very next tick saw the same filled order again and re-recorded it).

Here is the full source of {fname}, the file that calls record_trade() for
this strategy:
<file name="{fname}">
{src}
</file>

Find the exact code path that calls record_trade("{anomaly['strategy']}", ...)
with note "{anomaly['note']}" and diagnose why it would fire repeatedly for
one real event. Produce a MINIMAL, targeted patch — add whatever state
tracking is needed so the record_trade() call fires exactly once per real
event, without changing any other behavior (position sizing, entry/exit
thresholds, order types).

Output EXACTLY this format, nothing else:

=== DIAGNOSIS ===
(2-4 sentences: the exact root cause)

=== PATCH ===
FILE: {fname}
OLD:
```
<exact existing code, byte-for-byte>
```
NEW:
```
<exact replacement>
```
(If you cannot produce a patch you are highly confident matches the OLD
snippet verbatim, output "NO PATCH" instead of guessing.)"""

    import requests
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key":         ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        },
        json={
            "model":      "claude-opus-4-8",
            "max_tokens": 4096,
            "messages":   [{"role": "user", "content": prompt}],
        },
        timeout=120,
    )
    if r.ok:
        return r.json()["content"][0]["text"], None
    return None, f"Claude API error: {r.status_code} {r.text[:300]}"


def apply_patch_gated(text):
    """Same two gates as project_optimizer.py's apply_patches: py_compile,
    then pyflakes 'undefined name'. Refuses PROTECTED/NEVER_AUTOPATCH files
    and .yml/.yaml outright. Returns (applied: bool, filename, reason)."""
    import py_compile

    if "NO PATCH" in text and "FILE:" not in text:
        return False, None, "model returned NO PATCH"

    m = re.search(
        r"FILE:\s*(\S+)\nOLD:\n```[^\n]*\n(.*?)```\nNEW:\n```[^\n]*\n(.*?)```",
        text, re.DOTALL,
    )
    if not m:
        return False, None, "no patch block found in model output"

    fname, old_code, new_code = m.group(1).strip(), m.group(2), m.group(3)
    if fname in NEVER_AUTOPATCH or fname.endswith((".yml", ".yaml")):
        return False, fname, "protected/infra — filed to backlog instead"

    filepath = f"{BASE_DIR}/{fname}"
    if not os.path.exists(filepath):
        return False, fname, "file not found"
    original = read_file(filepath)
    if old_code.strip() not in original:
        return False, fname, "OLD snippet did not match current code"

    patched = original.replace(old_code.strip(), new_code.strip(), 1)
    with open(filepath, "w") as f:
        f.write(patched)

    try:
        py_compile.compile(filepath, doraise=True)
    except py_compile.PyCompileError as e:
        with open(filepath, "w") as f:
            f.write(original)
        return False, fname, f"syntax error, reverted: {str(e)[:150]}"

    flakes = subprocess.run([sys.executable, "-m", "pyflakes", filepath],
                            capture_output=True, text=True)
    if "undefined name" in (flakes.stdout + flakes.stderr):
        with open(filepath, "w") as f:
            f.write(original)
        reason = next((ln for ln in flakes.stdout.splitlines()
                       if "undefined name" in ln), "undefined name")
        return False, fname, f"undefined name, reverted: {reason[-100:]}"

    return True, fname, "applied"


# ── Orchestration ───────────────────────────────────────────────────────────

def run():
    print(f"[BUG_HUNTER] Scanning for anomalies ({TODAY})…", flush=True)

    dup_anomalies       = detect_duplicate_trades()
    date_mismatches     = detect_report_date_mismatches()
    missing_reports     = detect_missing_reports()
    drift               = detect_performance_drift()

    report_lines = [f"# Bug Hunter Report — {TODAY}\n"]
    applied_patches, flagged, repairs = [], [], []

    if dup_anomalies:
        report_lines.append("## Duplicate trade recording\n")
        for a in dup_anomalies:
            report_lines.append(
                f"- **{a['strategy']}** / {a['symbol']}: {a['count']}x identical "
                f"\"{a['note']}\" pnl={a['pnl']} between {a['first_ts']} and {a['last_ts']}"
            )
        did_repair = repair_duplicate_trades(dup_anomalies)
        if did_repair:
            repairs.append(f"Deduped {sum(len(a['remove_idx']) for a in dup_anomalies)} "
                           f"ledger entries; regenerated performance.json + capital_weights.json")
            report_lines.append(f"\n✅ Auto-repaired: {repairs[-1]}\n")

        for a in dup_anomalies:
            diag, err = diagnose_and_patch(a)
            if err:
                flagged.append(f"{a['strategy']} ({STRATEGY_FILE.get(a['strategy'], 'unknown file')}): {err}")
                continue
            applied, fname, reason = apply_patch_gated(diag)
            diagnosis = diag.split("=== PATCH ===")[0].replace("=== DIAGNOSIS ===", "").strip()
            report_lines.append(f"\n### {a['strategy']} root-cause diagnosis\n{diagnosis}\n")
            if applied:
                applied_patches.append(fname)
                report_lines.append(f"✅ Patch applied to `{fname}` and gate-verified (py_compile + pyflakes).\n")
                with open(LESSONS_FILE, "a") as f:
                    f.write(f"\n- {TODAY}: bug_hunter auto-fixed a duplicate-trade-recording bug "
                           f"in `{fname}` ({a['strategy']}): {diagnosis[:200]}\n")
            else:
                flagged.append(f"{fname or a['strategy']}: {reason}")
                report_lines.append(f"⏭️ Not auto-applied ({reason}) — filed to backlog.\n")

    if date_mismatches:
        report_lines.append("\n## Reports generated before that trading day's session ended (always flagged, never auto-fixed)\n")
        for a in date_mismatches:
            report_lines.append(
                f"- `reports/{a['file']}` claims to cover {a['filename_date']} but was generated "
                f"at {a['generated_et']} — before that day's 4 PM ET close, so it can't reflect a "
                f"completed session. Likely a late catch-up cron rollover (the 2026-07-16.md "
                f"incident). Needs a human look (renaming/merging reports risks losing data)."
            )
            flagged.append(f"report {a['file']}: generated {a['generated_et']}, before that day's close")

    if missing_reports:
        report_lines.append("\n## Missing reports for completed trading days\n")
        for a in missing_reports:
            report_lines.append(f"- No report for: {', '.join(a['dates'])}")
            flagged.append(f"missing reports: {', '.join(a['dates'])}")

    if drift:
        report_lines.append("\n## performance.json drift from ledger truth\n")
        for a in drift:
            for s in a["strategies"]:
                report_lines.append(
                    f"- {s['strategy']}: committed total_pnl={s['committed_total_pnl']} "
                    f"vs true total_pnl={s['true_total_pnl']}"
                )
        repair_performance_drift()
        repairs.append("Regenerated performance.json + capital_weights.json from ledger truth")
        report_lines.append(f"\n✅ Auto-repaired: {repairs[-1]}\n")

    if not (dup_anomalies or date_mismatches or missing_reports or drift):
        report_lines.append("\nNo anomalies found. Ledger, reports, and performance.json are consistent.\n")

    report_lines.append(f"\n---\n**Summary:** {len(applied_patches)} patch(es) auto-applied, "
                        f"{len(repairs)} mechanical repair(s), {len(flagged)} item(s) flagged for human review.\n")

    report_text = "\n".join(report_lines)
    print(report_text, flush=True)
    with open(f"{REPORTS_DIR}/bug_hunter_{TODAY}.md", "w") as f:
        f.write(report_text)

    return {
        "applied_patches": applied_patches,
        "repairs":         repairs,
        "flagged":         flagged,
    }


if __name__ == "__main__":
    run()
