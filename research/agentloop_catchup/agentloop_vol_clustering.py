import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from bt_lib import align, run, null_basket, realized_vol
# Volatility Clustering Regime Switch: vol-clustering (GARCH-lite) proxy --
# de-risk SPY to half-position when 5d realized vol > 20d realized vol
# (vol is actively expanding, "vol begets vol"), full SPY otherwise.
data = align(["SPY", "BIL"])
s = data["SPY"]["a"]
weights = []
for i in range(len(data["dates"])):
    v5, v20 = realized_vol(s, i, 5), realized_vol(s, i, 20)
    if v5 is None or v20 is None:
        weights.append({}); continue
    expanding = v5 > v20
    weights.append({"SPY": 0.5, "BIL": 0.5} if expanding else {"SPY": 1.0})
res = run(data, weights, label="agentloop_vol_clustering")
res["null"] = null_basket(["SPY"])
print(json.dumps(res))
