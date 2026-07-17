import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from bt_lib import align, run, sma

data = align(["QQQ"])
a = data["QQQ"]["a"]
weights = []
holding = False
days_held = 0
for i in range(len(data["dates"])):
    s = sma(a, i, 200)
    if s is None:
        weights.append({})
        continue
    if holding:
        days_held += 1
        # exit at first close higher than prior close, or after 5 days
        if a[i] > a[i-1] or days_held >= 5:
            holding = False
            days_held = 0
    if not holding:
        if a[i] > s and i >= 3 and a[i] < a[i-1] < a[i-2] < a[i-3]:
            holding = True
            days_held = 0
    weights.append({"QQQ": 1.0} if holding else {})
res = run(data, weights, label="streak3_qqq")
print(json.dumps(res))
