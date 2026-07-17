import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from bt_lib import align, run, sma

data = align(["QQQ"])
a = data["QQQ"]["a"]
weights = []
holding = False
for i in range(len(data["dates"])):
    s = sma(a, i, 200)
    if s is None:
        weights.append({}); continue
    if not holding:
        if a[i] > s and i >= 6 and a[i] == min(a[i-6:i+1]):
            holding = True
    else:
        if i >= 6 and a[i] == max(a[i-6:i+1]):
            holding = False
    weights.append({"QQQ": 1.0} if holding else {})
res = run(data, weights, label="dbl7_qqq")
print(json.dumps(res))
