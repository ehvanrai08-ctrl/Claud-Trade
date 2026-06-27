"""
Gold strategy backtest — the "support/resistance → liquidity sweep → break entry"
model from the YouTube gold video, tested on GLD (tradeable on Alpaca, unlike
spot XAU). Settles whether the strategy has an edge with DATA, not assertion.

The video's 3 steps, codified faithfully:
  1. Mark support/resistance  → recent swing high (resistance) / swing low (support)
  2. Wait for a liquidity sweep → a bar wicks BEYOND that level (stop-hunt)
  3. Enter on the break        → break-of-structure back in the reversal direction
     stop beyond the sweep extreme, target the opposite S/R zone (or trail).

Differences from the TJR test (deliberately faithful to THIS video):
  - Instrument: GLD (the video trades gold).
  - No killzone — the gold strategy runs the whole session.
  - Multiple trades per day allowed (TJR was 1/session).
  - Long AND short (the video shows both buy and sell setups).

Honest controls (same as backtest_tjr): realistic slippage, an in-sample /
out-of-sample split (#7 — a config that only works in-sample is noise), and
several reasonable lookbacks so the result isn't one cherry-picked setting.

Run: python backtest_gold.py
"""

from backtest_tjr import fetch_5m, metrics, fmt, bos, SLIPPAGE_BPS

SYMBOL   = "GLD"
START    = "2024-01-01"
NOTIONAL = 2000


def simulate_day(bars, lookback, max_wait, trail):
    """Walk one day's 5-min bars; return a list of trade dicts (may be several)."""
    trades = []
    n = len(bars)
    i = lookback
    while i < n - 1:
        window = bars[i - lookback:i]
        resistance = max(b["h"] for b in window)
        support    = min(b["l"] for b in window)
        bar = bars[i]

        # Step 2: liquidity sweep beyond a level.
        if bar["h"] > resistance:
            direction, sweep_extreme, target0 = "short", bar["h"], support
        elif bar["l"] < support:
            direction, sweep_extreme, target0 = "long", bar["l"], resistance
        else:
            i += 1
            continue

        # Step 3: wait up to max_wait bars for a break of structure in the
        # reversal direction (the "trendline break / strong-candle close").
        entry, eidx = None, None
        for j in range(i + 1, min(i + 1 + max_wait, n)):
            if bos(bars[:j + 1], direction):
                entry, eidx = bars[j]["c"], j
                break
        if entry is None:
            i += 1
            continue

        # Stop beyond the sweep extreme; structural target = opposite S/R zone.
        if direction == "short":
            stop = sweep_extreme
            target = target0
            if stop <= entry or target >= entry:   # geometry must make sense
                i = eidx + 1
                continue
        else:
            stop = sweep_extreme
            target = target0
            if stop >= entry or target <= entry:
                i = eidx + 1
                continue

        slip = SLIPPAGE_BPS / 10000.0
        init_risk = abs(entry - stop)
        fill_entry = entry * (1 + slip) if direction == "long" else entry * (1 - slip)
        be_moved = False
        exit_px, xidx = bars[-1]["c"], n - 1
        for k in range(eidx + 1, n):
            b = bars[k]
            if trail and not be_moved:
                if (direction == "long" and b["h"] >= entry + init_risk) or \
                   (direction == "short" and b["l"] <= entry - init_risk):
                    stop = entry; be_moved = True
            if trail and be_moved:
                stop = max(stop, b["c"] - init_risk) if direction == "long" \
                    else min(stop, b["c"] + init_risk)
            hit_stop   = (direction == "long" and b["l"] <= stop) or \
                         (direction == "short" and b["h"] >= stop)
            hit_target = (not trail) and ((direction == "long" and b["h"] >= target) or
                                          (direction == "short" and b["l"] <= target))
            if hit_stop:
                exit_px, xidx = stop, k; break
            if hit_target:
                exit_px, xidx = target, k; break

        fill_exit = exit_px * (1 - slip) if direction == "long" else exit_px * (1 + slip)
        qty = int(NOTIONAL // fill_entry)
        if qty >= 1:
            pnl = ((fill_exit - fill_entry) if direction == "long"
                   else (fill_entry - fill_exit)) * qty
            trades.append({"pnl": pnl, "ret": pnl / (fill_entry * qty) * 100})
        i = xidx + 1   # no overlapping trades
    return trades


def run_config(intraday, split_date, lookback, max_wait, trail):
    ins, oos = [], []
    for d in sorted(intraday.keys()):
        for t in simulate_day(sorted(intraday[d], key=lambda b: b["_et"]),
                              lookback, max_wait, trail):
            (ins if d < split_date else oos).append(t)
    return metrics(ins), metrics(oos)


if __name__ == "__main__":
    print(f"Fetching 5-min {SYMBOL} since {START} (rate-limited)…")
    intraday = fetch_5m(SYMBOL, START)
    days = sorted(intraday.keys())
    if not days:
        print("No data fetched (check .env / network)."); raise SystemExit(1)
    split = days[len(days) // 2]
    print(f"{len(days)} trading days. In-sample < {split} <= out-of-sample.\n")
    print(f"{'config':30} {'IN-SAMPLE':^52} | OUT-OF-SAMPLE")

    for lookback in (10, 20):
        for max_wait in (6, 12):
            for trail in (False, True):
                name = (f"LB{lookback} wait{max_wait} "
                        f"{'trail+BE' if trail else 'fixed-tgt'}")
                ins, oos = run_config(intraday, split, lookback, max_wait, trail)
                print(f"  {name:28} {fmt(ins)} | {fmt(oos)}")

    print("\nRead the OUT-OF-SAMPLE column. '<<<' = PF>1.2, ret/DD>1, profitable. "
          "The video's claim is a real edge; data decides if it survives unseen bars.")
