import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from bt_lib import align, run, sma

data = align(["QQQ", "BIL"])
weights = []
for i in range(len(data["dates"])):
    s = sma(data["QQQ"]["a"], i, 210)
    if s is None:
        weights.append({})
        continue
    weights.append({"QQQ": 1.0} if data["QQQ"]["a"][i] > s else {"BIL": 1.0})
res = run(data, weights, label="QQQ 10m SMA switch")
print(json.dumps(res))
