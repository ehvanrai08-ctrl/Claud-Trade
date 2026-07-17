import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from bt_lib import align, run, sma, rsi

data = align(["QQQ"])
q = data["QQQ"]
weights = []
holding = False
for i in range(len(data["dates"])):
    s = sma(q["a"], i, 200)
    r = rsi(q["a"], i, 2)
    if s is None or r is None:
        weights.append({})
        continue
    hl = q["h"][i] - q["l"][i]
    ibs = (q["c"][i] - q["l"][i]) / hl if hl > 0 else 0.5
    if holding:
        if ibs > 0.75:
            holding = False
    else:
        if q["a"][i] > s and ibs < 0.20 and r < 25:
            holding = True
    weights.append({"QQQ": 1.0} if holding else {})
res = run(data, weights, label="IBS+RSI2 combo QQQ")
print(json.dumps(res))
