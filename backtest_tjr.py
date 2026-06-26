"""
TJR backtest — let unseen data decide, not chart-picking
========================================================
The live TJR bot (tjr_strategy.py) stacks EIGHT required gates (liquidity sweep,
5-min BOS/inverse-FVG, SMT divergence, 1-min retrace on both indices, alignment,
entry BOS on both, Fib golden-pocket, R:R>=1.5) — and has taken ZERO trades since
going live. That is exactly the failure the "I improved TJR's strategy" video
documents: complexity didn't add edge, it strangled the strategy.

This harness tests the video's concrete claims on real SPY/QQQ 5-min history:

  #1 Simplicity wins   → SIMPLE core (sweep→BOS→entry) vs FULL stack (+SMT +golden pocket)
  #4 Exits matter      → FIXED target vs TRAIL+breakeven-at-+1R (let winners run)
  #9 Long-only indices → LONG+SHORT vs LONG-ONLY
  #2 Per-asset config  → 5-min vs 15-min (resampled) bars
  #11 Real costs       → SLIPPAGE_BPS applied to every fill
  #7 Validate unseen   → split history in half; report IN-SAMPLE and OUT-OF-SAMPLE
                          separately. A config that only works in-sample is noise.

It does NOT touch the live bot. Read the OOS column, then decide.
Run: python backtest_tjr.py
"""

import os
import math
import statistics
import time
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
from dotenv import dotenv_values

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
config   = dotenv_values(f"{BASE_DIR}/.env")
DATA_HEADERS = {
    "APCA-API-KEY-ID":     config.get("ALPACA_API_KEY", ""),
    "APCA-API-SECRET-KEY": config.get("ALPACA_SECRET_KEY", ""),
}
ET = ZoneInfo("America/New_York")

SYMBOLS      = ["SPY", "QQQ"]
START        = "2024-01-01"
NOTIONAL     = 2000
SLIPPAGE_BPS = 2.0     # 2 bps (~$0.10 on a $500 share) each side — realistic for liquid ETFs
MIN_RR       = 1.5

# Killzone (ET): the bot only trades 9:30–11:00.
KZ_START = (9, 30)
KZ_END   = (11, 0)


# ── Data ──────────────────────────────────────────────────────────────────────

def fetch_5m(symbol, start):
    bars, token = [], None
    while True:
        params = {"timeframe": "5Min", "start": f"{start}T00:00:00Z",
                  "limit": 10000, "sort": "asc", "adjustment": "raw"}
        if token:
            params["page_token"] = token
        r = None
        for attempt in range(5):
            try:
                r = requests.get(f"https://data.alpaca.markets/v2/stocks/{symbol}/bars",
                                 headers=DATA_HEADERS, params=params, timeout=45)
            except requests.exceptions.RequestException:
                time.sleep(2 ** attempt); continue   # transient SSL/connection blip
            if r.status_code == 429:
                time.sleep(2 ** attempt); continue
            break
        if r is None or not r.ok:
            break
        j = r.json()
        bars.extend(j.get("bars") or [])
        token = j.get("next_page_token")
        if not token:
            break
    by_date = defaultdict(list)
    for b in bars:
        t = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(ET)
        b["_et"] = t
        by_date[t.date()].append(b)
    return by_date


def resample_15m(bars_5m):
    """Aggregate sorted 5-min bars into 15-min bars (3 per bucket, aligned to :00/:15/:30/:45)."""
    out, bucket = [], []
    def flush():
        if bucket:
            out.append({"o": bucket[0]["o"], "h": max(b["h"] for b in bucket),
                        "l": min(b["l"] for b in bucket), "c": bucket[-1]["c"],
                        "v": sum(b["v"] for b in bucket), "_et": bucket[0]["_et"]})
    for b in bars_5m:
        m = b["_et"].minute
        if m % 15 == 0 and bucket:
            flush(); bucket = []
        bucket.append(b)
    flush()
    return out


# ── Strategy primitives (5-min granularity, mirrors the live logic) ────────────

def in_kz(t):
    h, m = t.hour, t.minute
    return (h, m) >= KZ_START and (h, m) <= KZ_END


def detect_sweep(prior, last):
    """Sweep above prior high → short bias; below prior low → long bias."""
    ph = max(b["h"] for b in prior)
    pl = min(b["l"] for b in prior)
    if last["h"] > ph:
        return "short"
    if last["l"] < pl:
        return "long"
    return None


def bos(bars, direction):
    """Break of structure: close beyond the recent swing in `direction`."""
    if len(bars) < 3:
        return False
    recent = bars[-6:]
    if direction == "short":
        return recent[-1]["c"] < min(b["l"] for b in recent[:-1])
    return recent[-1]["c"] > max(b["h"] for b in recent[:-1])


def smt_divergence(a_now, a_prev, b_now, b_prev, direction):
    """One index sweeps while the other fails to confirm (the live bot's gate)."""
    if direction == "short":
        a = max(x["h"] for x in a_now) > max(x["h"] for x in a_prev)
        b = max(x["h"] for x in b_now) > max(x["h"] for x in b_prev)
    else:
        a = min(x["l"] for x in a_now) < min(x["l"] for x in a_prev)
        b = min(x["l"] for x in b_now) < min(x["l"] for x in b_prev)
    return a != b


def in_golden(price, hi, lo, direction):
    rng = hi - lo
    if rng <= 0:
        return False
    frac = (price - lo) / rng if direction == "short" else (hi - price) / rng
    return 0.5 <= frac <= 0.79


# ── One day's simulation for one config ────────────────────────────────────────

def simulate_day(prim, other, full, long_only, trail):
    """prim = primary index intraday bars (list), other = the second index (for SMT).
    Returns a trade dict or None. One trade per day max (live behaviour)."""
    kz = [b for b in prim if in_kz(b["_et"])]
    if len(kz) < 8:
        return None
    other_kz = [b for b in other if in_kz(b["_et"])]

    phase, direction, entry, stop, target, eidx = "watch", None, None, None, None, None
    for i in range(6, len(kz)):
        window = kz[:i]
        last   = kz[i]
        if phase == "watch":
            d = detect_sweep(window[-12:] if len(window) >= 12 else window, last)
            if d:
                direction, phase = d, "swept"
            continue
        if phase == "swept":
            if not bos(kz[:i + 1], direction):
                continue
            if full:
                lb = 6
                if len(window) < lb * 2 or len(other_kz) < lb * 2:
                    continue
                if not smt_divergence(kz[i - lb:i], kz[i - 2 * lb:i - lb],
                                      other_kz[-lb:], other_kz[-2 * lb:-lb], direction):
                    continue
            # Entry candidate at this bar's close.
            entry_px = last["c"]
            recent   = kz[max(0, i - 4):i + 1]
            if direction == "short":
                if long_only:
                    phase = "watch"; direction = None; continue
                stop   = max(b["h"] for b in recent)
                target = min(b["l"] for b in kz[max(0, i - 20):i + 1])
            else:
                stop   = min(b["l"] for b in recent)
                target = max(b["h"] for b in kz[max(0, i - 20):i + 1])
            if full:
                hi = max(b["h"] for b in kz[max(0, i - 20):i + 1])
                lo = min(b["l"] for b in kz[max(0, i - 20):i + 1])
                if not in_golden(entry_px, hi, lo, direction):
                    continue
            risk   = abs(entry_px - stop)
            reward = abs(target - entry_px)
            if risk <= 0 or reward / risk < MIN_RR:
                continue
            entry, eidx, phase = entry_px, i, "in"
            break

    if phase != "in":
        return None

    # Manage the trade bar-by-bar to the killzone end.
    slip = SLIPPAGE_BPS / 10000.0
    be_moved = False
    init_risk = abs(entry - stop)
    fill_entry = entry * (1 + slip) if direction == "long" else entry * (1 - slip)
    exit_px = kz[-1]["c"]
    for b in kz[eidx + 1:]:
        if trail and not be_moved:
            # Move stop to breakeven once price reaches +1R.
            if (direction == "long" and b["h"] >= entry + init_risk) or \
               (direction == "short" and b["l"] <= entry - init_risk):
                stop = entry; be_moved = True
        if trail and be_moved:
            # Trail the stop behind price by 1R, never loosening it.
            if direction == "long":
                stop = max(stop, b["c"] - init_risk)
            else:
                stop = min(stop, b["c"] + init_risk)
        hit_stop   = (direction == "long" and b["l"] <= stop) or (direction == "short" and b["h"] >= stop)
        hit_target = (not trail) and ((direction == "long" and b["h"] >= target) or
                                      (direction == "short" and b["l"] <= target))
        if hit_stop:
            exit_px = stop; break
        if hit_target:
            exit_px = target; break

    fill_exit = exit_px * (1 - slip) if direction == "long" else exit_px * (1 + slip)
    qty = int(NOTIONAL // fill_entry)
    if qty < 1:
        return None
    pnl = ((fill_exit - fill_entry) if direction == "long" else (fill_entry - fill_exit)) * qty
    return {"pnl": pnl, "ret": pnl / (fill_entry * qty) * 100, "dir": direction}


# ── Metrics ────────────────────────────────────────────────────────────────────

def max_drawdown(pnls):
    cum, peak, mdd = 0.0, 0.0, 0.0
    for p in pnls:
        cum += p; peak = max(peak, cum); mdd = max(mdd, peak - cum)
    return mdd


def metrics(trades):
    if not trades:
        return None
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    los  = [p for p in pnls if p <= 0]
    total = sum(pnls)
    pf  = abs(sum(wins) / sum(los)) if los and sum(los) else float("inf")
    wr  = len(wins) / len(pnls) * 100
    mdd = max_drawdown(pnls)
    rdd = (total / mdd) if mdd > 0 else float("inf")
    sd  = statistics.stdev([t["ret"] for t in trades]) if len(trades) > 1 else 0
    sharpe = (statistics.mean([t["ret"] for t in trades]) / sd) * math.sqrt(len(trades)) if sd > 0 else 0
    return {"n": len(pnls), "wr": wr, "pf": pf, "total": total,
            "mdd": mdd, "rdd": rdd, "sharpe": sharpe}


def run_config(intraday, intraday_other, split_date, full, long_only, trail):
    ins, oos = [], []
    for sym in SYMBOLS:
        days = sorted(intraday[sym].keys())
        for d in days:
            t = simulate_day(intraday[sym][d], intraday_other[sym].get(d, []),
                             full, long_only, trail)
            if t:
                (ins if d < split_date else oos).append(t)
    return metrics(ins), metrics(oos)


def fmt(m):
    if not m:
        return f"{'no trades':>52}"
    flag = " <<<" if (m["pf"] > 1.2 and m["rdd"] > 1.0 and m["total"] > 0) else ""
    return (f"n={m['n']:4} WR={m['wr']:4.1f}% PF={m['pf']:4.2f} "
            f"ret/DD={m['rdd']:5.2f} Sharpe={m['sharpe']:5.2f} ${m['total']:+8.0f}{flag}")


if __name__ == "__main__":
    print(f"Fetching 5-min SPY/QQQ since {START} (rate-limited)…")
    intraday = {}
    for s in SYMBOLS:
        intraday[s] = fetch_5m(s, START)
        time.sleep(0.2)
    # 15-min variants, resampled per day.
    intraday_15 = {s: {d: resample_15m(sorted(intraday[s][d], key=lambda b: b["_et"]))
                       for d in intraday[s]} for s in SYMBOLS}
    # The "other" index for SMT: SPY's other is QQQ and vice-versa.
    other = {"SPY": intraday["QQQ"], "QQQ": intraday["SPY"]}
    other15 = {"SPY": intraday_15["QQQ"], "QQQ": intraday_15["SPY"]}

    all_days = sorted({d for s in SYMBOLS for d in intraday[s]})
    if not all_days:
        print("No data fetched (check .env / network)."); raise SystemExit(1)
    split = all_days[len(all_days) // 2]
    print(f"{len(all_days)} trading days. In-sample < {split} <= out-of-sample.\n")
    print(f"{'config':38} {'IN-SAMPLE':^52} | OUT-OF-SAMPLE")

    combos = [(full, lo, tr) for full in (False, True)
              for lo in (False, True) for tr in (False, True)]
    for tf_name, data, oth in (("5m", intraday, other), ("15m", intraday_15, other15)):
        print(f"\n── {tf_name} bars ──")
        for full, lo, tr in combos:
            name = (f"{'FULL' if full else 'SIMPLE':6} "
                    f"{'long-only' if lo else 'long+short':10} "
                    f"{'trail+BE' if tr else 'fixed-tgt':9}")
            ins, oos = run_config(data, oth, split, full, lo, tr)
            print(f"  {name:36} {fmt(ins)} | {fmt(oos)}")

    print("\nRead the OUT-OF-SAMPLE column. A config that wins in-sample but dies "
          "out-of-sample (#7) is overfit noise. '<<<' = PF>1.2, ret/DD>1, profitable.")
