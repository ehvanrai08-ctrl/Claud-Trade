import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from bt_lib import align, run, realized_vol

data = align(["SPY", "BIL"])
weights = []
for i in range(len(data["dates"])):
    v = realized_vol(data["SPY"]["a"], i, 20)
    if v is None or v <= 0:
        weights.append({})
        continue
    w = min(1.0, 0.10 / v)
    weights.append({"SPY": w, "BIL": 1.0 - w})
res = run(data, weights, label="SPY 10% vol targeting")
print(json.dumps(res))
