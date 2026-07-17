import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from bt_lib import align, run, sma

data = align(["SPY", "HYG", "IEF"])
weights = []
for i in range(len(data["dates"])):
    s = sma(data["HYG"]["a"], i, 100)
    if s is None:
        weights.append({})
        continue
    weights.append({"SPY": 1.0} if data["HYG"]["a"][i] > s else {"IEF": 1.0})
res = run(data, weights, label="hyg_gate")
print(json.dumps(res))
