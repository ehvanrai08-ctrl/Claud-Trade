import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from bt_lib import align, run, null_basket, tot_ret
# Sector Dispersion Mean Reversion: monthly, buy the single WORST-performing
# sector of the last month (cross-sectional short-term reversal), gated by
# its own 200d SMA to avoid catching a falling knife.
SECTORS = ["XLK","XLF","XLE","XLV","XLI","XLP","XLU","XLY","XLB"]
data = align(SECTORS + ["BIL"])
dates = data["dates"]
weights, cur = [], None
for i in range(len(dates)):
    if i > 0 and dates[i][:7] == dates[i-1][:7]:
        weights.append(cur); continue
    rets = {s: tot_ret(data[s]["a"], i, 21) for s in SECTORS}
    if any(v is None for v in rets.values()):
        cur = {}; weights.append(cur); continue
    worst = min(rets, key=rets.get)
    sma200 = sum(data[worst]["a"][max(0,i-199):i+1]) / min(200, i+1)
    cur = {worst: 1.0} if data[worst]["a"][i] > sma200 else {"BIL": 1.0}
    weights.append(cur)
res = run(data, weights, label="agentloop_sector_dispersion_revert")
res["null"] = null_basket(SECTORS)
print(json.dumps(res))
