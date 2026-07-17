import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from bt_lib import align, run, null_basket, sma

data = align(["SPY", "BIL", "^VIX"])
spy = data["SPY"]["a"]
vix = data["^VIX"]["a"]
n = len(data["dates"])

weights = []
timer = 0
for i in range(n):
    s = sma(spy, i, 200)
    if s is None:
        weights.append({})
        continue
    above = spy[i] > s
    if not above:
        vmax = max(vix[max(0, i - 20):i + 1])
        if vix[i] >= 30 and vix[i] < 0.80 * vmax:
            timer = 21  # (re)trigger resets the timer
    if timer > 0 and not above:
        weights.append({"SPY": 0.5, "BIL": 0.5})
    else:
        weights.append({"SPY": 1.0} if above else {"BIL": 1.0})
    if timer > 0:
        timer -= 1

res = run(data, weights, label="vix_crush_rebound")
res["null"] = null_basket(["SPY"])
print(json.dumps(res))
