import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from bt_lib import align, run, null_basket, sma

ASSETS = ["SPY", "TLT", "GLD"]
data = align(ASSETS + ["BIL"])
weights = []
for i in range(len(data["dates"])):
    smas = {a: sma(data[a]["a"], i, 210) for a in ASSETS}
    if any(v is None for v in smas.values()):
        weights.append({})
        continue
    w = {}
    for a in ASSETS:
        tgt = a if data[a]["a"][i] > smas[a] else "BIL"
        w[tgt] = w.get(tgt, 0.0) + 1.0 / 3.0
    weights.append(w)

res = run(data, weights, label="ew3_gated")
res["null_sharpe"] = null_basket(ASSETS)["strategy"]["sharpe"]
print(json.dumps(res))
