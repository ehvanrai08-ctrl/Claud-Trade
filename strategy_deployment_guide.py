"""
Strategy Deployment Recommendation Engine

Analyzes:
1. Live bots and their current performance
2. Efficient discovery results (proven profitable variations)
3. Current portfolio composition and gaps
4. Recommends:
   - Which new strategies to deploy (Tier 1 only)
   - How much capital to allocate
   - Parameter tuning for live bots
   - Which underperforming bots to pause

Usage:
  python strategy_deployment_guide.py

Outputs:
  - Summary to console
  - Detailed report to reports/deployment_guide_YYYY-MM-DD.md
"""

import json
import os
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = f"{BASE_DIR}/reports"


def load_performance():
    """Load current live performance metrics."""
    perf_file = f"{BASE_DIR}/performance.json"
    if os.path.exists(perf_file):
        with open(perf_file) as f:
            return json.load(f)
    return {"by_strategy": {}, "total_trades": 0}


def load_efficient_discovery():
    """Load most recent efficient discovery results."""
    # Read the most recent report
    import glob
    reports = sorted(glob.glob(f"{REPORTS_DIR}/efficient_discovery_*.md"), reverse=True)
    if not reports:
        return None

    # Parse the Tier 1 table from markdown. Match on ROW SHAPE (name + numeric
    # sharpe in the first two cells), not on any status wording — the report's
    # status column text changes, but a data row always starts with a strategy
    # name and a float Sharpe.
    def _num(s):
        try:
            return float(s)
        except (TypeError, ValueError):
            return None

    tier1 = []
    with open(reports[0]) as f:
        content = f.read()
    in_tier1 = False
    for line in content.split("\n"):
        if "Tier 1 —" in line:
            in_tier1 = True
            continue
        if in_tier1 and line.startswith("### ") and "Tier 1" not in line:
            break
        if in_tier1 and line.strip().startswith("|"):
            parts = [p.strip() for p in line.strip().strip("|").split("|")]
            if len(parts) >= 5 and _num(parts[1]) is not None:  # parts[0]=name, [1]=sharpe
                tier1.append({
                    "name": parts[0],
                    "sharpe": _num(parts[1]),
                    "spy_sharpe": _num(parts[2]),
                    "cagr": parts[3],
                    "maxdd": parts[4],
                })

    return {"tier1_candidates": tier1, "source": reports[0]}


def analyze_portfolio():
    """Analyze current portfolio composition and gaps."""
    perf = load_performance()
    live_strategies = list(perf.get("by_strategy", {}).keys())

    # Strategy families
    families = {
        "momentum": ["trend_basket", "sector_momentum", "dual_momentum"],
        "mean_reversion": ["ibs_strategy", "rsi2_strategy", "mean_reversion"],
        "options": ["wheel"],
        "copy": ["copy_trader", "superinvestor_copy"],
        "trend_following": ["tsmom_sleeve", "credit_vol_qqq"],
        "emerging": ["emerging_growth"],
        "dca": ["dca_index"],
        "single_name": ["tsla_trailing"],
    }

    # Current coverage
    covered = set()
    for fam, strats in families.items():
        if any(s in live_strategies for s in strats):
            covered.add(fam)

    missing = set(families.keys()) - covered
    gaps = {k: families[k] for k in missing}

    return {
        "live_count": len(live_strategies),
        "live_strategies": live_strategies,
        "covered_families": sorted(covered),
        "missing_families": sorted(missing),
        "gaps": gaps,
    }


# Every family efficient_strategy_discovery tests is ALREADY live in the fleet.
# So a "candidate" is a parameter re-tune of an existing bot, NOT a new strategy
# to deploy alongside it — deploying a redundant sleeve would fight the live bot
# for the same symbols (QQQ/SPY), dilute capital, and fake diversification.
# This map classifies each candidate back to the live bot it would tune.
LIVE_FAMILY_MAP = {
    "credit_vol": "credit_vol_qqq.py",
    "tsmom":      "tsmom_sleeve.py",
    "rsi":        "rsi2_strategy.py / ibs_strategy.py / mean_reversion.py",
    "mr":         "rsi2_strategy.py / ibs_strategy.py / mean_reversion.py",
}


def classify_candidate(name):
    """Return the live bot a candidate re-tunes, or None if genuinely novel."""
    for prefix, live_bot in LIVE_FAMILY_MAP.items():
        if name.startswith(prefix):
            return live_bot
    return None


def build_report(perf, discovery, portfolio):
    """Assemble deployment guide markdown."""
    timestamp = datetime.now(timezone.utc).isoformat()

    lines = [
        f"# Strategy Deployment Guide — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        "",
        f"**Timestamp**: {timestamp}",
        f"**Portfolio Size**: {portfolio['live_count']} live strategies",
        f"**Total Trades (YTD)**: {perf.get('total_trades', 0)}",
        "",
        "## Current Status",
        "",
        "### Live Strategies",
        "| Strategy | Trades | Win% | P&L | PF |",
        "|---|---|---|---|---|",
    ]

    # Add live strategy stats
    for strat, stats in sorted(perf.get("by_strategy", {}).items()):
        win_pct = stats.get("win_rate", 0)
        pnl = stats.get("total_pnl", 0)
        pf = stats.get("profit_factor", 0)
        trades = stats.get("trades", 0)
        lines.append(f"| {strat} | {trades} | {win_pct:.0f}% | ${pnl:.2f} | {pf:.2f} |")

    lines.extend([
        "",
        "### Portfolio Composition",
        f"**Covered families**: {', '.join(portfolio['covered_families'])}",
        f"**Missing families**: {', '.join(portfolio['missing_families']) or 'None'}",
        "",
        "## Recommendations",
        "",
    ])

    if discovery and discovery.get("tier1_candidates"):
        tier1 = discovery["tier1_candidates"]
        # Split candidates: re-tunes of live bots vs genuinely novel families.
        retunes = [c for c in tier1 if classify_candidate(c["name"])]
        novel   = [c for c in tier1 if not classify_candidate(c["name"])]

        lines.extend([
            "> **Read this first.** Every family efficient discovery tests"
            " (credit/vol, TSMOM, mean-reversion) is ALREADY live in the fleet."
            " The high-Sharpe candidates below are parameter *re-tunes* of"
            " existing bots, NOT new strategies to deploy alongside them."
            " Deploying a redundant sleeve fights the live bot for the same"
            " symbols, dilutes capital, and fakes diversification. Treat these"
            " as tuning inputs for the live bot — validate the winning params"
            " against the bot's ACTUAL mechanism before changing anything"
            " (see credit_vol_qqq: its live VIXY-percentile gate is a different"
            " signal from discovery's QQQ-realized-vol proxy, so the discovery"
            " Sharpe does NOT transfer 1:1 — cf. research/backtest_vixy_gate.py).",
            "",
            "### Tuning candidates for LIVE bots (top by Sharpe)",
            "",
            "| Candidate | Sharpe | vs SPY | CAGR | MaxDD | Tunes live bot |",
            "|---|---|---|---|---|---|",
        ])
        for cand in sorted(retunes, key=lambda c: c.get("sharpe", 0), reverse=True):
            live_bot = classify_candidate(cand["name"])
            spy_s = cand.get("spy_sharpe", 0.8) or 0.8
            lines.append(
                f"| {cand['name']} | {cand['sharpe']:.2f} | +{float(cand['sharpe']) - spy_s:.2f} | "
                f"{cand['cagr']} | {cand['maxdd']} | {live_bot} |"
            )

        if novel:
            lines.extend([
                "",
                "### Genuinely novel candidates (no matching live bot)",
                "",
                "These do NOT map to an existing bot and would be real additions"
                " — the only rows that justify a *new* sleeve. Validate on live"
                " Alpaca paper for 2–4 weeks before allocating capital.",
                "",
                "| Candidate | Sharpe | CAGR | MaxDD |",
                "|---|---|---|---|",
            ])
            for cand in sorted(novel, key=lambda c: c.get("sharpe", 0), reverse=True):
                lines.append(
                    f"| {cand['name']} | {cand['sharpe']:.2f} | {cand['cagr']} | {cand['maxdd']} |"
                )
        else:
            lines.extend([
                "",
                "### Genuinely novel candidates",
                "",
                "**None this run.** Every Tier-1 candidate re-tunes a live bot."
                " No new sleeve is warranted — the fleet already covers these"
                " families. The honest action is parameter tuning, not deployment.",
            ])

    lines.extend([
        "",
        "## How to act on this",
        "",
        "1. Efficient discovery tests parameter variations on proven families"
        " (no Claude API). Its job is **tuning live bots**, not finding new ones —"
        " all three families it tests are already deployed.",
        "2. A candidate only justifies a NEW sleeve if it appears under \"novel\""
        " above (no matching live bot). Redundant re-tunes do not.",
        "3. Before changing a live bot's params, backtest the bot's ACTUAL"
        " mechanism (not discovery's proxy) — the two can diverge materially.",
        "4. All numbers are in-sample 2015–2026, no holdout. Directional, not gospel.",
        "",
        "## Next Steps",
        "",
        "- [ ] Treat credit_vol / tsmom candidates as tuning inputs only (already live)",
        "- [ ] For any param change, validate on the live bot's real mechanism first",
        "- [ ] Only deploy from the \"novel\" table (empty this run)",
        "- [ ] Re-run efficient_strategy_discovery weekly; watch for novel families",
        "",
        "---",
        "*Report generated by strategy_deployment_guide.py*",
    ])

    return "\n".join(lines)


def main():
    """Run the deployment guide."""
    print("[DEPLOY] Analyzing portfolio and recommendations...", flush=True)

    perf = load_performance()
    discovery = load_efficient_discovery()
    portfolio = analyze_portfolio()

    report = build_report(perf, discovery, portfolio)

    # Write report
    report_file = f"{REPORTS_DIR}/deployment_guide_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md"
    with open(report_file, "w") as f:
        f.write(report)

    print(f"[DEPLOY] Report written to {report_file}", flush=True)
    print(report)


if __name__ == "__main__":
    main()
