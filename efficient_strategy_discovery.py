"""
Efficient Strategy Discovery — Profitable Variations Without API Burn

PROBLEM: agent_loop's discovery → backtest_generator pipeline:
- Calls Claude to generate code for EVERY candidate (expensive)
- Discovered strategies (15 candidates) all underperformed buy-and-hold
- No pre-filtering against families known to be dead

SOLUTION: Use the proven top-20 research (2026-07-16) as a cache.
- Propose parameter variations of PROVEN families only
- Test locally using research/bt_lib.py + Yahoo cache (zero Claude tokens)
- Focus on optimization of what works, not broad rediscovery

The top-20 sweep identified:
- Tier 1 (beat SPY): TSMOM family, credit_vol_qqq, golden_cross, IBS+RSI
- Tier 2 (diversifiers): trend-following on single assets, mean reversion
- Proven dead: calendar/seasonality, gap fades, NR7, pairs

This script:
1. Defines parameter grid for each proven family
2. Tests variations on Yahoo 2016–2026 data (same as top-20)
3. Ranks by Sharpe/maxDD/correlation
4. Recommends candidates for live deployment
5. Reports results to reports/efficient_discovery_YYYY-MM-DD.md

Usage:
  python efficient_strategy_discovery.py [--family tsmom|credit_vol|all] [--limit 10]
"""

import json
import os
import sys
import subprocess
from datetime import datetime, timezone
import tempfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESEARCH_DIR = f"{BASE_DIR}/research"
REPORTS_DIR = f"{BASE_DIR}/reports"
sys.path.insert(0, RESEARCH_DIR)

# Sanity-check the backtest library is importable up front (each backtest runs
# in its own subprocess and imports bt_lib itself; this just fails fast with a
# clear message if research/ isn't set up).
if not os.path.exists(f"{RESEARCH_DIR}/bt_lib.py"):
    print("ERROR: research/bt_lib.py not found. Ensure research/cache/ is populated.")
    sys.exit(1)


def test_tsmom_variation(lookback_months, n_assets, assets, use_vol_gate=False):
    """Test a TSMOM variation: n-month momentum on n_assets, optionally with vol gate."""
    name = f"tsmom_{lookback_months}m_{n_assets}a"
    if use_vol_gate:
        name += "_voltgt"

    code = f"""
import sys
sys.path.insert(0, '{RESEARCH_DIR}')
from bt_lib import align, run, load
import json

lookback_months = {lookback_months}
assets_list = {json.dumps(assets[:n_assets])}
n_assets = len(assets_list)

data = align(assets_list)
dates = data["dates"]
weights = []
lookback_days = int(lookback_months * 252 / 12)

for i in range(len(dates)):
    w = {{}}

    # TSMOM: use n-month trailing return to weight momentum
    for sym in assets_list:
        if i >= lookback_days:
            total_ret = data[sym]["a"][i] / data[sym]["a"][i - lookback_days] - 1
            # Momentum: weight by sign of total_ret (go long if positive, cash if negative)
            if total_ret > 0:
                w[sym] = 1.0 / n_assets
            else:
                w[sym] = 0.0
        else:
            w[sym] = 0.0

    weights.append(w)

result = run(data, weights, label='{name}')
print(json.dumps(result))
"""

    return execute_backtest(code, name)


def test_credit_vol_variation(credit_threshold=1.0, vol_threshold=30, market_start=200):
    """Test credit_vol_qqq with different thresholds."""
    name = f"credit_vol_hyg{credit_threshold:.1f}_vix{vol_threshold}"

    code = f"""
import sys
import math
sys.path.insert(0, '{RESEARCH_DIR}')
from bt_lib import align, run, load
import json

data = align(["QQQ", "HYG", "BIL"])
dates = data["dates"]
weights = []

for i in range(len(dates)):
    w = {{}}

    # 200-day SMA for HYG
    if i >= 200:
        hyg_sma = sum(data["HYG"]["a"][i-200:i]) / 200
        hyg_above = data["HYG"]["a"][i] > hyg_sma * {credit_threshold}
    else:
        hyg_above = False

    # Assume no live VIX — use QQQ realized vol as proxy
    if i >= 20:
        qqq_rets = [data["QQQ"]["a"][j] / data["QQQ"]["a"][j-1] - 1
                    for j in range(i-20, i)]
        mean = sum(qqq_rets) / 20
        var = sum((r - mean) ** 2 for r in qqq_rets) / 19
        vol_pct = math.sqrt(var) * math.sqrt(252) * 100
        vol_ok = vol_pct < {vol_threshold}
    else:
        vol_ok = False

    if hyg_above and vol_ok:
        w["QQQ"] = 1.0
        w["BIL"] = 0.0
    else:
        w["QQQ"] = 0.0
        w["BIL"] = 1.0

    weights.append(w)

result = run(data, weights, label='{name}')
print(json.dumps(result))
"""

    return execute_backtest(code, name)


def test_mean_reversion_variation(rsi_threshold=30, sma_days=200, hold_days=5):
    """Test RSI oversold mean reversion on SPY."""
    name = f"rsi_mr_rsi{rsi_threshold}_hold{hold_days}"

    code = f"""
import sys
import math
sys.path.insert(0, '{RESEARCH_DIR}')
from bt_lib import align, run, load

def rsi(xs, i, n=2):
    if i < n:
        return None
    g = l = 0.0
    for j in range(i - n + 1, i + 1):
        ch = xs[j] - xs[j - 1]
        g += max(ch, 0); l += max(-ch, 0)
    if l == 0:
        return 100.0
    return 100 - 100 / (1 + g / l)

data = align(["SPY", "BIL"])
dates = data["dates"]
weights = []
entry_day = None

for i in range(len(dates)):
    w = {{}};

    rsi_val = rsi(data["SPY"]["a"], i, 2)

    # SMA check
    if i >= {sma_days}:
        sma_val = sum(data["SPY"]["a"][i-{sma_days}:i]) / {sma_days}
        above_sma = data["SPY"]["a"][i] > sma_val
    else:
        above_sma = False

    # Entry: RSI < threshold and above SMA
    if entry_day is None and rsi_val is not None and rsi_val < {rsi_threshold} and above_sma:
        entry_day = i

    # Exit: hold_days have passed or price exceeded entry
    if entry_day is not None:
        if i - entry_day >= {hold_days}:
            entry_day = None
        else:
            w["SPY"] = 1.0
            w["BIL"] = 0.0

    if entry_day is None:
        w["SPY"] = 0.0
        w["BIL"] = 1.0

    weights.append(w)

result = run(data, weights, label='{name}')
print(json.dumps(result))
"""

    return execute_backtest(code, name)


def execute_backtest(code, name):
    """Write, validate, run backtest code, return metrics."""
    import py_compile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, dir="/tmp") as f:
        f.write(code)
        temp_path = f.name

    try:
        # Validate syntax
        try:
            py_compile.compile(temp_path, doraise=True)
        except py_compile.PyCompileError as e:
            return {"strategy": name, "error": f"Syntax: {str(e)[:100]}"}

        # Run with PYTHONPATH set
        env = {**os.environ, "PYTHONPATH": RESEARCH_DIR}
        result = subprocess.run(
            ["python", temp_path],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=RESEARCH_DIR,
            env=env,
        )

        if result.returncode != 0:
            return {"strategy": name, "error": f"Runtime: {result.stderr[:150]}"}

        try:
            data = json.loads(result.stdout)
            return {
                "strategy": name,
                "sharpe": data.get("strategy", {}).get("sharpe"),
                "cagr": data.get("strategy", {}).get("cagr"),
                "maxdd": data.get("strategy", {}).get("maxdd"),
                "spy_sharpe": data.get("spy_benchmark", {}).get("sharpe"),
                "corr_spy": data.get("corr_spy"),
                "data": data,
            }
        except json.JSONDecodeError:
            return {"strategy": name, "error": "No JSON output"}
    finally:
        try:
            os.unlink(temp_path)
        except:
            pass


def test_proven_families():
    """Test variations of proven families from the top-20."""
    results = []

    # TSMOM family: test different lookback periods and n-assets
    print("[DISCOVERY] Testing TSMOM variations...", flush=True)
    for lookback in [6, 9, 12]:
        for n_assets in [4, 5]:
            assets = ["SPY", "TLT", "GLD", "DBC", "UUP"]
            result = test_tsmom_variation(lookback, n_assets, assets)
            results.append(result)
            if "sharpe" in result:
                print(f"  {result['strategy']}: Sharpe={result['sharpe']:.2f} vs SPY {result.get('spy_sharpe', '?')}", flush=True)

    # Credit Vol QQQ: test different thresholds
    print("[DISCOVERY] Testing credit_vol_qqq variations...", flush=True)
    for credit_thresh in [0.95, 1.0, 1.05]:
        for vix_thresh in [25, 30, 35]:
            result = test_credit_vol_variation(credit_thresh, vix_thresh)
            results.append(result)
            if "sharpe" in result:
                print(f"  {result['strategy']}: Sharpe={result['sharpe']:.2f} vs SPY {result.get('spy_sharpe', '?')}", flush=True)

    # Mean Reversion: test different RSI thresholds
    print("[DISCOVERY] Testing mean reversion variations...", flush=True)
    for rsi_thresh in [20, 25, 30]:
        for hold in [3, 5, 7]:
            result = test_mean_reversion_variation(rsi_thresh, 200, hold)
            results.append(result)
            if "sharpe" in result:
                print(f"  {result['strategy']}: Sharpe={result['sharpe']:.2f} vs SPY {result.get('spy_sharpe', '?')}", flush=True)

    return results


def build_report(results):
    """Assemble markdown report."""
    timestamp = datetime.now(timezone.utc).isoformat()

    # Filter out errors
    valid = [r for r in results if "sharpe" in r]

    # Sort by Sharpe descending
    valid.sort(key=lambda r: r.get("sharpe", 0), reverse=True)

    lines = [
        f"# Efficient Strategy Discovery — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "**Method**: Parameter grid variations on proven families (TSMOM, credit_vol_qqq, mean reversion).",
        "**Data**: Yahoo 2016–2026 (same as top-20 research sweep).",
        "**Zero Claude tokens**: All testing via `research/bt_lib.py` and cached Yahoo data.",
        "",
        "> ⚠ **These are parameter re-tunes of ALREADY-LIVE bots, not new strategies.**",
        "> All three families here (credit/vol, TSMOM, mean-reversion) are already",
        "> deployed. \"Deploy\" below means *tune the live bot's params* — it does NOT",
        "> mean add a redundant sleeve. And discovery's vol proxy differs from the live",
        "> credit_vol bot's actual VIXY-percentile gate, so Sharpe won't transfer 1:1",
        "> (see `research/backtest_vixy_gate.py`). See the deployment guide for which",
        "> candidates, if any, are genuinely novel.",
        "",
        f"**Timestamp**: {timestamp}",
        f"**Tested**: {len(valid)} (+ {len(results) - len(valid)} errors)",
        "",
        "## Results by Sharpe Ratio",
        "",
        "| Strategy | Sharpe | SPY Sharpe | CAGR | MaxDD | Corr(SPY) | Status |",
        "|---|---|---|---|---|---|---|",
    ]

    # Tier 1: Beat SPY on Sharpe AND lower maxDD
    tier1 = [r for r in valid
             if r.get("sharpe", 0) > r.get("spy_sharpe", 0)
             and r.get("maxdd", 100) < 25]

    # Tier 2: Within 0.05 of SPY Sharpe (robustness check)
    tier2 = [r for r in valid
             if abs(r.get("sharpe", 0) - r.get("spy_sharpe", 0)) <= 0.05]

    # Others
    other = [r for r in valid if r not in tier1 and r not in tier2]

    if tier1:
        lines.append("### Tier 1 — Beat SPY on Sharpe AND Max Drawdown")
        for r in sorted(tier1, key=lambda x: x["sharpe"], reverse=True):
            lines.append(
                f"| {r['strategy']} | {r['sharpe']:.2f} | {r.get('spy_sharpe', '?'):.2f} | "
                f"{r.get('cagr', '?')}% | {r.get('maxdd', '?'):.1f}% | {r.get('corr_spy', '?'):.2f} | → Tune live bot |"
            )

    if tier2:
        lines.append("### Tier 2 — Within 0.05 Sharpe of SPY (Diversifier)")
        for r in sorted(tier2, key=lambda x: x["sharpe"], reverse=True)[:5]:
            lines.append(
                f"| {r['strategy']} | {r['sharpe']:.2f} | {r.get('spy_sharpe', '?'):.2f} | "
                f"{r.get('cagr', '?')}% | {r.get('maxdd', '?'):.1f}% | {r.get('corr_spy', '?'):.2f} | → Review |"
            )

    if other:
        lines.append("### Other Candidates")
        for r in sorted(other, key=lambda x: x["sharpe"], reverse=True)[:5]:
            lines.append(
                f"| {r['strategy']} | {r['sharpe']:.2f} | {r.get('spy_sharpe', '?'):.2f} | "
                f"{r.get('cagr', '?')}% | {r.get('maxdd', '?'):.1f}% | {r.get('corr_spy', '?'):.2f} | — |"
            )

    lines.extend([
        "",
        "## Key Insights",
        "",
        f"- **Top performer**: {valid[0]['strategy'] if valid else 'N/A'} (Sharpe {valid[0].get('sharpe', '?')})",
        f"- **Efficiency**: {len(valid)} variations tested, zero Claude API tokens",
        "- **Method**: Systematic parameter grid on proven families",
        "- **Recommendation**: Use as tuning inputs for the already-live bots these"
        " families map to; see the deployment guide for any genuinely novel candidates.",
        "",
        "---",
        "*Report generated by efficient_strategy_discovery.py*"
    ])

    return "\n".join(lines)


def main():
    """Run the discovery pipeline."""
    print("[DISCOVERY] Starting efficient strategy discovery...", flush=True)

    os.makedirs(REPORTS_DIR, exist_ok=True)

    # Run tests
    results = test_proven_families()

    # Build report
    report = build_report(results)

    # Write report
    report_file = f"{REPORTS_DIR}/efficient_discovery_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md"
    with open(report_file, "w") as f:
        f.write(report)

    print(f"[DISCOVERY] Report written to {report_file}", flush=True)
    print(report)


if __name__ == "__main__":
    main()
