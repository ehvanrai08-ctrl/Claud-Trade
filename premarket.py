"""
Pre-market game plan — runs at 9:00 AM ET each trading day.

Scores the SIP-ORB universe by yesterday's absolute return (the best
available pre-market proxy for opening relative volume) and writes a
brief watchlist plan to premarket_plan.md before the open.

Also writes premarket_signals.json — a machine-readable market regime
snapshot consumed by IBS and RSI(2) to scale position size:
  market_regime: "bull" | "neutral" | "bear"
  spy_5d_mom, qqq_5d_mom: 5-day return %
  spy_above_200d, qqq_above_200d: bool

Why |yesterday return|: stocks that moved hard yesterday are the most
likely candidates for today's Stocks-in-Play universe. RVol can't be
computed until the opening bar prints, so this is the best pre-open
filter available without a news/catalyst API.
"""

import json
import logging
import os
import requests
from datetime import datetime, timedelta, timezone
from dotenv import dotenv_values

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
config   = dotenv_values(f"{BASE_DIR}/.env")

# Use .get() at module level so importing this file (e.g. IBS/RSI2 calling
# read_signals()) can never KeyError on a missing/late .env. The functions that
# actually hit the API degrade gracefully on a bad key (r.ok is False); a hard
# subscript here would crash any bot that merely imports us. (CLAUDE.md gotcha #1)
BASE_URL = config.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2")
HEADERS  = {
    "APCA-API-KEY-ID":     config.get("ALPACA_API_KEY", ""),
    "APCA-API-SECRET-KEY": config.get("ALPACA_SECRET_KEY", ""),
}
DATA_HEADERS = {
    "APCA-API-KEY-ID":     config.get("ALPACA_API_KEY", ""),
    "APCA-API-SECRET-KEY": config.get("ALPACA_SECRET_KEY", ""),
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
    """Fetch last `days` daily bars for each symbol in batches of 50.
    limit is set to days+15 so ascending-sort returns enough history
    for 5d momentum AND 200d SMA when days=210."""
    start = (datetime.now(timezone.utc) - timedelta(days=days + 10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    limit = min(days + 15, 1000)
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
                "limit":     limit,
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


SIGNALS_FILE = f"{BASE_DIR}/premarket_signals.json"


def fetch_regime_bars(symbol, days=220):
    """Fetch daily bars for a single symbol with pagination — needed for 200d SMA.
    The batch endpoint doesn't paginate per-symbol, so we use the single endpoint."""
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    bars, token = [], None
    while True:
        params = {"timeframe": "1Day", "start": start, "limit": 1000,
                  "sort": "asc", "adjustment": "all"}
        if token:
            params["page_token"] = token
        r = requests.get(
            f"https://data.alpaca.markets/v2/stocks/{symbol}/bars",
            headers=DATA_HEADERS, params=params, timeout=30,
        )
        if not r.ok:
            break
        j = r.json()
        bars.extend(j.get("bars") or [])
        token = j.get("next_page_token")
        if not token:
            break
    return bars


def compute_regime(bars_by_sym=None):
    """
    Derive a market regime from SPY and QQQ bars.
    Fetches its own 220-day history for SPY/QQQ via the paginated
    single-symbol endpoint (the batch endpoint can't return 200+ bars
    per symbol reliably). bars_by_sym is accepted but not used for regime.

    Regime rules (both must agree for non-neutral):
      bull  — both SPY and QQQ: 5d mom > +1%  AND  close > 200d SMA
      bear  — either SPY or QQQ: 5d mom < -1%  OR  close < 200d SMA
      neutral — mixed signals

    Notional scaling in IBS/RSI2:
      bull    → 1.25×  (lean in when everything lines up)
      neutral → 1.0×   (base size)
      bear    → 0.5×   (size down; RSI2 also has a 200d hard block)
    """
    def sym_stats(symbol):
        bars = fetch_regime_bars(symbol, days=220)
        if len(bars) < 10:
            return None
        close    = bars[-1]["c"]
        close_5d = bars[-6]["c"] if len(bars) >= 6 else bars[0]["c"]
        mom_5d   = (close / close_5d - 1) * 100 if close_5d else 0
        sma_200  = sum(b["c"] for b in bars[-200:]) / min(len(bars), 200)
        return {"close": close, "mom_5d": mom_5d, "above_200d": close > sma_200}

    spy = sym_stats("SPY")
    qqq = sym_stats("QQQ")

    if spy is None or qqq is None:
        regime = "neutral"
    elif (spy["mom_5d"] > 1.0 and qqq["mom_5d"] > 1.0
          and spy["above_200d"] and qqq["above_200d"]):
        regime = "bull"
    elif (spy["mom_5d"] < -1.0 or qqq["mom_5d"] < -1.0
          or not spy["above_200d"] or not qqq["above_200d"]):
        regime = "bear"
    else:
        regime = "neutral"

    signals = {
        "date":           datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "market_regime":  regime,
        "spy_5d_mom":     round(spy["mom_5d"], 2) if spy else 0,
        "qqq_5d_mom":     round(qqq["mom_5d"], 2) if qqq else 0,
        "spy_above_200d": spy["above_200d"] if spy else True,
        "qqq_above_200d": qqq["above_200d"] if qqq else True,
        "top_movers":     [],   # filled by caller
    }
    return signals


def write_signals(signals):
    with open(SIGNALS_FILE, "w") as f:
        json.dump(signals, f, indent=2)
    return SIGNALS_FILE


def read_signals():
    """Read today's premarket_signals.json. Returns default neutral if missing/stale."""
    default = {"market_regime": "neutral", "spy_5d_mom": 0, "qqq_5d_mom": 0,
               "spy_above_200d": True, "qqq_above_200d": True}
    try:
        with open(SIGNALS_FILE) as f:
            data = json.load(f)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if data.get("date") != today:
            return default          # stale file — treat as neutral, don't block trades
        return data
    except Exception:
        return default


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

    # Fetch recent bars for the screener (7 days is enough for score_universe).
    # compute_regime fetches its own deep history (220d) for SPY/QQQ separately.
    all_syms    = list(dict.fromkeys(UNIVERSE + ["SPY", "QQQ", "IWM"]))
    bars_by_sym = fetch_daily_bars(all_syms, days=7)
    log.info(f"Fetched bars for {len(bars_by_sym)} symbols")

    mkt_ctx    = market_context(bars_by_sym)
    candidates = score_universe(bars_by_sym)
    log.info(f"Scored {len(candidates)} candidates; top: {[c['symbol'] for c in candidates[:5]]}")

    # Compute and persist machine-readable market regime for IBS/RSI2.
    signals = compute_regime(bars_by_sym)
    signals["top_movers"] = [c["symbol"] for c in candidates[:10]]
    sig_path = write_signals(signals)
    log.info(f"Regime: {signals['market_regime']} | SPY 5d {signals['spy_5d_mom']:+.1f}% "
             f"| QQQ 5d {signals['qqq_5d_mom']:+.1f}% | written {sig_path}")

    path = write_plan(candidates, mkt_ctx)
    log.info(f"Plan written to {path}")
    print(f"[PREMARKET] Plan written: {path}")
    print(f"[PREMARKET] Regime: {signals['market_regime']} | "
          f"SPY 5d {signals['spy_5d_mom']:+.1f}% | QQQ 5d {signals['qqq_5d_mom']:+.1f}%")
    print(mkt_ctx)
    print(f"Top 5 candidates: {[c['symbol'] for c in candidates[:5]]}")


if __name__ == "__main__":
    run()
