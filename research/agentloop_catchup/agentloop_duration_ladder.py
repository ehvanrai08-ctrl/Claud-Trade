import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from bt_lib import align, run, null_basket, tot_ret
# BND/TLT Duration Momentum Ladder: monthly, hold whichever of SHY (short)
# IEF (medium) TLT (long) BND (aggregate) has the strongest 126d total
# return -- relative momentum across the duration curve.
LADDER = ["SHY", "IEF", "TLT", "BND"]
data = align(LADDER)
dates = data["dates"]
weights, cur = [], None
for i in range(len(dates)):
    if i > 0 and dates[i][:7] == dates[i-1][:7]:
        weights.append(cur); continue
    rets = {s: tot_ret(data[s]["a"], i, 126) for s in LADDER}
    if any(v is None for v in rets.values()):
        cur = {}; weights.append(cur); continue
    winner = max(rets, key=rets.get)
    cur = {winner: 1.0}
    weights.append(cur)
res = run(data, weights, label="agentloop_duration_ladder")
res["null"] = null_basket(LADDER)
print(json.dumps(res))
