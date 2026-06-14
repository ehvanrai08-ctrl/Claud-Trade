"""
Post-market analysis — runs after each session close.
Reads all logs, positions, and P&L, then calls Claude API
to generate a performance report and suggest/apply code improvements.
Commits the report and any code changes back to the repo.
"""

import json
import os
import requests
from datetime import datetime, timedelta
from dotenv import dotenv_values

BASE_DIR = "/home/user/Claud-Trade"
config   = dotenv_values(f"{BASE_DIR}/.env")

BASE_URL = config["ALPACA_BASE_URL"]
HEADERS  = {
    "APCA-API-KEY-ID":     config["ALPACA_API_KEY"],
    "APCA-API-SECRET-KEY": config["ALPACA_SECRET_KEY"],
}
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TODAY = datetime.utcnow().strftime("%Y-%m-%d")


# ── Gather data ───────────────────────────────────────────────────────────────

def read_file(path):
    try:
        with open(path) as f:
            return f.read()
    except Exception:
        return "(not found)"

def get_account():
    r = requests.get(f"{BASE_URL}/account", headers=HEADERS)
    return r.json() if r.ok else {}

def get_positions():
    r = requests.get(f"{BASE_URL}/positions", headers=HEADERS)
    return r.json() if r.ok else []

def get_orders_today():
    r = requests.get(f"{BASE_URL}/orders",
        headers=HEADERS,
        params={"status": "all", "after": f"{TODAY}T00:00:00Z", "limit": 50})
    return r.json() if r.ok else []

def tail_log(path, lines=80):
    try:
        with open(path) as f:
            all_lines = f.readlines()
        return "".join(all_lines[-lines:])
    except Exception:
        return "(no log)"


# ── Build report ──────────────────────────────────────────────────────────────

def build_context():
    acct      = get_account()
    positions = get_positions()
    orders    = get_orders_today()

    try:
        from performance_tracker import summary_text
        perf_summary = summary_text()
    except Exception as e:
        perf_summary = f"PERFORMANCE: tracker error — {e}"

    equity      = float(acct.get("equity", 0))
    last_equity = float(acct.get("last_equity", equity))
    daily_pnl   = equity - last_equity
    daily_pct   = (daily_pnl / last_equity * 100) if last_equity else 0

    pos_summary = "\n".join(
        f"  {p['symbol']:6} qty={p['qty']:>4}  avg=${float(p['avg_entry_price']):.2f}"
        f"  now=${float(p['current_price']):.2f}  P&L=${float(p['unrealized_pl']):+.2f}"
        for p in positions
    ) or "  (none)"

    order_summary = "\n".join(
        f"  {o['submitted_at'][:16]}  {o['symbol']:6} {o['side']:4} {o['qty']:>4}"
        f"  {o['type']:12} status={o['status']}"
        for o in orders
    ) or "  (none)"

    return f"""
DATE: {TODAY}

{perf_summary}

ACCOUNT
  Equity:       ${equity:,.2f}
  Daily P&L:    ${daily_pnl:+,.2f} ({daily_pct:+.2f}%)
  Cash:         ${float(acct.get('cash', 0)):,.2f}

POSITIONS
{pos_summary}

TODAY'S ORDERS
{order_summary}

STRATEGY STATE (TSLA trailing stop)
{read_file(f"{BASE_DIR}/strategy_state.json")}

WHEEL STATE
{read_file(f"{BASE_DIR}/wheel_state.json")}

COPY TRADER STATE
{read_file(f"{BASE_DIR}/copy_trader_state.json")}

TJR STRATEGY STATE
{read_file(f"{BASE_DIR}/tjr_state.json")}

MEAN REVERSION STATE
{read_file(f"{BASE_DIR}/mean_reversion_state.json")}

MONITOR LOG (last 80 lines)
{tail_log(f"{BASE_DIR}/monitor.log")}

WHEEL LOG (last 80 lines)
{tail_log(f"{BASE_DIR}/wheel.log")}

COPY TRADER LOG (last 80 lines)
{tail_log(f"{BASE_DIR}/copy_trader.log")}

TJR LOG (last 80 lines)
{tail_log(f"{BASE_DIR}/tjr.log")}

MEAN REVERSION LOG (last 80 lines)
{tail_log(f"{BASE_DIR}/mean_reversion.log")}
""".strip()


# ── Ask Claude for analysis + improvements ────────────────────────────────────

def call_claude(context):
    if not ANTHROPIC_KEY:
        return "ANTHROPIC_API_KEY not set — skipping AI analysis."

    market_monitor  = read_file(f"{BASE_DIR}/market_monitor.py")
    wheel_strategy  = read_file(f"{BASE_DIR}/wheel_strategy.py")
    copy_trader     = read_file(f"{BASE_DIR}/copy_trader.py")

    prompt = f"""You are reviewing a paper trading bot after today's market session.

Here is today's performance data:
<data>
{context}
</data>

Here is the current code for each bot:
<market_monitor>
{market_monitor}
</market_monitor>
<wheel_strategy>
{wheel_strategy}
</wheel_strategy>
<copy_trader>
{copy_trader}
</copy_trader>

Your job:
1. Write a concise daily report (what worked, what didn't, any errors spotted in logs)
2. Identify up to 3 specific, concrete improvements to the code
3. For each improvement, output the EXACT old code snippet and the new replacement using this format:

FILE: <filename>
OLD:
```
<exact old code>
```
NEW:
```
<exact new code>
```

Only suggest changes that are safe, tested improvements — not speculative rewrites.
If today had no activity (market was closed or bots didn't fire), just note that and skip improvements.
Keep the report under 400 words."""

    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key":         ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        },
        json={
            "model":      "claude-sonnet-4-6",
            "max_tokens": 4096,
            "messages":   [{"role": "user", "content": prompt}],
        },
    )
    if r.ok:
        return r.json()["content"][0]["text"]
    return f"Claude API error: {r.status_code} {r.text[:200]}"


# ── Apply code patches ────────────────────────────────────────────────────────

def apply_patches(analysis_text):
    """Parse FILE/OLD/NEW blocks from Claude's response and apply them."""
    import re
    pattern = r"FILE:\s*(\S+)\nOLD:\n```[^\n]*\n(.*?)```\nNEW:\n```[^\n]*\n(.*?)```"
    patches = re.findall(pattern, analysis_text, re.DOTALL)
    applied = []
    for filename, old_code, new_code in patches:
        filepath = f"{BASE_DIR}/{filename.strip()}"
        if not os.path.exists(filepath):
            continue
        with open(filepath) as f:
            content = f.read()
        if old_code.strip() in content:
            content = content.replace(old_code.strip(), new_code.strip(), 1)
            with open(filepath, "w") as f:
                f.write(content)
            applied.append(filename.strip())
    return applied


# ── Save report ───────────────────────────────────────────────────────────────

def save_report(context, analysis):
    report = f"# Post-Market Report — {TODAY}\n\n## Session Data\n```\n{context}\n```\n\n## Analysis & Improvements\n\n{analysis}\n"
    os.makedirs(f"{BASE_DIR}/reports", exist_ok=True)
    path = f"{BASE_DIR}/reports/{TODAY}.md"
    with open(path, "w") as f:
        f.write(report)
    return path


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    print(f"Post-market analysis — {TODAY}")
    context  = build_context()
    analysis = call_claude(context)
    patches  = apply_patches(analysis)
    path     = save_report(context, analysis)

    print(f"Report saved: {path}")
    if patches:
        print(f"Code improvements applied to: {', '.join(patches)}")
    else:
        print("No code patches applied today.")
    print("\n--- ANALYSIS ---")
    print(analysis)


if __name__ == "__main__":
    run()
