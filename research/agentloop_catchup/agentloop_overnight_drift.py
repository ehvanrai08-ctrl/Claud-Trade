import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from bt_lib import align, _metrics, _corr, load
# Overnight vs Intraday Drift Capture: SPY "always hold overnight, flat
# intraday" — earn today's open->yesterday's close as OFF, i.e. return from
# yesterday's close to TODAY's open (raw close/open, not adjusted, since
# splits/divs on raw o/c don't materially distort short overnight windows and
# adjclose has no separate "open" series). Position is synthetic (not a
# tradeable weight-vector, so this bypasses run()/turnover cost — flagged).
data = align(["SPY"])
o, c = data["SPY"]["o"], data["SPY"]["c"]
dates = data["dates"]
rets = [o[i]/c[i-1] - 1 - 0.0005 for i in range(1, len(dates))]  # 5bps cost per overnight round-trip
m = _metrics(dates, rets)
spy = load("SPY")
idx = {d:i for i,d in enumerate(spy["dates"])}
spy_rets = [spy["a"][idx[dates[i]]]/spy["a"][idx[dates[i-1]]]-1 for i in range(1,len(dates)) if dates[i] in idx and dates[i-1] in idx]
bm = _metrics(dates, spy_rets)
print(json.dumps({"label":"agentloop_overnight_drift","strategy":m,
                   "spy_benchmark":{k:bm[k] for k in ("sharpe","cagr","maxdd") if k in bm},
                   "corr_spy": _corr(rets, spy_rets), "avg_exposure": 1.0,
                   "caveat": "overnight-only position, not weight-vector tradeable via run() -- informational only"}))
