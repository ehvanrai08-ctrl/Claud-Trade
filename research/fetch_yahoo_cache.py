"""Fetch ~11y of daily bars for the research universe from Yahoo's keyless v8
chart API into per-symbol JSON caches. Adjusted close included for total-return
math; raw OHLC kept for intraday-pattern signals (IBS etc.)."""
import json, os, time, requests

HERE  = os.path.dirname(os.path.abspath(__file__))
CACHE = f"{HERE}/cache"
os.makedirs(CACHE, exist_ok=True)

SYMBOLS = [
    # core index
    "SPY", "QQQ", "DIA", "IWM", "RSP",
    # sectors (9 classic + XLRE)
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLP", "XLU", "XLY", "XLB", "XLRE", "VNQ",
    # bonds / cash
    "TLT", "IEF", "SHY", "AGG", "LQD", "HYG", "TIP", "BIL",
    # international
    "EFA", "EEM", "EWJ", "FEZ",
    # commodities / fx
    "GLD", "SLV", "DBC", "USO", "UUP",
    # factors / style / dividend
    "MTUM", "USMV", "QUAL", "VLUE", "SPLV", "IWF", "IWD", "SCHD", "VIG", "NOBL",
    # industry
    "SMH",
    # signal-only (not tradeable)
    "^VIX",
]

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}

def fetch(sym):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
    r = requests.get(url, headers=UA, timeout=30, params={
        "range": "11y", "interval": "1d", "events": "div,split"})
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    ts  = res["timestamp"]
    q   = res["indicators"]["quote"][0]
    adj = res["indicators"].get("adjclose", [{}])[0].get("adjclose") or q["close"]
    rows = []
    for i, t in enumerate(ts):
        o, h, l, c, a, v = (q["open"][i], q["high"][i], q["low"][i],
                            q["close"][i], adj[i], q["volume"][i])
        if None in (o, h, l, c, a):
            continue
        d = time.strftime("%Y-%m-%d", time.gmtime(t - 5 * 3600))  # ET-ish date
        rows.append({"d": d, "o": o, "h": h, "l": l, "c": c, "a": a, "v": v or 0})
    return rows

ok, fail = 0, []
for sym in SYMBOLS:
    fname = f"{CACHE}/{sym.replace('^','_')}.json"
    if os.path.exists(fname):
        ok += 1
        continue
    try:
        rows = fetch(sym)
        if len(rows) < 500:
            raise RuntimeError(f"only {len(rows)} rows")
        with open(fname, "w") as f:
            json.dump(rows, f)
        print(f"{sym:6} {len(rows):5} bars  {rows[0]['d']} .. {rows[-1]['d']}")
        ok += 1
    except Exception as e:
        print(f"{sym:6} FAILED: {e}")
        fail.append(sym)
    time.sleep(0.4)

print(f"\n{ok}/{len(SYMBOLS)} cached; failed: {fail or 'none'}")
