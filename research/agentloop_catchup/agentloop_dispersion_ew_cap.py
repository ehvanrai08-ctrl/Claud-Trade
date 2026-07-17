import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from bt_lib import align, run
# Dispersion-Based Equal-Weight vs Cap Rotation: hold RSP (equal-weight) when
# the RSP/SPY spread's own 20d z-score is BELOW -1 (equal-weight has recently
# underperformed and is "cheap" vs cap-weight -- mean-reversion of the
# breadth spread), SPY otherwise. Distinct from the rsp_spy_breadth trend
# candidate in the 2026-07-16 sweep (that one used the RATIO's own SMA
# trend; this uses the spread's z-score reversion).
data = align(["RSP", "SPY"])
r, s = data["RSP"]["a"], data["SPY"]["a"]
n = len(data["dates"])
spread = [r[i]/r[0] - s[i]/s[0] for i in range(n)]  # cumulative relative spread, base-100 style
weights = []
for i in range(n):
    if i < 20:
        weights.append({}); continue
    window = spread[i-19:i+1]
    m = sum(window)/20
    sd = (sum((x-m)**2 for x in window)/19)**0.5
    z = (spread[i]-m)/sd if sd > 0 else 0
    weights.append({"RSP": 1.0} if z < -1 else {"SPY": 1.0})
res = run(data, weights, label="agentloop_dispersion_ew_cap")
print(json.dumps(res))
