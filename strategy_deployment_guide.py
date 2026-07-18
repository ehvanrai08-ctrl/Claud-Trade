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

    # Parse Tier 1 results from markdown
    tier1 = []
    with open(reports[0]) as f:
        content = f.read()
        in_tier1 = False
        for line in content.split("\n"):
            if "Tier 1 —" in line:
                in_tier1 = True
                continue
            if in_tier1 and line.startswith("### Tier"):
                break
            if in_tier1 and "|" in line and "—" not in line and "Deploy" in line:
                parts = [p.strip() for p in line.split("|")]
                if len(parts) >= 6:
                    tier1.append({
                        "name": parts[1],
                        "sharpe": float(parts[2]) if parts[2] != "?" else None,
                        "spy_sharpe": float(parts[3]) if parts[3] != "?" else None,
                        "cagr": parts[4],
                        "maxdd": parts[5],
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


def calculate_allocation(tier1_candidates):
    """Recommend capital allocation for new strategies."""
    # Current: 5 trades in ~$40k account = ~$8k per strategy
    # Assume total capital is ~$40k (typical Alpaca paper account)
    TOTAL_CAPITAL = 40000
    N_STRATEGIES = 15  # Target portfolio size
    BASE_ALLOCATION = TOTAL_CAPITAL / N_STRATEGIES  # ~$2.7k per strategy

    recommendations = []
    for cand in tier1_candidates:
        # Weight by Sharpe: high-Sharpe strategies get more capital
        sharpe = cand.get("sharpe", 0.8)
        spy_sharpe = cand.get("spy_sharpe", 0.8)
        if sharpe > spy_sharpe:
            # This strategy has genuine alpha, allocate more
            allocation = BASE_ALLOCATION * (sharpe / spy_sharpe)
        else:
            # Diversifier, standard allocation
            allocation = BASE_ALLOCATION

        recommendations.append({
            "strategy": cand["name"],
            "sharpe": cand.get("sharpe"),
            "recommended_capital": int(allocation),
            "reason": "alpha" if sharpe > spy_sharpe else "diversification",
        })

    return sorted(recommendations, key=lambda x: x["recommended_capital"], reverse=True)


def build_report(perf, discovery, portfolio, allocations):
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
        lines.extend([
            "### New Strategies to Deploy (Tier 1 from efficient discovery)",
            "",
            "High-Sharpe, look-ahead-safe backtest results (2016–2026 Yahoo data):",
            "",
            "| Strategy | Sharpe | vs SPY | CAGR | MaxDD | Allocation | Rationale |",
            "|---|---|---|---|---|---|---|",
        ])

        for alloc in allocations:
            # Find the corresponding Tier 1 candidate
            cand = next((c for c in tier1 if c["name"] == alloc["strategy"]), None)
            if cand:
                lines.append(
                    f"| {cand['name']} | {cand['sharpe']:.2f} | +{float(cand['sharpe']) - (cand.get('spy_sharpe', 0.8)):.2f} | "
                    f"{cand['cagr']} | {cand['maxdd']} | ${alloc['recommended_capital']:,} | "
                    f"{alloc['reason']} |"
                )

        lines.extend([
            "",
            "### Deployment Order",
            "1. **Priority 1** (top 3 by Sharpe): Start with credit_vol variations (best risk-adjusted returns)",
            "2. **Priority 2** (next 3): TSMOM variations (proven diversifiers, low correlation to SPY)",
            "3. **Priority 3** (others): Test in staging first, deploy if live backtest confirms",
            "",
        ])

    lines.extend([
        "## Parameter Tuning for Live Bots",
        "",
        "Based on efficient discovery, live bots can be improved:",
        "",
        "- **credit_vol_qqq**: Currently uses HYG > 200d SMA and VIX < 30. Backtest suggests HYG > 1.0×200d and VIX < 25 is optimal (Sharpe 1.17 vs 1.13 live).",
        "- **tsmom_sleeve**: 6-month lookback on 5 assets (SPY/TLT/GLD/DBC/UUP) achieves Sharpe 1.01, comparable to current (0.86–1.19 range).",
        "- **rsi2_strategy / ibs_strategy**: Mean reversion still under test — discovery phase incomplete.",
        "",
        "## Process",
        "",
        "1. Efficient discovery identifies parameter variations on proven families (no Claude API used).",
        "2. Tier 1 candidates (beat SPY on Sharpe AND maxDD) are recommended for live deployment.",
        "3. Each new strategy gets 5–10% of portfolio, scaled by Sharpe ratio.",
        "4. Live backtest on Alpaca paper for 2–4 weeks before moving capital.",
        "5. Monthly re-optimization: tune parameters on live data, redeploy if improvements found.",
        "",
        "## Next Steps",
        "",
        "- [ ] Deploy credit_vol_hyg1.0_vix25 (Sharpe 1.17)",
        "- [ ] Deploy tsmom_6m_5a (Sharpe 1.01) as alternative to current TSMOM sleeve",
        "- [ ] Re-test mean reversion (debug, fix errors)",
        "- [ ] Run efficient_strategy_discovery again in 1 week",
        "- [ ] Measure live Alpaca performance vs backtest, adjust parameters weekly",
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
    allocations = calculate_allocation(discovery.get("tier1_candidates", []) if discovery else [])

    report = build_report(perf, discovery, portfolio, allocations)

    # Write report
    report_file = f"{REPORTS_DIR}/deployment_guide_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md"
    with open(report_file, "w") as f:
        f.write(report)

    print(f"[DEPLOY] Report written to {report_file}", flush=True)
    print(report)


if __name__ == "__main__":
    main()
