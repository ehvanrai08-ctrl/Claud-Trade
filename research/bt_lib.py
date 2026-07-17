"""bt_lib — shared, look-ahead-safe backtest core for the research sweep.

Contract every strategy uses:
    data = align(["SPY", "TLT", ...])     # common trading dates + per-symbol bars
    w    = [...]                          # one {sym: weight} dict PER DATE, decided
                                          # using ONLY data up to and including that
                                          # date's close (index i may look at [:i+1])
    res  = run(data, w)                   # weight[t] earns the t->t+1 adj-close
                                          # return, minus 5bps per unit turnover
    print(json.dumps(res))

- Returns use ADJUSTED close (dividends/splits). Raw o/h/l/c available for
  intraday-pattern signals (IBS etc.) but never for return math.
- run() also reports the same-period SPY buy-and-hold benchmark and a
  first-half/second-half Sharpe split (cheap robustness check).
- null_basket(syms) = equal-weight, monthly-rebalanced, never-timed holding of
  the same universe — the honest null for any allocation/timing strategy.
"""
import json, math, os

HERE  = os.path.dirname(os.path.abspath(__file__))
CACHE = f"{HERE}/cache"
COST  = 0.0005   # 5 bps per unit of turnover (each side), repo convention
TDY   = 252


def load(sym):
    """{'dates': [...], 'o': [...], 'h': [...], 'l': [...], 'c': [...], 'a': [...], 'v': [...]}"""
    with open(f"{CACHE}/{sym.replace('^', '_')}.json") as f:
        rows = json.load(f)
    out = {"dates": [r["d"] for r in rows]}
    for k in ("o", "h", "l", "c", "a", "v"):
        out[k] = [r[k] for r in rows]
    return out


def align(syms, start=None, end=None):
    """Intersect dates across symbols. Returns {'dates': [...], sym: {o,h,l,c,a,v}}."""
    raw = {s: load(s) for s in syms}
    common = set(raw[syms[0]]["dates"])
    for s in syms[1:]:
        common &= set(raw[s]["dates"])
    dates = sorted(d for d in common
                   if (start is None or d >= start) and (end is None or d <= end))
    out = {"dates": dates}
    for s in syms:
        idx = {d: i for i, d in enumerate(raw[s]["dates"])}
        out[s] = {k: [raw[s][k][idx[d]] for d in dates]
                  for k in ("o", "h", "l", "c", "a", "v")}
    return out


def _metrics(dates, rets):
    n = len(rets)
    if n < 50:
        return {"error": f"only {n} return days"}
    eq, peak, maxdd = 1.0, 1.0, 0.0
    curve = []
    for r in rets:
        eq *= (1 + r)
        peak = max(peak, eq)
        maxdd = max(maxdd, 1 - eq / peak)
        curve.append(eq)
    mean = sum(rets) / n
    var  = sum((r - mean) ** 2 for r in rets) / max(n - 1, 1)
    sd   = math.sqrt(var)
    shp  = (mean / sd) * math.sqrt(TDY) if sd > 0 else 0.0
    cagr = eq ** (TDY / n) - 1
    h    = n // 2
    def half_sharpe(xs):
        if len(xs) < 30:
            return None
        m = sum(xs) / len(xs)
        s = math.sqrt(sum((x - m) ** 2 for x in xs) / max(len(xs) - 1, 1))
        return round((m / s) * math.sqrt(TDY), 3) if s > 0 else 0.0
    return {"sharpe": round(shp, 3), "cagr": round(cagr * 100, 2),
            "maxdd": round(maxdd * 100, 2), "total_return": round((eq - 1) * 100, 1),
            "sharpe_1st_half": half_sharpe(rets[:h]),
            "sharpe_2nd_half": half_sharpe(rets[h:]),
            "days": n, "start": dates[1], "end": dates[-1]}


def _corr(a, b):
    n = min(len(a), len(b))
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    ca = [x - ma for x in a]; cb = [x - mb for x in b]
    den = math.sqrt(sum(x * x for x in ca) * sum(x * x for x in cb))
    return round(sum(x * y for x, y in zip(ca, cb)) / den, 3) if den > 0 else 0.0


def run(data, weights, cost=COST, label=""):
    """weights[i] = {sym: w} chosen at close of data['dates'][i] (may use [:i+1]).
    Earns sym's adj-close return from i to i+1. Turnover charged at `cost`."""
    dates = data["dates"]
    syms  = [s for s in data if s != "dates"]
    assert len(weights) == len(dates), f"need {len(dates)} weight rows, got {len(weights)}"
    # trim indicator warm-up: score only from the first day a position exists,
    # so cash-warmup days don't dilute the metrics (benchmark gets same window)
    first = next((i for i, w in enumerate(weights) if w), 0)
    dates, weights = dates[first:], weights[first:]
    data = {**{s: {k: data[s][k][first:] for k in data[s]} for s in syms},
            "dates": dates}
    rets, prev_w = [], {}
    for i in range(1, len(dates)):
        w = weights[i - 1] or {}
        gross = sum(w.get(s, 0.0) *
                    (data[s]["a"][i] / data[s]["a"][i - 1] - 1) for s in w)
        turn = sum(abs(w.get(s, 0.0) - prev_w.get(s, 0.0))
                   for s in set(w) | set(prev_w))
        rets.append(gross - turn * cost)
        prev_w = w
    m = _metrics(dates, rets)
    # same-period SPY benchmark
    spy = load("SPY")
    idx = {d: i for i, d in enumerate(spy["dates"])}
    spy_rets = []
    for i in range(1, len(dates)):
        if dates[i] in idx and dates[i - 1] in idx:
            spy_rets.append(spy["a"][idx[dates[i]]] / spy["a"][idx[dates[i - 1]]] - 1)
    bm = _metrics(dates, spy_rets)
    exposure = sum(sum(abs(v) for v in (w or {}).values()) for w in weights) / len(weights)
    return {"label": label, "strategy": m,
            "spy_benchmark": {k: bm[k] for k in ("sharpe", "cagr", "maxdd") if k in bm},
            "corr_spy": _corr(rets, spy_rets), "avg_exposure": round(exposure, 3)}


def null_basket(syms, start=None, end=None, cost=COST):
    """Equal-weight the same universe, rebalance monthly, never time it."""
    data = align(syms, start, end)
    dates = data["dates"]
    w, cur = [], None
    for i, d in enumerate(dates):
        if cur is None or (i > 0 and d[:7] != dates[i - 1][:7]):
            cur = {s: 1.0 / len(syms) for s in syms}
        w.append(cur)
    return run(data, w, cost, label=f"null: EW {'+'.join(syms)}")


# convenience indicators (all trailing — safe at index i using [:i+1])
def sma(xs, i, n):
    return sum(xs[i - n + 1:i + 1]) / n if i + 1 >= n else None

def rsi(xs, i, n=2):
    if i < n:
        return None
    g = l = 0.0
    for j in range(i - n + 1, i + 1):
        ch = xs[j] - xs[j - 1]
        g += max(ch, 0); l += max(-ch, 0)
    if l == 0:
        return 100.0
    return 100 - 100 / (1 + g / l)

def tot_ret(xs, i, n):
    return xs[i] / xs[i - n] - 1 if i >= n and xs[i - n] > 0 else None

def realized_vol(xs, i, n=20):
    if i < n:
        return None
    rs = [xs[j] / xs[j - 1] - 1 for j in range(i - n + 1, i + 1)]
    m = sum(rs) / n
    return math.sqrt(sum((r - m) ** 2 for r in rs) / (n - 1)) * math.sqrt(TDY)
