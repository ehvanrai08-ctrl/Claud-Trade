"""
Backtest Automation Agent
Converts natural-language strategy descriptions into executable backtests.

Takes a strategy description (e.g., "mean reversion on QQQ when RSI oversold"),
calls Claude to generate backtest code on Alpaca daily data, validates it,
runs it, and extracts performance metrics (PF, Sharpe, maxDD, robustness).

Usage:
  python backtest_generator.py "description of strategy here"

Returns JSON: {
  "strategy": "...",
  "verdict": "PASS" | "FAIL" | "ERROR",
  "code_generated": "...",
  "performance": {
    "profit_factor": 1.23,
    "sharpe": 0.45,
    "max_drawdown": 0.15,
    "cagr": 0.08,
    ...
  },
  "diagnostics": "..."
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


def generate_backtest_code(strategy_description):
    """Call Claude to generate backtest code for the strategy."""
    if not ANTHROPIC_KEY:
        return None, "ANTHROPIC_API_KEY not set"

    prompt = f"""You are an expert quantitative trading developer. Generate a COMPLETE, WORKING Python backtest script.

STRATEGY DESCRIPTION:
{strategy_description}

REQUIREMENTS:
1. Use ONLY Alpaca daily data (2016-2026, SPY/QQQ/QQQ/TLT/AGG available).
2. NO external dependencies beyond requests, statistics, datetime (no pandas/numpy).
3. Import and use the fetch_daily() and perf_from_daily_returns() helpers from backtest_research.py.
4. Use the strategy on the most liquid/appropriate symbol(s).
5. For each entry/exit, document WHY (e.g., "buy when RSI<30 because mean reversion").
6. Output: daily log returns as a list; use perf_from_daily_returns() to compute metrics.
7. Compare against buy-and-hold benchmark on the same symbol/period.
8. Print results using show() helper in this format:
   show("Strategy Name", metrics, benchmark)

EXAMPLE TEMPLATE (adapt to your strategy):
```
from backtest_research import fetch_daily, perf_from_daily_returns, show

def my_strategy(bars):
    closes = [b["c"] for b in bars]
    rets = []
    holding = False
    for i in range(len(bars)):
        r = 0.0
        if holding and i > 0:
            r = closes[i] / closes[i-1] - 1
        rets.append(r)
        # Entry/exit logic here based on closes
        if not holding and /* entry condition */:
            holding = True
        elif holding and /* exit condition */:
            holding = False
    return rets

data = fetch_daily("QQQ")
bh = [data[i]["c"]/data[i-1]["c"]-1 for i in range(1, len(data))]
bench = perf_from_daily_returns(bh)
rets = my_strategy(data)
m = perf_from_daily_returns(rets)
show("Your Strategy", m, bench)
```

OUTPUT: Return ONLY the complete Python code block, ready to run with: python /tmp/backtest.py
No explanations, no markdown, no imports section — just code."""

    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key":         ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        },
        json={
            "model":      "claude-opus-4-8",
            "max_tokens": 2048,
            "messages":   [{"role": "user", "content": prompt}],
        },
    )
    if r.ok:
        return r.json()["content"][0]["text"], None
    return None, f"Claude API error: {r.status_code} {r.text[:200]}"


def validate_and_run(code):
    """Write code to temp file, validate it compiles, run it, return (success, output, error)."""
    import py_compile

    # Try to extract just the Python code if wrapped in markdown.
    code = code.strip()
    if code.startswith("```"):
        code = re.sub(r"^```[^\n]*\n", "", code)
        code = re.sub(r"\n```$", "", code)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, dir="/tmp") as f:
        f.write(code)
        temp_path = f.name

    try:
        # Validate syntax.
        try:
            py_compile.compile(temp_path, doraise=True)
        except py_compile.PyCompileError as e:
            return False, None, f"Syntax error: {str(e)[:200]}"

        # `cwd=` does NOT put BASE_DIR on the import path — Python adds the
        # SCRIPT's own directory (/tmp) to sys.path[0], not the subprocess's
        # working directory. Every generated backtest was crashing on
        # `from backtest_research import ...` with ModuleNotFoundError until
        # this was set explicitly via PYTHONPATH.
        env = {**os.environ, "PYTHONPATH": BASE_DIR}
        result = subprocess.run(
            ["python", temp_path],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=BASE_DIR,
            env=env,
        )
        if result.returncode != 0:
            return False, None, f"Runtime error: {result.stderr[:500]}"

        return True, result.stdout, None
    finally:
        try:
            os.unlink(temp_path)
        except:
            pass


def parse_metrics(output):
    """Extract PF, Sharpe, maxDD, CAGR from show() output."""
    metrics = {}

    # Look for pattern: "PF=1.23" or "Sharpe 0.45" etc.
    pf_match = re.search(r"PF\s*[=:]?\s*([\d.]+)", output, re.IGNORECASE)
    if pf_match:
        metrics["profit_factor"] = float(pf_match.group(1))

    sharpe_match = re.search(r"Sharpe\s*[=:]?\s*([\d.]+)", output, re.IGNORECASE)
    if sharpe_match:
        metrics["sharpe"] = float(sharpe_match.group(1))

    maxdd_match = re.search(r"maxDD\s*[=:]?\s*([\d.]+%?)", output, re.IGNORECASE)
    if maxdd_match:
        val = maxdd_match.group(1).rstrip("%")
        metrics["max_drawdown"] = float(val) / 100 if "%" in maxdd_match.group(1) else float(val)

    cagr_match = re.search(r"CAGR\s*[=:]?\s*([\d.]+%?)", output, re.IGNORECASE)
    if cagr_match:
        val = cagr_match.group(1).rstrip("%")
        metrics["cagr"] = float(val) / 100 if "%" in cagr_match.group(1) else float(val)

    return metrics


def verdict(metrics):
    """Simple pass/fail: PF > 1.0 and Sharpe > 0.5 as a rough bar."""
    pf    = metrics.get("profit_factor", 0)
    sharp = metrics.get("sharpe", 0)
    # Sharpe > 0.5 is solid for daily bars; PF > 1.0 is break-even.
    if pf > 1.0 and sharp > 0.5:
        return "PASS"
    elif pf > 1.0 and sharp > 0.2:
        return "MAYBE"  # profitable but not robust
    else:
        return "FAIL"


def main(strategy_description):
    """Run the full pipeline: generate → validate → run → extract metrics."""
    result = {
        "strategy": strategy_description,
        "verdict": "ERROR",
        "code_generated": None,
        "performance": {},
        "diagnostics": "",
    }

    # Step 1: Generate code.
    code, err = generate_backtest_code(strategy_description)
    if err:
        result["diagnostics"] = err
        return result

    result["code_generated"] = code

    # Step 2: Validate & run.
    success, output, err = validate_and_run(code)
    if err:
        result["diagnostics"] = err
        return result

    # Step 3: Parse metrics.
    metrics = parse_metrics(output)
    result["performance"] = metrics
    result["diagnostics"] = output[-500:] if len(output) > 500 else output  # tail of output

    # Step 4: Verdict.
    result["verdict"] = verdict(metrics)

    return result


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python backtest_generator.py 'strategy description'")
        sys.exit(1)

    description = " ".join(sys.argv[1:])
    result = main(description)
    print(json.dumps(result, indent=2))
