"""
Backtest — Master Strategy: Regime-Filtered Mean Reversion
==========================================================
Event-driven daily backtest over real Alpaca historical bars. Walks one bar at
a time and only ever uses data available AT that day's close — no look-ahead
bias (a documented LLM failure mode we explicitly guard against).

MASTER STRATEGY (long-only, one position per symbol):
  Indicators computed each day from closed bars only:
    - IBS  = (Close - Low) / (High - Low)        # intraday close strength
    - RSI(2)                                       # Connors 2-period RSI
    - SMA200, SMA5
  ENTRY (next-open or same-close, see FILL):
    IBS < IBS_MAX  AND  RSI2 < RSI2_MAX  AND  Close > SMA200   (buy dips in uptrends)
  EXIT:
    Close > previous day's High   (Connors/Alvarez "first strength" exit)
    OR a hard time-stop after MAX_HOLD days (safety).

Reports: full trade log (entry/exit dates), win rate, profit factor, CAGR,
max drawdown, Sharpe — all vs. buy-and-hold for the same capital and window.

Run: python backtest.py            (defaults: SPY,QQQ, ~10y)
"""

import os
import math
import statistics
from datetime import datetime, timezone
import requests
from dotenv import dotenv_values

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
config   = dotenv_values(f"{BASE_DIR}/.env")
DATA_HEADERS = {
    "APCA-API-KEY-ID":     config["ALPACA_API_KEY"],
    "APCA-API-SECRET-KEY": config["ALPACA_SECRET_KEY"],
}

# ── Strategy parameters ───────────────────────────────────────────────────────
SYMBOLS     = ["SPY", "QQQ"]
START       = "2015-01-01"
IBS_MAX     = 0.20      # buy only when close is in the bottom 20% of the day's range
RSI2_MAX    = 10.0      # Connors oversold threshold
SMA_REGIME  = 200       # only buy dips when price is above this (uptrend gate)
MAX_HOLD    = 10        # safety time-stop in trading days
ALLOC       = 10000     # $ deployed per position
COMMISSION  = 0.0       # Alpaca is commission-free; slippage modeled via FILL
SLIPPAGE    = 0.0005    # 5 bps each side, realistic for liquid ETFs


# ── Data ──────────────────────────────────────────────────────────────────────

def fetch_daily(symbol, start=START):
    """All adjusted daily bars since `start`, oldest first, handling pagination."""
    bars, token = [], None
    while True:
        params = {"timeframe": "1Day", "start": f"{start}T00:00:00Z",
                  "limit": 10000, "sort": "asc", "adjustment": "all"}
        if token:
            params["page_token"] = token
        r = requests.get(f"https://data.alpaca.markets/v2/stocks/{symbol}/bars",
                         headers=DATA_HEADERS, params=params, timeout=30)
        if not r.ok:
            print(f"  data error {symbol}: {r.status_code} {r.text[:120]}")
            break
        j = r.json()
        bars.extend(j.get("bars") or [])
        token = j.get("next_page_token")
        if not token:
            break
    return bars


# ── Indicators ────────────────────────────────────────────────────────────────

def rsi(closes, period):
    """Wilder's RSI for the last value given a window of closes (len > period)."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(-period, 0):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0))
        losses.append(max(-ch, 0))
    avg_g = sum(gains) / period
    avg_l = sum(losses) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - (100 / (1 + rs))


def ibs(bar):
    rng = bar["h"] - bar["l"]
    return (bar["c"] - bar["l"]) / rng if rng > 0 else 0.5


# ── Backtest one symbol ───────────────────────────────────────────────────────

def backtest_symbol(symbol, bars):
    """Returns (trades, equity_curve_daily_returns_list)."""
    trades = []
    in_pos = False
    entry_price = entry_date = qty = 0
    hold_days = 0

    for i in range(1, len(bars)):
        today = bars[i]
        prev  = bars[i - 1]
        closes = [b["c"] for b in bars[:i + 1]]

        # ── Manage open position FIRST (exit decisions use today's close) ──
        if in_pos:
            hold_days += 1
            exit_now = today["c"] > prev["h"] or hold_days >= MAX_HOLD
            if exit_now:
                exit_price = today["c"] * (1 - SLIPPAGE)   # sell with slippage
                pnl = (exit_price - entry_price) * qty - COMMISSION
                trades.append({
                    "symbol": symbol,
                    "entry_date": entry_date, "entry": entry_price,
                    "exit_date": today["t"][:10], "exit": exit_price,
                    "qty": qty, "pnl": pnl,
                    "ret_pct": (exit_price / entry_price - 1) * 100,
                    "held": hold_days,
                    "reason": "close>prevHigh" if today["c"] > prev["h"] else "time-stop",
                })
                in_pos = False
            continue   # one action per day

        # ── Entry signal (uses only data through today's close) ──
        if i < SMA_REGIME:
            continue
        sma200 = sum(closes[-SMA_REGIME:]) / SMA_REGIME
        r2 = rsi(closes, 2)
        if r2 is None:
            continue
        if ibs(today) < IBS_MAX and r2 < RSI2_MAX and today["c"] > sma200:
            entry_price = today["c"] * (1 + SLIPPAGE)   # buy at close + slippage
            qty = ALLOC / entry_price
            entry_date = today["t"][:10]
            hold_days = 0
            in_pos = True

    return trades


# ── Metrics ───────────────────────────────────────────────────────────────────

def buy_hold_return(bars):
    if len(bars) < 2:
        return 0.0
    return (bars[-1]["c"] / bars[0]["c"] - 1) * 100


def summarize(all_trades, bars_by_symbol):
    if not all_trades:
        return "No trades generated over the window."

    wins   = [t for t in all_trades if t["pnl"] > 0]
    losses = [t for t in all_trades if t["pnl"] <= 0]
    total_pnl = sum(t["pnl"] for t in all_trades)
    gross_w = sum(t["pnl"] for t in wins)
    gross_l = sum(t["pnl"] for t in losses)
    pf = abs(gross_w / gross_l) if gross_l != 0 else float("inf")
    wr = len(wins) / len(all_trades) * 100
    avg_w = (gross_w / len(wins)) if wins else 0
    avg_l = (gross_l / len(losses)) if losses else 0
    rets  = [t["ret_pct"] for t in all_trades]
    avg_r = sum(rets) / len(rets)

    # Per-trade Sharpe proxy (mean/stdev of trade returns, annualized by trades/yr)
    if len(rets) > 1:
        sd = statistics.stdev(rets)
        # estimate trades per year
        days = sum(len(b) for b in bars_by_symbol.values()) / len(bars_by_symbol)
        yrs = days / 252
        tpy = len(all_trades) / yrs if yrs else len(all_trades)
        sharpe = (avg_r / sd) * math.sqrt(tpy) if sd > 0 else 0
    else:
        sharpe = 0

    # Equity curve & max drawdown (sequential, trades sorted by exit date)
    seq = sorted(all_trades, key=lambda t: t["exit_date"])
    equity = ALLOC
    peak = equity
    max_dd = 0
    for t in seq:
        equity += t["pnl"]
        peak = max(peak, equity)
        dd = (peak - equity) / peak * 100
        max_dd = max(max_dd, dd)

    # window for CAGR
    first = min(t["entry_date"] for t in all_trades)
    last  = max(t["exit_date"] for t in all_trades)
    d0 = datetime.strptime(first, "%Y-%m-%d")
    d1 = datetime.strptime(last, "%Y-%m-%d")
    yrs = max((d1 - d0).days / 365.25, 0.5)
    # Strategy return on the capital actually committed (one ALLOC unit recycled)
    strat_ret = total_pnl / ALLOC * 100
    cagr = ((1 + strat_ret / 100) ** (1 / yrs) - 1) * 100 if strat_ret > -100 else -100

    bh = {s: buy_hold_return(b) for s, b in bars_by_symbol.items()}
    bh_avg = sum(bh.values()) / len(bh)

    out = []
    out.append(f"Window: {first} → {last}  ({yrs:.1f} years)")
    out.append(f"Trades: {len(all_trades)}  ({len(all_trades)/yrs:.0f}/yr)")
    out.append(f"Win rate: {wr:.1f}%   Profit factor: {pf:.2f}")
    out.append(f"Avg win: ${avg_w:+.2f}   Avg loss: ${avg_l:+.2f}   Avg trade: {avg_r:+.2f}%")
    out.append(f"Total P&L (one ${ALLOC:,} unit recycled): ${total_pnl:+,.2f}  ({strat_ret:+.1f}%)")
    out.append(f"Approx CAGR on committed capital: {cagr:+.1f}%")
    out.append(f"Max drawdown (closed-trade equity): {max_dd:.1f}%")
    out.append(f"Per-trade Sharpe (annualized): {sharpe:.2f}")
    out.append("")
    out.append("Buy-and-hold over same window:")
    for s, v in bh.items():
        out.append(f"  {s}: {v:+.1f}%")
    out.append(f"  avg: {bh_avg:+.1f}%")
    out.append("")
    out.append("NOTE: strategy is in the market only ~5-15% of the time (cash otherwise),")
    out.append("so compare risk-adjusted return & drawdown, not just total return vs B&H.")
    return "\n".join(out)


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    print(f"Master Strategy backtest — {', '.join(SYMBOLS)} since {START}\n")
    print(f"Rules: IBS<{IBS_MAX} AND RSI2<{RSI2_MAX} AND Close>SMA{SMA_REGIME} → buy;"
          f" exit Close>prevHigh or {MAX_HOLD}d stop\n")

    all_trades = []
    bars_by_symbol = {}
    for sym in SYMBOLS:
        bars = fetch_daily(sym)
        if len(bars) < SMA_REGIME + 5:
            print(f"  {sym}: only {len(bars)} bars — skipping")
            continue
        bars_by_symbol[sym] = bars
        t = backtest_symbol(sym, bars)
        all_trades.extend(t)
        print(f"  {sym}: {len(bars)} bars, {len(t)} trades")

    print("\n" + "=" * 64)
    print("TRADE LOG (every signal, chronological)")
    print("=" * 64)
    for t in sorted(all_trades, key=lambda x: x["entry_date"]):
        print(f"  {t['entry_date']}  BUY  {t['symbol']:4} @ ${t['entry']:7.2f}  "
              f"→  {t['exit_date']}  SELL @ ${t['exit']:7.2f}  "
              f"{t['ret_pct']:+5.2f}%  ${t['pnl']:+8.2f}  ({t['held']}d, {t['reason']})")

    print("\n" + "=" * 64)
    print("SUMMARY")
    print("=" * 64)
    summary = summarize(all_trades, bars_by_symbol)
    print(summary)

    # Save a markdown report
    report = f"# Master Strategy Backtest — {datetime.now(timezone.utc):%Y-%m-%d}\n\n"
    report += f"**Rules:** IBS<{IBS_MAX} AND RSI(2)<{RSI2_MAX} AND Close>SMA{SMA_REGIME} "
    report += f"→ buy at close; exit when Close>prevHigh or after {MAX_HOLD} days.\n\n"
    report += f"**Universe:** {', '.join(SYMBOLS)}  |  **Slippage:** {SLIPPAGE*1e4:.0f}bps/side\n\n"
    report += "## Summary\n```\n" + summary + "\n```\n\n## Trade Log\n\n"
    report += "| Entry | Sym | In | Exit | Out | Ret% | P&L | Held | Reason |\n"
    report += "|-------|-----|----|----|-----|------|-----|------|--------|\n"
    for t in sorted(all_trades, key=lambda x: x["entry_date"]):
        report += (f"| {t['entry_date']} | {t['symbol']} | ${t['entry']:.2f} | "
                   f"{t['exit_date']} | ${t['exit']:.2f} | {t['ret_pct']:+.2f} | "
                   f"${t['pnl']:+.2f} | {t['held']}d | {t['reason']} |\n")
    path = f"{BASE_DIR}/reports/backtest_master.md"
    os.makedirs(f"{BASE_DIR}/reports", exist_ok=True)
    with open(path, "w") as f:
        f.write(report)
    print(f"\nReport saved: {path}")


if __name__ == "__main__":
    run()
