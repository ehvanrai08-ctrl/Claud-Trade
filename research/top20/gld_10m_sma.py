import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from bt_lib import align, run, sma

data = align(["GLD", "BIL"])
weights = []
for i in range(len(data["dates"])):
    s = sma(data["GLD"]["a"], i, 210)
    if s is None:
        weights.append({})
        continue
    weights.append({"GLD": 1.0} if data["GLD"]["a"][i] > s else {"BIL": 1.0})
res = run(data, weights, label="gld_10m_sma")
print(json.dumps(res))
