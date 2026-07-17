import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from bt_lib import align, run, sma

data = align(["SPY", "BIL"])
a = data["SPY"]["a"]
weights = []
in_spy = True
for i in range(len(data["dates"])):
    s200 = sma(a, i, 200)
    s50 = sma(a, i, 50)
    if s200 is None:
        weights.append({})
        continue
    if in_spy:
        if a[i] < s200:
            in_spy = False
    else:
        if a[i] > s200 and s50 is not None and a[i] > s50:
            in_spy = True
    weights.append({"SPY": 1.0} if in_spy else {"BIL": 1.0})
res = run(data, weights, label="sma_hysteresis")
print(json.dumps(res))
