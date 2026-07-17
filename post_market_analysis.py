"""
Post-market analysis — runs after each session close.
Reads all logs, positions, and P&L, then calls Claude API
to generate a performance report and suggest/apply code improvements.
Commits the report and any code changes back to the repo.
"""

import json
import os
import sys
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import dotenv_values
from capital_allocator import compute_weights, summary_text as weights_summary

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
config   = dotenv_values(f"{BASE_DIR}/.env")

BASE_URL = config["ALPACA_BASE_URL"]
HEADERS  = {
    "APCA-API-KEY-ID":     config["ALPACA_API_KEY"],
    "APCA-API-SECRET-KEY": config["ALPACA_SECRET_KEY"],
}
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
# ET, not UTC: the 23:40 UTC catch-up cron is "best-effort" and routinely
# fires 1-2h late (GitHub's own scheduling slop). A run that slips past
# 20:00 UTC (8 PM ET) used to roll datetime.utcnow() onto the NEXT calendar
# date, mislabel that day's report, and — because run() skips if
# reports/<date>.md already exists — permanently block the real report for
# the day that was about to start. ET doesn't roll over until hours later,
# so it survives the same lateness UTC didn't.
TODAY = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

LESSONS_FILE   = f"{BASE_DIR}/lessons_learned.md"
MAX_LESSONS    = 60   # keep the file small (context is expensive) — trim oldest
BENCHMARK_FILE = f"{BASE_DIR}/benchmark_state.json"
BENCHMARK_SYM  = "VOO"   # the index the whole system is implicitly betting it can beat


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


# ── Persistent lessons memory (stateless runs build their own brain) ───────────

def read_lessons():
    """Existing lessons as bullet lines (the '- ...' entries only)."""
    if not os.path.exists(LESSONS_FILE):
        return []
    out = []
    for ln in read_file(LESSONS_FILE).splitlines():
        s = ln.strip()
        if s.startswith("- "):
            out.append(s[2:].strip())
    return out


def append_lessons(new_lessons):
    """Append genuinely-new lessons (case-insensitive dedup vs existing), trim to
    MAX_LESSONS so the file — which is fed back into every future run — stays
    small. Returns the list of lessons actually written."""
    existing = read_lessons()
    seen = {l.lower() for l in existing}
    fresh = []
    for l in new_lessons:
        l = l.strip().lstrip("-").strip()
        if l and l.lower() not in seen:
            fresh.append(l); seen.add(l.lower())
    if not fresh:
        return []
    block = "".join(f"- {l}\n" for l in fresh)
    with open(LESSONS_FILE, "a") as f:
        f.write(f"\n### {TODAY}\n{block}")
    # Trim: if the bullet count exceeds the cap, drop the oldest bullets.
    all_lessons = read_lessons()
    if len(all_lessons) > MAX_LESSONS:
        kept = all_lessons[-MAX_LESSONS:]
        header = ("# Lessons Learned\n\nDurable, distilled lessons the nightly bot "
                  "accumulates across runs (trimmed to the most recent "
                  f"{MAX_LESSONS}).\n\n---\n\n")
        with open(LESSONS_FILE, "w") as f:
            f.write(header + "".join(f"- {l}\n" for l in kept))
    return fresh


def parse_lessons(analysis_text):
    """Pull bullet lines out of a LESSONS: section in Claude's response."""
    import re
    m = re.search(r"LESSONS:\s*\n(.*?)(?:\n\s*\n|\Z)", analysis_text, re.DOTALL)
    if not m:
        return []
    lessons = []
    for ln in m.group(1).splitlines():
        s = ln.strip()
        if s.startswith("- ") or s.startswith("* "):
            lessons.append(s[2:].strip())
    return lessons


# ── Build report ──────────────────────────────────────────────────────────────

def trade_autopsy():
    """Per-strategy win rate, profit factor, and P&L from the realized-trade ledger.
    Surfaces the 'high win-rate trap': a strategy with 70% wins but 3:1 loss size
    is a loser. Returns a formatted string for Claude's context."""
    from perf import read_ledger
    trades = read_ledger()
    if not trades:
        return "TRADE AUTOPSY\n  No realized trades in ledger yet.\n"

    from collections import defaultdict
    import statistics
    # Preserve ledger (time) order so the drawdown curve is chronological.
    by_strat = defaultdict(list)
    for t in trades:
        by_strat[t["strategy"]].append(t["pnl"])

    def max_drawdown(pnls):
        """Largest peak-to-trough dip of the cumulative P&L curve ($)."""
        cum, peak, mdd = 0.0, 0.0, 0.0
        for p in pnls:
            cum += p
            peak = max(peak, cum)
            mdd  = max(mdd, peak - cum)
        return mdd

    lines = ["TRADE AUTOPSY (realized trades, all-time) — risk-adjusted"]
    for strat, pnls in sorted(by_strat.items()):
        wins   = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        total  = sum(pnls)
        wr     = len(wins) / len(pnls) * 100 if pnls else 0
        avg_w  = sum(wins) / len(wins) if wins else 0
        avg_l  = sum(losses) / len(losses) if losses else 0
        pf     = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float("inf")
        # Risk-adjusted (video lesson #3): judge return per unit of risk, not raw P&L.
        expectancy = total / len(pnls) if pnls else 0          # avg $ per trade
        mdd        = max_drawdown(pnls)
        ret_dd     = (total / mdd) if mdd > 0 else float("inf")  # return-over-maxDD
        sd         = statistics.stdev(pnls) if len(pnls) > 1 else 0
        sharpe     = (expectancy / sd) if sd > 0 else 0          # per-trade Sharpe
        lines.append(
            f"  {strat:22}  n={len(pnls):3}  wr={wr:.0f}%  "
            f"avg_win=${avg_w:+.2f}  avg_loss=${avg_l:+.2f}  "
            f"PF={pf:.2f}  total=${total:+.2f}"
        )
        lines.append(
            f"      expectancy=${expectancy:+.2f}/trade  maxDD=${mdd:.2f}  "
            f"ret/DD={ret_dd:.2f}  Sharpe={sharpe:.2f}"
        )
        if wr > 65 and pf < 1.0:
            lines.append("    ⚠ HIGH WIN-RATE TRAP: winning often but losing more per loss")
        elif wr < 35 and pf > 2.0:
            lines.append(f"    ✓ low win-rate but positive expectancy (pf>{pf:.1f})")
        if mdd > 0 and ret_dd < 1.0 and total > 0:
            lines.append("    ⚠ POOR RISK-ADJUSTED RETURN: profit smaller than worst drawdown (ret/DD<1)")
    return "\n".join(lines)


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

    autopsy = trade_autopsy()

    return f"""
DATE: {TODAY}

{perf_summary}

{autopsy}

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

    prior_lessons = read_lessons()
    lessons_block = ("\n".join(f"- {l}" for l in prior_lessons)
                     if prior_lessons else "(none yet)")

    prompt = f"""You are reviewing a paper trading bot after today's market session.

Here is today's performance data:
<data>
{context}
</data>

Lessons you have ALREADY learned on prior days (do NOT repeat these — only add
genuinely new ones):
<lessons>
{lessons_block}
</lessons>

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
Keep the report under 400 words.

4. Finally, if today taught a DURABLE lesson worth remembering on future days
   (a recurring error pattern, a strategy behaviour, a risk observation), add a
   section in EXACTLY this format (omit it entirely if there is nothing genuinely
   new beyond the lessons already listed above):

LESSONS:
- <one concise, durable lesson>
- <another, if any>"""

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
        flakes = subprocess.run([sys.executable, "-m", "pyflakes", filepath],
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

    lines.append(trade_autopsy())
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


# ── Benchmark vs buying the index (the SPIVA question) ────────────────────────

def _latest_price(symbol):
    try:
        r = requests.get(f"https://data.alpaca.markets/v2/stocks/{symbol}/trades/latest",
                         headers=HEADERS, timeout=15)
        return float(r.json()["trade"]["p"]) if r.ok else None
    except Exception:
        return None


def benchmark_vs_index():
    """Answer the one question that matters: is the whole system beating what you'd
    have made by just buying-and-holding the index with the same money?

    SPIVA: ~89% of professional managers fail to beat the S&P 500 over 15 years.
    An automated bot fleet has to clear that same bar. We track it honestly:
    on first run we record a baseline (system equity + index price); every run
    after compares system return since that baseline to the index's return over
    the identical window. The delta is alpha — positive means the bots are
    earning their complexity; negative means a VOO DCA would have done better.

    Self-bootstrapping (measures from first run forward), so it never fabricates
    a backdated number it can't actually verify.
    """
    acct   = get_account()
    equity = float(acct.get("equity", 0) or 0)
    idx_px = _latest_price(BENCHMARK_SYM)
    if equity <= 0 or not idx_px:
        return "BENCHMARK vs {}: data unavailable (equity or index price missing).".format(BENCHMARK_SYM)

    base = None
    if os.path.exists(BENCHMARK_FILE):
        try:
            base = json.loads(read_file(BENCHMARK_FILE))
        except Exception:
            base = None

    if not base or not base.get("equity") or not base.get("index_price"):
        with open(BENCHMARK_FILE, "w") as f:
            json.dump({"date": TODAY, "equity": equity,
                       "index": BENCHMARK_SYM, "index_price": idx_px}, f, indent=2)
        return (f"BENCHMARK vs {BENCHMARK_SYM}: baseline established today "
                f"(equity ${equity:,.2f}, {BENCHMARK_SYM} ${idx_px:.2f}). "
                f"Out/under-performance will be tracked from here forward.")

    sys_ret = (equity / base["equity"] - 1) * 100
    idx_ret = (idx_px / base["index_price"] - 1) * 100
    alpha   = sys_ret - idx_ret
    verdict = ("✓ system is BEATING a buy-and-hold of the index"
               if alpha >= 0 else
               "⚠ a buy-and-hold of the index would have done BETTER (negative alpha)")
    return (f"BENCHMARK vs {BENCHMARK_SYM} (since {base['date']})\n"
            f"  System return:   {sys_ret:+.2f}%  (equity ${base['equity']:,.0f} → ${equity:,.0f})\n"
            f"  {BENCHMARK_SYM} buy-and-hold: {idx_ret:+.2f}%  "
            f"(${base['index_price']:.2f} → ${idx_px:.2f})\n"
            f"  Alpha:           {alpha:+.2f}%  — {verdict}")


# ── Save report ───────────────────────────────────────────────────────────────

def save_report(context, analysis):
    # Self-contained generation timestamp (UTC) so a downstream check (e.g.
    # bug_hunter.py) can tell "ran before market close" from the report file
    # alone, without depending on git history (shallow checkouts don't keep
    # it) or comparing two fields both derived from the same TODAY value.
    generated_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    report = (f"# Post-Market Report — {TODAY}\n\n"
              f"_Generated: {generated_at} UTC_\n\n"
              f"## Session Data\n```\n{context}\n```\n\n## Analysis & Improvements\n\n{analysis}\n")
    os.makedirs(f"{BASE_DIR}/reports", exist_ok=True)
    path = f"{BASE_DIR}/reports/{TODAY}.md"
    with open(path, "w") as f:
        f.write(report)
    return path


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    print(f"Post-market analysis — {TODAY}")

    # Idempotency guard. The workflow has TWO scheduled triggers: the primary
    # (4:15 PM ET) and a later catch-up that backstops GitHub dropping/delaying
    # the primary (scheduled cron is best-effort and routinely fires 1–2h late or
    # not at all). If today's report already exists, the primary succeeded — the
    # catch-up must NOT run again: a second pass would re-call Claude (cost) and
    # re-apply patches. Set FORCE_RUN=1 to override (e.g. a manual re-run).
    report_path = f"{BASE_DIR}/reports/{TODAY}.md"
    if os.path.exists(report_path) and os.environ.get("FORCE_RUN") != "1":
        print(f"Report {report_path} already exists — skipping (catch-up no-op).")
        return

    context = build_context()

    # Deterministic analysis always runs (free, no API).
    local = local_analysis()

    # Benchmark the whole system against just buying the index (the SPIVA question).
    try:
        benchmark = benchmark_vs_index()
    except Exception as e:
        benchmark = f"BENCHMARK: error (non-fatal) — {e}"

    # AI analysis layers on top when the API is available.
    ai = call_claude(context)
    applied, rejected = apply_patches(ai)

    analysis = (f"### Automated checks (no API required)\n{local}\n\n"
                f"### Benchmark vs index (are the bots beating buy-and-hold?)\n{benchmark}\n\n"
                f"### AI analysis\n{ai}")
    if applied:
        analysis += "\n\n### Code improvements applied (syntax-verified)\n" + \
                    "\n".join(f"- {f}" for f in applied)
    if rejected:
        analysis += "\n\n### Patches rejected (not applied)\n" + \
                    "\n".join(f"- {f}: {reason}" for f, reason in rejected)

    # Distil and persist durable lessons (memory the next stateless run reads).
    try:
        new_lessons = append_lessons(parse_lessons(ai))
        if new_lessons:
            analysis += "\n\n### New lessons recorded\n" + \
                        "\n".join(f"- {l}" for l in new_lessons)
            print(f"Recorded {len(new_lessons)} new lesson(s).")
    except Exception as e:
        print(f"Lesson update failed (non-fatal): {e}")

    # Rotate oversized logs into logs/archive/ so committed logs don't grow unbounded.
    try:
        from archive_logs import rotate
        archived = rotate()
        if archived:
            analysis += "\n\n### Logs archived\n" + \
                        "\n".join(f"- {a}" for a in archived)
            print(f"Archived {len(archived)} log(s).")
    except Exception as e:
        print(f"Log archive failed (non-fatal): {e}")

    # Update dynamic capital weights from today's realized trades.
    try:
        weights_result = compute_weights()
        analysis += f"\n\n### Capital Allocation Update\n```\n{weights_summary(weights_result)}\n```"
        print("Capital weights updated.")
    except Exception as e:
        print(f"Capital weight update failed (non-fatal): {e}")

    path = save_report(context, analysis)

    print(f"Report saved: {path}")
    print(f"Patches applied: {applied or 'none'} | rejected: {len(rejected)}")
    print("\n--- ANALYSIS ---")
    print(analysis)


if __name__ == "__main__":
    run()
