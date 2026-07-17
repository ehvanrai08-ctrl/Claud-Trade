import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from bt_lib import align, run, null_basket, sma

data = align(["QQQ", "BIL", "HYG", "^VIX"])
weights = []
for i in range(len(data["dates"])):
    s = sma(data["HYG"]["a"], i, 200)
    if s is None:
        weights.append({})
        continue
    credit_ok = data["HYG"]["a"][i] > s
    vol_ok = data["^VIX"]["c"][i] < 30
    weights.append({"QQQ": 1.0} if (credit_ok and vol_ok) else {"BIL": 1.0})

res = run(data, weights, label="credit_vol_qqq")
res["null"] = null_basket(["QQQ"])
print(json.dumps(res))
