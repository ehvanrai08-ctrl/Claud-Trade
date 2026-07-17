import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from bt_lib import align, run, null_basket, realized_vol
# Low-Volatility Sector Tilt: monthly, hold the single lowest-20d-realized-vol
# sector SPDR out of the 9 classic sectors.
SECTORS = ["XLK","XLF","XLE","XLV","XLI","XLP","XLU","XLY","XLB"]
data = align(SECTORS)
dates = data["dates"]
weights, cur = [], None
for i in range(len(dates)):
    if i > 0 and dates[i][:7] == dates[i-1][:7]:
        weights.append(cur); continue
    vols = {s: realized_vol(data[s]["a"], i, 20) for s in SECTORS}
    if any(v is None for v in vols.values()):
        cur = {}; weights.append(cur); continue
    winner = min(vols, key=vols.get)
    cur = {winner: 1.0}
    weights.append(cur)
res = run(data, weights, label="agentloop_lowvol_sector")
res["null"] = null_basket(SECTORS)
print(json.dumps(res))
