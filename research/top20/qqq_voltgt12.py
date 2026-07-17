import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from bt_lib import align, run, realized_vol

data = align(["QQQ", "BIL"])
weights = []
for i in range(len(data["dates"])):
    v = realized_vol(data["QQQ"]["a"], i, 20)
    if v is None or v <= 0:
        weights.append({})
        continue
    w = min(1.0, 0.12 / v)
    weights.append({"QQQ": w, "BIL": 1.0 - w})
res = run(data, weights, label="QQQ 12% vol targeting")
print(json.dumps(res))
