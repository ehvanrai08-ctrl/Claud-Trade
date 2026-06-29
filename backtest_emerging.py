"""
Backtest — Emerging-Growth Momentum Basket
==========================================
The "find companies early that become huge" idea, in a DEFENSIBLE, tradeable
form: instead of fortune-telling one stock, hold a diversified basket of
emerging high-growth names and let momentum decide which to own, so a few big
winners carry the basket while losers are rotated out.

Rule (monthly): rank the universe by ensembled 6–12mo momentum; hold the top N
that are also above their 200-day SMA (trend gate); equal-weight; go to cash for
any slot whose best name fails the trend gate.

HONESTY ON SURVIVORSHIP BIAS: the universe below is today's known emerging
winners, so an absolute return vs SPY is flattering (we picked names that
survived). The benchmark that MATTERS and is bias-neutral is **equal-weight
buy-and-hold of the SAME universe** — both arms see the identical names, so
beating it isolates whether momentum SELECTION adds value over just owning the
whole basket. We print all three: strategy, EW-hold-universe, SPY.

Run: python backtest_emerging.py
"""

import time
from backtest_research import fetch_daily, perf_from_daily_returns, show

# Emerging / high-growth names with meaningful Alpaca history (~2016+). Mixed
# IPO vintages; names without enough history are simply ineligible until they
# have it. TSLA/NVDA/AMD/etc. that other bots already own are EXCLUDED to avoid
# overlap — this is the "next tier" basket.
UNIVERSE = [
    "SHOP", "SQ", "MELI", "NOW", "TEAM", "NFLX", "ISRG", "WDAY", "VEEV",
    "OKTA", "ZS", "TTD", "ROKU", "DDOG", "CRWD", "NET", "SNOW", "ABNB",
    "DASH", "PLTR", "U", "RBLX", "COIN", "HOOD", "MDB", "ZM", "DOCU",
    "PINS", "SNAP", "TWLO",
]

TOP_N           = 6
LOOKBACK_MONTHS = [6, 9, 12]
TD_PER_MONTH    = 21
SMA_TREND       = 200
SLIP            = 0.0005


def build_date_index(data):
    """Union of all trading dates across the universe, sorted ascending."""
    dates = set()
    for bars in data.values():
        dates.update(b["d"] for b in bars)
    return sorted(dates)


def closes_by_date(bars):
    return {b["d"]: b["c"] for b in bars}


def momentum(closes_list):
    """Ensembled trailing return, or None if too little history."""
    longest = max(LOOKBACK_MONTHS) * TD_PER_MONTH
    if len(closes_list) < longest + 1:
        return None
    now = closes_list[-1]
    rs = []
    for m in LOOKBACK_MONTHS:
        past = closes_list[-1 - m * TD_PER_MONTH]
        if past > 0:
            rs.append(now / past - 1)
    return sum(rs) / len(rs) if rs else None


def run_basket(data, dates, select=True):
    """Daily return series. If select=True, monthly momentum top-N + trend gate.
    If select=False, equal-weight buy-and-hold of the whole universe (the
    bias-neutral benchmark)."""
    cbd = {s: closes_by_date(b) for s, b in data.items()}
    # ascending close lists per symbol up to each date, built incrementally
    hist = {s: [] for s in data}
    last_seen = {s: None for s in data}

    rets = []
    holdings = []          # list of symbols held this month
    prev_prices = {}       # symbol -> price at last date (for daily return)
    month = None

    for di, d in enumerate(dates):
        # update history with today's close where present
        for s in data:
            px = cbd[s].get(d)
            if px is not None:
                hist[s].append(px)
                last_seen[s] = px

        # daily return of current holdings (equal weight), using held names that
        # have a price today and yesterday
        day_ret = 0.0
        if holdings:
            contribs = []
            for s in holdings:
                p_now = cbd[s].get(d)
                p_prev = prev_prices.get(s)
                if p_now is not None and p_prev:
                    contribs.append(p_now / p_prev - 1)
            day_ret = sum(contribs) / len(holdings) if contribs else 0.0
        rets.append(day_ret)

        # remember today's prices for tomorrow's return calc
        for s in data:
            if cbd[s].get(d) is not None:
                prev_prices[s] = cbd[s][d]

        # rebalance at the first trading day of a new month
        ym = d[:7]
        if ym != month:
            month = ym
            if select:
                scored = []
                for s in data:
                    mom = momentum(hist[s])
                    if mom is None:
                        continue
                    sma = (sum(hist[s][-SMA_TREND:]) / SMA_TREND
                           if len(hist[s]) >= SMA_TREND else None)
                    if sma is None or hist[s][-1] <= sma:
                        continue   # trend gate
                    scored.append((s, mom))
                scored.sort(key=lambda x: x[1], reverse=True)
                new_holdings = [s for s, _ in scored[:TOP_N]]
            else:
                # EW hold: own every name that has any history so far
                new_holdings = [s for s in data if hist[s]]

            # turnover slippage
            if set(new_holdings) != set(holdings):
                rets[-1] -= SLIP
            holdings = new_holdings

    return rets


if __name__ == "__main__":
    print(f"Fetching {len(UNIVERSE)} emerging names + SPY (this takes a bit)…")
    data = {}
    for s in UNIVERSE:
        b = fetch_daily(s)
        if b and len(b) > 260:     # need at least ~1yr to be useful
            data[s] = b
        else:
            print(f"  {s}: insufficient history, dropped")
        time.sleep(0.1)
    spy = fetch_daily("SPY")
    spy_bh = [spy[i]["c"]/spy[i-1]["c"]-1 for i in range(1, len(spy))]
    bench = perf_from_daily_returns(spy_bh)

    dates = build_date_index(data)
    print(f"\n{len(data)} names, {dates[0]}→{dates[-1]} ({len(dates)} trading days)\n")
    show("SPY buy & hold", bench)

    ew = run_basket(data, dates, select=False)
    ew_m = perf_from_daily_returns(ew)
    show("EW hold universe (bias-neutral)", ew_m, bench)

    strat = run_basket(data, dates, select=True)
    strat_m = perf_from_daily_returns(strat)
    show(f"Momentum top-{TOP_N} + trend gate", strat_m, bench)

    print("\nVERDICT GUIDE:")
    print("  - Beats SPY → tradeable edge vs the index.")
    print("  - Beats EW-hold-universe → momentum SELECTION adds value (the")
    print("    bias-neutral test; this is what actually matters).")
    print("  - Loses to EW-hold → selection adds nothing; just survivorship.")
    if strat_m and ew_m:
        sel_alpha = strat_m["sharpe"] - ew_m["sharpe"]
        print(f"\n  Selection alpha (Sharpe): {sel_alpha:+.2f} "
              f"({'momentum helps' if sel_alpha > 0 else 'momentum does NOT help'})")
