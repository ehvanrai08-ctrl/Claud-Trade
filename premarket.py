"""
Pre-market game plan — runs at 9:00 AM ET each trading day.

Scores the SIP-ORB universe by yesterday's absolute return (the best
available pre-market proxy for opening relative volume) and writes a
brief watchlist plan to premarket_plan.md before the open.

Why |yesterday return|: stocks that moved hard yesterday are the most
likely candidates for today's Stocks-in-Play universe. RVol can't be
computed until the opening bar prints, so this is the best pre-open
filter available without a news/catalyst API.
"""

import logging
import os
import requests
from datetime import datetime, timedelta, timezone
from dotenv import dotenv_values

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
config   = dotenv_values(f"{BASE_DIR}/.env")

BASE_URL = config["ALPACA_BASE_URL"]
HEADERS  = {
    "APCA-API-KEY-ID":     config["ALPACA_API_KEY"],
    "APCA-API-SECRET-KEY": config["ALPACA_SECRET_KEY"],
}
DATA_HEADERS = {
    "APCA-API-KEY-ID":     config["ALPACA_API_KEY"],
    "APCA-API-SECRET-KEY": config["ALPACA_SECRET_KEY"],
}

logging.basicConfig(
    filename=f"{BASE_DIR}/premarket.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger()

# Same universe as sip_orb.py
UNIVERSE = [
    "AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA","AVGO","JPM","V",
    "UNH","XOM","LLY","WMT","MA","JNJ","PG","HD","MRK","ABBV",
    "CVX","CRM","COST","NFLX","AMD","ORCL","MCD","ACN","ADBE","INTC",
    "PEP","TMO","ABT","NKE","DHR","TXN","PM","AMGN","NEE","RTX",
    "QCOM","UNP","BMY","HON","MS","GS","BLK","AXP","SPGI","ISRG",
    "LMT","CAT","DE","MMM","GE","BA","AMT","GILD","VRTX","REGN",
    "SLB","MU","AMAT","KLAC","LRCX","MRVL","PANW","CRWD","ZS","SNOW",
    "SHOP","SQ","PYPL","COIN","HOOD","RBLX","RIVN","LCID","PLTR","SOFI",
    "SPY","QQQ","IWM","XLK","XLF","XLE","ARKK","SQQQ","TQQQ","UVXY",
]
MARKET_ETFs = ["SPY", "QQQ", "IWM", "VIX"]

TOP_N = 15


def fetch_daily_bars(symbols, days=5):
    """Fetch last `days` daily bars for each symbol in batches of 50."""
    start = (datetime.now(timezone.utc) - timedelta(days=days + 10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = {}
    for i in range(0, len(symbols), 50):
        chunk = symbols[i:i+50]
        r = requests.get(
            "https://data.alpaca.markets/v2/stocks/bars",
            headers=DATA_HEADERS,
            params={
                "symbols":   ",".join(chunk),
                "timeframe": "1Day",
                "start":     start,
                "limit":     10,
                "sort":      "asc",
                "adjustment":"all",
            },
        )
        if r.ok:
            for sym, bars in r.json().get("bars", {}).items():
                result[sym] = bars
    return result


def market_is_open():
    r = requests.get(f"{BASE_URL}/clock", headers=HEADERS)
    return r.json().get("is_open", False)


def score_universe(bars_by_sym):
    """Score each symbol by |yesterday return|, 5-day momentum, and avg volume."""
    candidates = []
    for sym, bars in bars_by_sym.items():
        if len(bars) < 2:
            continue
        yesterday = bars[-1]
        prev      = bars[-2]
        if prev["c"] <= 0:
            continue
        day_ret   = (yesterday["c"] / prev["c"] - 1) * 100
        vol       = yesterday["v"]
        avg_vol   = sum(b["v"] for b in bars) / len(bars)
        rvol_est  = vol / avg_vol if avg_vol > 0 else 1.0
        mom_5d    = (yesterday["c"] / bars[0]["c"] - 1) * 100 if bars[0]["c"] > 0 else 0
        candidates.append({
            "symbol":   sym,
            "close":    yesterday["c"],
            "day_ret":  day_ret,
            "rvol_est": rvol_est,
            "mom_5d":   mom_5d,
            "avg_vol":  avg_vol,
        })
    # Rank by absolute yesterday return (best pre-market proxy for tomorrow's RVol)
    candidates.sort(key=lambda x: abs(x["day_ret"]), reverse=True)
    return candidates


def market_context(bars_by_sym):
    """SPY/QQQ trend summary."""
    lines = []
    for sym in ("SPY", "QQQ", "IWM"):
        bars = bars_by_sym.get(sym, [])
        if len(bars) < 2:
            continue
        ret = (bars[-1]["c"] / bars[-2]["c"] - 1) * 100
        trend = "↑" if ret > 0 else "↓"
        lines.append(f"{sym}: ${bars[-1]['c']:.2f} ({ret:+.2f}% yesterday) {trend}")
    return "\n".join(lines) if lines else "(no market data)"


def write_plan(candidates, mkt_ctx):
    top = candidates[:TOP_N]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    lines = [
        f"# Pre-Market Game Plan — {today}",
        "",
        "## Market Context (yesterday's close)",
        mkt_ctx,
        "",
        f"## Top {TOP_N} SIP-ORB Candidates (ranked by |yesterday return|)",
        "",
        "| Rank | Symbol | Close | Day Ret% | Est RVol | 5d Mom% | Avg Vol |",
        "|------|--------|-------|----------|----------|---------|---------|",
    ]
    for i, c in enumerate(top, 1):
        direction = "↑ LONG" if c["day_ret"] > 0 else "↓ SHORT"
        lines.append(
            f"| {i:2} | {c['symbol']:6} | ${c['close']:7.2f} | {c['day_ret']:+6.2f}% {direction} "
            f"| {c['rvol_est']:.1f}x | {c['mom_5d']:+5.1f}% | {int(c['avg_vol']):,} |"
        )

    lines += [
        "",
        "## Notes",
        "- SIP-ORB enters at 9:35 AM ET on the opening 5-min bar breakout.",
        "- The actual RVol filter (>1.5×) runs live at 9:35; these are pre-market estimates.",
        "- Direction shown is yesterday's bias — opening gap may reverse it.",
        "- ORB strategy trades QQQ only; watch QQQ day return for regime context.",
        "",
        f"Generated at {datetime.now(timezone.utc).strftime('%H:%M UTC')}",
    ]

    path = f"{BASE_DIR}/premarket_plan.md"
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


def run():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log.info(f"Pre-market scan starting — {today}")

    clock_r = requests.get(f"{BASE_URL}/clock", headers=HEADERS)
    clock   = clock_r.json() if clock_r.ok else {}
    if clock.get("is_open"):
        log.info("Market already open — pre-market scan running late, proceeding anyway.")

    # Fetch yesterday's bars for the full universe
    all_syms   = list(dict.fromkeys(UNIVERSE + ["SPY", "QQQ", "IWM"]))
    bars_by_sym = fetch_daily_bars(all_syms, days=7)
    log.info(f"Fetched bars for {len(bars_by_sym)} symbols")

    mkt_ctx    = market_context(bars_by_sym)
    candidates = score_universe(bars_by_sym)
    log.info(f"Scored {len(candidates)} candidates; top: {[c['symbol'] for c in candidates[:5]]}")

    path = write_plan(candidates, mkt_ctx)
    log.info(f"Plan written to {path}")
    print(f"[PREMARKET] Plan written: {path}")
    print(mkt_ctx)
    print(f"Top 5 candidates: {[c['symbol'] for c in candidates[:5]]}")


if __name__ == "__main__":
    run()
