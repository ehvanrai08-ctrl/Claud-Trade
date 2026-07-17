import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from bt_lib import align, run, null_basket, sma
# Gold Momentum with Trend Confirmation: hold GLD only when BOTH the 50d SMA
# is above the 200d SMA (medium-term trend up) AND price is above the 50d
# SMA (short-term confirmation), else BIL.
data = align(["GLD", "BIL"])
g = data["GLD"]["a"]
weights = []
for i in range(len(data["dates"])):
    s50, s200 = sma(g, i, 50), sma(g, i, 200)
    if s50 is None or s200 is None:
        weights.append({}); continue
    on = (s50 > s200) and (g[i] > s50)
    weights.append({"GLD": 1.0} if on else {"BIL": 1.0})
res = run(data, weights, label="agentloop_gold_trend_confirm")
res["null"] = null_basket(["GLD"])
print(json.dumps(res))
