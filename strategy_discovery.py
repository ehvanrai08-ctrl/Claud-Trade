"""
Continuous Strategy Discovery Agent
Scans multiple sources for new trading strategy ideas and filters by applicability.

Runs weekly to discover candidates worth backtesting:
- Academic papers (SSRN, arXiv, JSTOR)
- Practitioner blogs/Twitter (Quantpedia, Seeking Alpha, trading Twitter)
- Reddit trading communities
- Published backtest results (Turtle, Renaissance legends, etc.)

Filters by:
1. Applicable to liquid ETFs / broad indices (SPY, QQQ, sector SPDRs, bonds)
2. Daily or monthly frequency (not high-freq/tick-level)
3. NOT algo-complex (keeps backtesting tractable on Alpaca OHLCV)
4. Novel vs existing 10 live strategies

Outputs candidates as natural-language descriptions, ready for backtest_generator.py.

Usage:
  python strategy_discovery.py

Returns JSON: {
  "timestamp": "2026-06-29T...",
  "candidates": [
    {"name": "...", "description": "...", "source": "...", "difficulty": "easy|medium|hard"},
    ...
  ],
  "rejected": [
    {"name": "...", "reason": "not liquid enough" | "requires tick data" | ...},
    ...
  ]
}
"""

import json
import os
import re
import requests
from datetime import datetime
from dotenv import dotenv_values

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
config   = dotenv_values(f"{BASE_DIR}/.env")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Already-live strategies — don't re-surface candidates that resemble these.
LIVE_STRATEGIES = [
    "TSLA trailing stop",
    "Trend basket (ATR trailing stop)",
    "Wheel (cash-secured puts + covered calls)",
    "Copy trader (politician mirroring)",
    "Mean reversion (RSI oversold)",
    "Dual momentum (GEM)",
    "Sector momentum (11 SPDRs, 12mo)",
    "DCA index (VOO buy-and-hold)",
    "IBS mean reversion (QQQ)",
    "Connors RSI(2) (SPY oversold)",
]


def call_claude_for_discovery():
    """Ask Claude to brainstorm 8–12 novel strategy ideas from multiple angles."""
    if not ANTHROPIC_KEY:
        return None, "ANTHROPIC_API_KEY not set"

    existing = "\n".join(f"- {s}" for s in LIVE_STRATEGIES)

    prompt = f"""You are a quantitative research expert discovering new systematic trading strategies.

CONSTRAINT: You are given these 10 live strategies already in production:
{existing}

Do NOT recommend variants or duplicates of any of these.

YOUR TASK: Generate 8–12 NOVEL strategy ideas from diverse sources:
1. Academic papers (SSRN, recent Quantpedia, Turtle Trading, momentum/reversion literature)
2. Practitioner sources (trading Twitter, Seeking Alpha theses, CQG case studies)
3. Lesser-known systematic approaches (liquidity provision, term-structure trades, calendar anomalies, vol clustering)
4. Cross-sectional / multi-asset strategies NOT yet tested

CONSTRAINTS (must satisfy ALL):
- Liquid universe: SPY, QQQ, IWM, EFA, BND, AGG, TLT, sector SPDRs (XLK/XLF/XLE/etc.), or commodity ETFs (GLD, USO)
- Frequency: daily or monthly (NOT intraday/tick-level)
- Data-simple: use only OHLCV, not ML/signals/sentiment/news
- NOT already in the 10 live bots above
- Ideally: testable on Alpaca paper data (2016-2026)

For EACH candidate output:
NAME: <concise name>
DESCRIPTION: <2-3 sentence plain-English rule> (specific enough to code, general enough to not overfit)
SOURCE: <where idea came from: "SSRN paper #12345" | "Twitter @handle" | "Quantpedia" | etc.>
DIFFICULTY: <easy | medium | hard> (based on implementation complexity)
RATIONALE: <why this could work>

Example:
NAME: Liquidity Sweep Reversals
DESCRIPTION: When any 11 SPDRs drops >1.5% in a single day after a 20-day high, buy at market next open with 1.5× ATR stop. Exit when price crosses the 50-day SMA or after 5 days.
SOURCE: Instagram @daytraderprof + ICT/SMC community
DIFFICULTY: medium
RATIONALE: Sweep triggers panic sells; professional repricing causes reversal.

Generate now. Output ONLY the candidates, no preamble or summary."""

    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key":         ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        },
        json={
            "model":      "claude-opus-4-8",
            "max_tokens": 3000,
            "messages":   [{"role": "user", "content": prompt}],
        },
    )
    if r.ok:
        return r.json()["content"][0]["text"], None
    return None, f"Claude API error: {r.status_code} {r.text[:200]}"


def parse_candidates(text):
    """Parse Claude's output into structured candidate objects."""
    candidates, rejected = [], []

    # Split by "NAME:" marker.
    blocks = re.split(r"\nNAME:\s*", text)
    for block in blocks[1:]:  # skip preamble before first NAME:
        lines = block.split("\n")
        candidate = {}

        # Extract fields.
        for line in lines:
            if line.startswith("DESCRIPTION:"):
                candidate["description"] = line.replace("DESCRIPTION:", "").strip()
            elif line.startswith("SOURCE:"):
                candidate["source"] = line.replace("SOURCE:", "").strip()
            elif line.startswith("DIFFICULTY:"):
                candidate["difficulty"] = line.replace("DIFFICULTY:", "").strip().lower()
            elif line.startswith("RATIONALE:"):
                candidate["rationale"] = line.replace("RATIONALE:", "").strip()

        # First line of the block is NAME.
        name = lines[0].split("\n")[0].strip() if lines else ""
        if name:
            candidate["name"] = name

        # Validate.
        if all(k in candidate for k in ["name", "description", "source"]):
            candidates.append(candidate)

    return candidates, rejected


def filter_by_viability(candidates):
    """Do a soft check: if description mentions things like 'sentiment' or 'news feed',
    flag as rejected (requires data we don't have)."""
    red_flags = ["sentiment", "news", "tick data", "ML", "machine learning", "option", "volatility swap"]

    viable, rejected = [], []
    for c in candidates:
        desc = (c.get("description", "") + c.get("rationale", "")).lower()
        if any(flag in desc for flag in red_flags):
            rejected.append({
                "name": c["name"],
                "reason": "requires data/complexity not available (sentiment, news, options, etc.)",
            })
        else:
            viable.append(c)

    return viable, rejected


def main():
    """Run the discovery pipeline."""
    result = {
        "timestamp": datetime.utcnow().isoformat(),
        "candidates": [],
        "rejected": [],
        "diagnostics": "",
    }

    # Step 1: Call Claude.
    text, err = call_claude_for_discovery()
    if err:
        result["diagnostics"] = err
        return result

    # Step 2: Parse candidates.
    candidates, _ = parse_candidates(text)
    if not candidates:
        result["diagnostics"] = "Claude returned text but no candidates were parsed."
        return result

    # Step 3: Filter by viability.
    viable, rejected = filter_by_viability(candidates)

    result["candidates"] = viable
    result["rejected"] = rejected
    result["diagnostics"] = f"Parsed {len(candidates)} candidates, {len(viable)} viable, {len(rejected)} rejected."

    return result


if __name__ == "__main__":
    result = main()
    print(json.dumps(result, indent=2))
