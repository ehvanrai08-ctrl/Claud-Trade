import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from bt_lib import align, run, null_basket, tot_ret
# EFA vs SPY Relative Momentum Switch: monthly, hold whichever of SPY/EFA had
# the higher 126d total return; absolute gate to BIL if the winner's own
# 252d return is negative.
data = align(["SPY", "EFA", "BIL"])
dates = data["dates"]
weights, cur = [], None
for i in range(len(dates)):
    if i > 0 and dates[i][:7] == dates[i-1][:7]:
        weights.append(cur); continue
    rs, re_ = tot_ret(data["SPY"]["a"], i, 126), tot_ret(data["EFA"]["a"], i, 126)
    if rs is None or re_ is None:
        cur = {}; weights.append(cur); continue
    winner = "SPY" if rs > re_ else "EFA"
    abs_gate = tot_ret(data[winner]["a"], i, 252)
    cur = {winner: 1.0} if (abs_gate is not None and abs_gate > 0) else {"BIL": 1.0}
    weights.append(cur)
res = run(data, weights, label="agentloop_efa_spy_relmom")
res["null"] = null_basket(["SPY","EFA"])
print(json.dumps(res))
