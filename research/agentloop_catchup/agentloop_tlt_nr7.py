import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from bt_lib import align, run
# TLT volatility-contraction breakout (Crabel NR7): when today's raw high-low
# range is the narrowest of the last 7 days, buy at close; exit after 3 days
# or on a close back below the breakout day's low, whichever first.
data = align(["TLT", "BIL"])
h, l, c = data["TLT"]["h"], data["TLT"]["l"], data["TLT"]["c"]
n = len(data["dates"])
weights, hold_days, floor = [], 0, None
for i in range(n):
    if hold_days > 0:
        hold_days -= 1
        if c[i] < floor:
            weights.append({"BIL": 1.0}); hold_days = 0; continue
        weights.append({"TLT": 1.0}); continue
    if i < 7:
        weights.append({}); continue
    ranges = [h[j]-l[j] for j in range(i-6, i+1)]
    is_nr7 = ranges[-1] == min(ranges)
    if is_nr7:
        weights.append({"TLT": 1.0}); hold_days = 2; floor = l[i]
    else:
        weights.append({"BIL": 1.0})
res = run(data, weights, label="agentloop_tlt_nr7")
print(json.dumps(res))
