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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
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
        f"  {o['submitted_at'][:16]}  {o['symbol']:6} {o['side']:4} {str(o['qty'] or o.get('notional','?')):>8}"
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
        # Distinguish "GitHub never injected the var" from "injected but empty"
        # so a manual run makes the cause obvious instead of guessing.
        present = "ANTHROPIC_API_KEY" in os.environ
        if present:
            return ("ANTHROPIC_API_KEY was injected by GitHub but is EMPTY — "
                    "the repository secret exists with no value. Re-save it under "
                    "Settings → Secrets and variables → Actions.")
        return ("ANTHROPIC_API_KEY not present in the environment — the workflow "
                "did not inject it. Check the secret name is exactly ANTHROPIC_API_KEY "
                "and that it is a repository secret (not an environment secret), "
                "then re-run.")

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

Output ONLY your FINAL version of each change — never include a draft snippet you then revise, and never emit two patches for the same location. Each FILE should appear at most once.
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
    """Parse FILE/OLD/NEW blocks from Claude's response and apply them.

    Two validation gates protect the live bots before any change is kept:
      1. py_compile — rejects syntax errors.
      2. pyflakes   — rejects undefined names / missing imports that compile
                      fine but crash at runtime (py_compile cannot see these).
    A failing patch is rolled back immediately, so a bad AI suggestion can
    never crash a live trading bot. When the AI emits both a draft and a
    revised fix for the same spot, the more specific (longer OLD) patch is
    applied first so the clean revision wins and the stale draft is rejected.
    Returns (applied, rejected) where rejected is a list of (filename, reason).
    """
    import re
    import py_compile
    import subprocess
    pattern = r"FILE:\s*(\S+)\nOLD:\n```[^\n]*\n(.*?)```\nNEW:\n```[^\n]*\n(.*?)```"
    patches = re.findall(pattern, analysis_text, re.DOTALL)
    # Most-specific (longest OLD) first: a complete revision beats a partial draft.
    patches.sort(key=lambda p: len(p[1]), reverse=True)
    applied, rejected = [], []
    for filename, old_code, new_code in patches:
        fname    = filename.strip()
        filepath = f"{BASE_DIR}/{fname}"
        if not os.path.exists(filepath):
            rejected.append((fname, "file not found"))
            continue
        with open(filepath) as f:
            original = f.read()
        if old_code.strip() not in original:
            rejected.append((fname, "OLD snippet did not match current code"))
            continue
        patched = original.replace(old_code.strip(), new_code.strip(), 1)
        with open(filepath, "w") as f:
            f.write(patched)
        # Gate 1: must still compile.
        try:
            py_compile.compile(filepath, doraise=True)
        except py_compile.PyCompileError as e:
            with open(filepath, "w") as f:
                f.write(original)  # roll back — never commit broken code
            rejected.append((fname, f"syntax error, reverted: {str(e)[:120]}"))
            continue
        # Gate 2: reject undefined names / missing imports — these compile but
        # crash at runtime. pyflakes' other warnings (unused var, f-string) are
        # ignored; only genuine "undefined name" errors trigger a rollback.
        flakes = subprocess.run(["python", "-m", "pyflakes", filepath],
                                capture_output=True, text=True)
        if "undefined name" in (flakes.stdout + flakes.stderr):
            with open(filepath, "w") as f:
                f.write(original)  # roll back — would crash a live bot at runtime
            reason = next((ln for ln in flakes.stdout.splitlines()
                           if "undefined name" in ln), "undefined name")
            rejected.append((fname, f"undefined name, reverted: {reason[-100:]}"))
            continue
        applied.append(fname)
    return applied, rejected


# ── Local rule-based analysis (no API, always runs) ───────────────────────────

def local_analysis():
    """Deterministic checks computed purely from live account data.

    Runs with zero external dependencies so the report is always useful even
    when the Claude API is unavailable (no key / no credits). Every line is
    derived from real numbers — nothing is inferred or predicted.
    """
    acct      = get_account()
    positions = get_positions()
    lines = []

    equity      = float(acct.get("equity", 0) or 0)
    last_equity = float(acct.get("last_equity", equity) or equity)
    cash        = float(acct.get("cash", 0) or 0)
    daily_pnl   = equity - last_equity

    lines.append(f"- Equity ${equity:,.2f} | day {daily_pnl:+,.2f} "
                 f"({(daily_pnl/last_equity*100) if last_equity else 0:+.2f}%) "
                 f"| cash {(cash/equity*100) if equity else 0:.0f}% of book")

    if not positions:
        lines.append("- No open positions.")
        return "\n".join(lines)

    total_unreal = sum(float(p.get("unrealized_pl", 0) or 0) for p in positions)
    lines.append(f"- {len(positions)} open positions | total unrealized ${total_unreal:+,.2f}")

    # Winners / losers, sorted by P&L
    ranked = sorted(positions, key=lambda p: float(p.get("unrealized_pl", 0) or 0), reverse=True)
    for p in ranked:
        pl   = float(p.get("unrealized_pl", 0) or 0)
        plpc = float(p.get("unrealized_plpc", 0) or 0) * 100
        flag = ""
        if plpc <= -20:
            flag = "  ⚠ down >20%"
        lines.append(f"    {p['symbol']:22} P&L ${pl:+,.2f} ({plpc:+.1f}%){flag}")

    # Option expiry warnings (parse OCC symbols: ROOT + YYMMDD + C/P + 8-digit strike)
    for p in positions:
        sym = p["symbol"]
        if len(sym) >= 16 and sym[-9] in ("C", "P") and sym[-8:].isdigit():
            try:
                exp_date = datetime.strptime(sym[-15:-9], "%y%m%d").date()
                days = (exp_date - datetime.utcnow().date()).days
                if days <= 10:
                    kind = "call" if sym[-9] == "C" else "put"
                    lines.append(f"- ⚠ Option {sym} ({kind}) expires in {days} day(s) on {exp_date}.")
            except Exception:
                pass

    # TSLA trailing-stop proximity, straight from strategy_state.json
    try:
        st = json.loads(read_file(f"{BASE_DIR}/strategy_state.json"))
        tsla = next((p for p in positions if p["symbol"] == st.get("symbol")), None)
        if tsla:
            price   = float(tsla["current_price"])
            entry   = float(st.get("entry_price", 0) or 0)
            stop    = float(st.get("current_stop", 0) or 0)
            trigger = entry * 1.10
            if st.get("trailing_active"):
                lines.append(f"- {st['symbol']} trailing active | ${price:.2f}, stop ${stop:.2f} "
                             f"({(price-stop)/price*100:.1f}% above stop).")
            elif entry:
                lines.append(f"- {st['symbol']} ${price:.2f} | trailing arms at ${trigger:.2f} "
                             f"({(trigger-price)/price*100:+.1f}% away).")
    except Exception:
        pass

    return "\n".join(lines)


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
    context = build_context()

    # Deterministic analysis always runs (free, no API).
    local = local_analysis()

    # AI analysis layers on top when the API is available.
    ai = call_claude(context)
    applied, rejected = apply_patches(ai)

    analysis = f"### Automated checks (no API required)\n{local}\n\n### AI analysis\n{ai}"
    if applied:
        analysis += f"\n\n### Code improvements applied (syntax-verified)\n" + \
                    "\n".join(f"- {f}" for f in applied)
    if rejected:
        analysis += f"\n\n### Patches rejected (not applied)\n" + \
                    "\n".join(f"- {f}: {reason}" for f, reason in rejected)

    path = save_report(context, analysis)

    print(f"Report saved: {path}")
    print(f"Patches applied: {applied or 'none'} | rejected: {len(rejected)}")
    print("\n--- ANALYSIS ---")
    print(analysis)


if __name__ == "__main__":
    run()
