import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from bt_lib import align, run, null_basket
# Gold-Bond Defensive Pair Switch: hold whichever of GLD/TLT had the stronger
# 63d total return, rebalanced daily (flight-to-safety asset rotation).
data = align(["GLD", "TLT", "BIL"])
g, t = data["GLD"]["a"], data["TLT"]["a"]
weights = []
for i in range(len(data["dates"])):
    if i < 63:
        weights.append({}); continue
    rg = g[i]/g[i-63]-1; rt = t[i]/t[i-63]-1
    weights.append({"GLD": 1.0} if rg > rt else {"TLT": 1.0})
res = run(data, weights, label="agentloop_gold_bond_flip")
res["null"] = null_basket(["GLD","TLT"])
print(json.dumps(res))
