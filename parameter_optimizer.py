"""
Parameter Optimizer Agent
Tunes a live strategy's parameters by sweeping variants and backtesting each.

Takes a live bot (e.g., sector_momentum.py with lookback_months=[9,10,11,12], top_n=3)
and generates a backtest harness that sweeps reasonable parameter ranges, backtests
each combo, and recommends the best configuration.

Useful for:
- Monthly tuning as market regime drifts
- Discovering if a recent performance drop is fixable by retuning
- Validating that the current params aren't stale/obsolete

Usage:
  python parameter_optimizer.py <strategy_name> "<param_description>"

Example:
  python parameter_optimizer.py sector_momentum "sweep lookback_months in [6,9,12,15] and top_n in [2,3,4]"

Returns JSON: {
  "strategy": "sector_momentum",
  "param_sweep": {
    "lookback_months": [6, 9, 12, 15],
    "top_n": [2, 3, 4]
  },
  "results": [
    {"params": {...}, "sharpe": 0.95, "profit_factor": 1.23, "max_drawdown": 0.18, "cagr": 0.11},
    ...
  ],
  "winner": {"params": {...}, "sharpe": 1.10, ...},
  "verdict": "RETUNE" | "HOLD_CURRENT" | "REVISIT_LATER"
}
"""

import json
import os
import sys
import re
import subprocess
import tempfile
import requests
from dotenv import dotenv_values

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
config   = dotenv_values(f"{BASE_DIR}/.env")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


def generate_sweep_harness(strategy_name, param_description):
    """Ask Claude to generate a backtest harness that sweeps parameters for the strategy."""
    if not ANTHROPIC_KEY:
        return None, "ANTHROPIC_API_KEY not set"

    # Read the live strategy file if it exists.
    strategy_file = f"{BASE_DIR}/{strategy_name}.py"
    if os.path.exists(strategy_file):
        with open(strategy_file) as f:
            current_code = f.read()
    else:
        current_code = "(file not found)"

    prompt = f"""You are a quantitative research expert optimizing trading strategy parameters.

STRATEGY: {strategy_name}
CURRENT CODE:
```
{current_code[:2000]}  # truncated for length
```

PARAMETER SWEEP REQUEST:
{param_description}

YOUR TASK: Generate a COMPLETE Python backtest harness that:
1. Imports from backtest_research.py: fetch_daily(), perf_from_daily_returns(), show()
2. For EACH parameter combo in the sweep, generates a modified strategy
3. Backtests each combo on Alpaca data (2016-2026)
4. Collects results: Sharpe, PF, max drawdown, CAGR
5. Outputs results as JSON-like text (each line: {{"params": {...}, "sharpe": 0.95, "profit_factor": 1.23, ...}})

Requirements:
- NO external deps beyond requests, statistics, datetime, json
- Use the same benchmark (buy-and-hold on appropriate symbol) for all variants
- Each combo should run < 10s (use 2016-2026 data, 10+ year history)
- Print ONE result dict per combo on stdout (parse these)

EXAMPLE (adapt to {strategy_name}):
```
from backtest_research import fetch_daily, perf_from_daily_returns

def sweep():
    data = fetch_daily("QQQ")
    bh = [data[i]["c"]/data[i-1]["c"]-1 for i in range(1, len(data))]
    bench = perf_from_daily_returns(bh)

    for window in [20, 30, 40]:
        for threshold in [0.3, 0.4, 0.5]:
            # run backtest with these params
            m = perf_from_daily_returns(rets)
            print(json.dumps({{"params": {{"window": window, "threshold": threshold}},
                             "sharpe": m.get("sharpe", 0), ...}}))

sweep()
```

Output ONLY the complete working Python script, ready to run.
No explanations, no markdown wrappers — just Python code."""

    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key":         ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        },
        json={
            "model":      "claude-opus-4-8",
            "max_tokens": 2500,
            "messages":   [{"role": "user", "content": prompt}],
        },
    )
    if r.ok:
        return r.json()["content"][0]["text"], None
    return None, f"Claude API error: {r.status_code} {r.text[:200]}"


def validate_and_run(code):
    """Write code, validate, run it, return (success, output, error)."""
    import py_compile

    code = code.strip()
    if code.startswith("```"):
        code = re.sub(r"^```[^\n]*\n", "", code)
        code = re.sub(r"\n```$", "", code)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, dir="/tmp") as f:
        f.write(code)
        temp_path = f.name

    try:
        try:
            py_compile.compile(temp_path, doraise=True)
        except py_compile.PyCompileError as e:
            return False, None, f"Syntax error: {str(e)[:200]}"

        result = subprocess.run(
            ["python", temp_path],
            capture_output=True,
            text=True,
            timeout=120,  # sweep can take a while
            cwd=BASE_DIR,  # so `from backtest_research import ...` resolves
        )
        if result.returncode != 0:
            return False, None, f"Runtime error: {result.stderr[:500]}"

        return True, result.stdout, None
    finally:
        try:
            os.unlink(temp_path)
        except:
            pass


def parse_results(output):
    """Parse JSON-like result dicts from stdout, one per line."""
    results = []
    for line in output.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            obj = json.loads(line)
            if "params" in obj:
                results.append(obj)
        except:
            pass
    return results


def pick_winner(results):
    """Choose the best result by Sharpe ratio (primary) and Profit Factor (tiebreaker)."""
    if not results:
        return None

    def score(r):
        sharpe = r.get("sharpe", 0)
        pf     = r.get("profit_factor", 0)
        # Weight Sharpe heavily, use PF as tiebreaker.
        return (sharpe * 100) + (max(0, pf - 1.0) * 10)

    return max(results, key=score)


def compare_to_current(current_metrics, winner):
    """Decide if we should retune based on the improvement."""
    if not current_metrics or not winner:
        return "REVISIT_LATER"

    current_sharpe = current_metrics.get("sharpe", 0)
    winner_sharpe  = winner.get("sharpe", 0)

    # If winner is >= 5% better in Sharpe, recommend retuning.
    if winner_sharpe > current_sharpe * 1.05:
        return "RETUNE"

    # If it's close (within 5%), hold current to avoid churn.
    if winner_sharpe >= current_sharpe * 0.95:
        return "HOLD_CURRENT"

    # If winner is worse but within 10%, investigate later.
    if winner_sharpe >= current_sharpe * 0.90:
        return "REVISIT_LATER"

    # Significant degradation — don't retune.
    return "HOLD_CURRENT"


def main(strategy_name, param_description, current_metrics=None):
    """Run the optimization pipeline."""
    result = {
        "strategy": strategy_name,
        "param_sweep_request": param_description,
        "results": [],
        "winner": None,
        "verdict": "ERROR",
        "diagnostics": "",
    }

    # Step 1: Generate sweep harness.
    code, err = generate_sweep_harness(strategy_name, param_description)
    if err:
        result["diagnostics"] = err
        return result

    # Step 2: Validate & run.
    success, output, err = validate_and_run(code)
    if err:
        result["diagnostics"] = err
        return result

    # Step 3: Parse results.
    results = parse_results(output)
    if not results:
        result["diagnostics"] = "Sweep ran but returned no result dicts."
        return result

    result["results"] = results

    # Step 4: Pick winner.
    winner = pick_winner(results)
    if winner:
        result["winner"] = winner

    # Step 5: Verdict.
    result["verdict"] = compare_to_current(current_metrics or {}, winner)

    return result


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python parameter_optimizer.py <strategy_name> <param_description>")
        print('Example: python parameter_optimizer.py sector_momentum "sweep lookback_months in [6,9,12]"')
        sys.exit(1)

    strategy = sys.argv[1]
    params   = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "default sweep"

    result = main(strategy, params)
    print(json.dumps(result, indent=2))
