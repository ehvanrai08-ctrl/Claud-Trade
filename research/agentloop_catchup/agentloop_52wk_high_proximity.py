import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from bt_lib import align, run, null_basket
# 52-Week High Proximity Momentum (George & Hwang): monthly, hold the sector
# SPDR trading CLOSEST to its own 252d high (highest close/252d-high ratio).
SECTORS = ["XLK","XLF","XLE","XLV","XLI","XLP","XLU","XLY","XLB"]
data = align(SECTORS)
dates = data["dates"]
weights, cur = [], None
for i in range(len(dates)):
    if i > 0 and dates[i][:7] == dates[i-1][:7]:
        weights.append(cur); continue
    if i < 252:
        cur = {}; weights.append(cur); continue
    prox = {s: data[s]["a"][i] / max(data[s]["a"][i-251:i+1]) for s in SECTORS}
    winner = max(prox, key=prox.get)
    cur = {winner: 1.0}
    weights.append(cur)
res = run(data, weights, label="agentloop_52wk_high_proximity")
res["null"] = null_basket(SECTORS)
print(json.dumps(res))
