"""
Agent Loop Orchestrator
Runs the three-agent discovery → backtest → optimize cycle weekly.

1. Discovery: surface 8–12 novel strategy candidates
2. Backtest Automation: test top 3 candidates on Alpaca data
3. Parameter Optimizer: tune the best backtested result (if any)

Outputs a weekly report to reports/agent_loop_YYYY-MM-DD.md.

Usage:
  python agent_loop.py [--limit-candidates 5]

Runs all 3 agents sequentially, collects results, writes a summary report.
Safe to re-run: only processes candidates that haven't been tested yet
(idempotency via results cache).
"""

import json
import os
import sys
import subprocess
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = f"{BASE_DIR}/reports"
CACHE_FILE  = f"{BASE_DIR}/.agent_loop_cache.json"


def read_cache():
    """Load the result cache to avoid re-testing candidates."""
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"tested_candidates": {}, "optimized_strategies": {}}


def write_cache(cache):
    """Persist the result cache."""
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


def run_discovery():
    """Step 1: Call strategy_discovery.py."""
    print("[AGENT LOOP] Running discovery…", flush=True)
    result = subprocess.run(
        ["python", f"{BASE_DIR}/strategy_discovery.py"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        print(f"[AGENT LOOP] Discovery failed: {result.stderr[:300]}", flush=True)
        return None

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        print("[AGENT LOOP] Discovery output was not valid JSON", flush=True)
        return None


def run_backtest(strategy_description, limit=5):
    """Step 2: Call backtest_generator.py for a single candidate."""
    print(f"[AGENT LOOP] Backtesting: {strategy_description[:60]}...", flush=True)
    result = subprocess.run(
        ["python", f"{BASE_DIR}/backtest_generator.py", strategy_description],
        capture_output=True,
        text=True,
        timeout=90,
    )
    if result.returncode != 0:
        print(f"[AGENT LOOP] Backtest failed: {result.stderr[:300]}", flush=True)
        return None

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        print("[AGENT LOOP] Backtest output was not valid JSON", flush=True)
        return None


def run_optimizer(strategy_name, param_description):
    """Step 3: Call parameter_optimizer.py on a live strategy."""
    print(f"[AGENT LOOP] Optimizing {strategy_name}...", flush=True)
    result = subprocess.run(
        ["python", f"{BASE_DIR}/parameter_optimizer.py", strategy_name, param_description],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        print(f"[AGENT LOOP] Optimizer failed: {result.stderr[:300]}", flush=True)
        return None

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        print("[AGENT LOOP] Optimizer output was not valid JSON", flush=True)
        return None


def build_report(discovery, backtest_results, optimizer_results):
    """Assemble the final markdown report."""
    timestamp = datetime.utcnow().isoformat()
    lines = [
        f"# Agent Loop Report — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "Automated strategy discovery, backtesting, and parameter tuning.",
        "",
        "## Discovery",
        f"**Timestamp**: {timestamp}",
        f"**Candidates Found**: {len(discovery.get('candidates', []))}",
        "",
    ]

    if discovery.get("candidates"):
        lines.append("| Name | Source | Difficulty | Status |")
        lines.append("|---|---|---|---|")
        for c in discovery.get("candidates", []):
            name = c.get("name", "?")
            source = c.get("source", "?")
            diff = c.get("difficulty", "?")
            # Check if this candidate was backtested.
            was_tested = any(bt["strategy"] == name for bt in backtest_results)
            status = "✓ Backtested" if was_tested else "pending"
            lines.append(f"| {name} | {source} | {diff} | {status} |")

    if discovery.get("rejected"):
        lines.append("")
        lines.append("### Rejected (not testable)")
        for r in discovery.get("rejected", []):
            lines.append(f"- **{r.get('name', '?')}**: {r.get('reason', '?')}")

    # Backtest results.
    lines.append("")
    lines.append("## Backtests")
    lines.append(f"**Tested**: {len(backtest_results)}")
    lines.append("")

    passed = [b for b in backtest_results if b.get("verdict") == "PASS"]
    maybe  = [b for b in backtest_results if b.get("verdict") == "MAYBE"]
    failed = [b for b in backtest_results if b.get("verdict") == "FAIL"]

    if passed:
        lines.append("### ✓ PASS (PF > 1.0, Sharpe > 0.5)")
        for b in passed:
            perf = b.get("performance", {})
            lines.append(f"- **{b.get('strategy')}**: Sharpe={perf.get('sharpe', '?'):.2f}, "
                        f"PF={perf.get('profit_factor', '?'):.2f}, "
                        f"maxDD={perf.get('max_drawdown', '?'):.1%}")

    if maybe:
        lines.append("### ~ MAYBE (profitable but low Sharpe)")
        for b in maybe:
            perf = b.get("performance", {})
            lines.append(f"- **{b.get('strategy')}**: Sharpe={perf.get('sharpe', '?'):.2f}, "
                        f"PF={perf.get('profit_factor', '?'):.2f}")

    if failed:
        lines.append("### ✗ FAIL (PF < 1.0 or negative)")
        for b in failed:
            perf = b.get("performance", {})
            reason = b.get("diagnostics", "no edge")
            lines.append(f"- **{b.get('strategy')}**: Sharpe={perf.get('sharpe', '?'):.2f}, "
                        f"PF={perf.get('profit_factor', '?'):.2f} — {reason[:80]}")

    # Optimizer results.
    if optimizer_results:
        lines.append("")
        lines.append("## Parameter Tuning")
        for opt in optimizer_results:
            verdict = opt.get("verdict", "?")
            lines.append(f"- **{opt.get('strategy')}**: {verdict}")
            if opt.get("winner"):
                winner = opt["winner"]
                lines.append(f"  - Best params: {winner.get('params')}")
                lines.append(f"  - Sharpe: {winner.get('sharpe', '?'):.2f}, "
                            f"PF: {winner.get('profit_factor', '?'):.2f}")

    lines.append("")
    lines.append("---")
    lines.append("*Report generated by agent loop.*")

    return "\n".join(lines)


def main(limit_candidates=5):
    """Run the full agent loop."""
    print("[AGENT LOOP] Starting discovery → backtest → optimize cycle", flush=True)

    os.makedirs(REPORTS_DIR, exist_ok=True)
    cache = read_cache()

    # Step 1: Discovery.
    discovery = run_discovery()
    if not discovery:
        print("[AGENT LOOP] Discovery failed, aborting.", flush=True)
        return

    candidates = discovery.get("candidates", [])[:limit_candidates]
    print(f"[AGENT LOOP] Found {len(candidates)} candidates (limited to {limit_candidates})", flush=True)

    # Step 2: Backtest top candidates (not already in cache).
    backtest_results = []
    for c in candidates:
        name = c.get("name", "")
        if name in cache.get("tested_candidates", {}):
            print(f"[AGENT LOOP] Skipping {name} (already tested)", flush=True)
            backtest_results.append(cache["tested_candidates"][name])
            continue

        desc = c.get("description", "")
        result = run_backtest(desc)
        if result:
            backtest_results.append(result)
            cache["tested_candidates"][name] = result
            write_cache(cache)

    # Step 3: Optimize the best candidate (if any passed).
    optimizer_results = []
    best = next((b for b in backtest_results if b.get("verdict") == "PASS"), None)
    if best:
        strategy_name = best.get("strategy", "").lower().replace(" ", "_")
        if strategy_name not in cache.get("optimized_strategies", {}):
            opt_result = run_optimizer(
                strategy_name,
                f"default sweep for {strategy_name}"
            )
            if opt_result:
                optimizer_results.append(opt_result)
                cache["optimized_strategies"][strategy_name] = opt_result
                write_cache(cache)
        else:
            optimizer_results.append(cache["optimized_strategies"][strategy_name])

    # Build & write report.
    report = build_report(discovery, backtest_results, optimizer_results)
    report_file = f"{REPORTS_DIR}/agent_loop_{datetime.utcnow().strftime('%Y-%m-%d')}.md"
    with open(report_file, "w") as f:
        f.write(report)

    print(f"[AGENT LOOP] Report written to {report_file}", flush=True)
    print(report)


if __name__ == "__main__":
    limit = 5
    if "--limit-candidates" in sys.argv:
        idx = sys.argv.index("--limit-candidates")
        if idx + 1 < len(sys.argv):
            limit = int(sys.argv[idx + 1])

    main(limit_candidates=limit)
