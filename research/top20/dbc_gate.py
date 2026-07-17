import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from bt_lib import align, run, tot_ret

data = align(["DBC", "BIL"])
dates = data["dates"]
weights = []
cur = None
for i in range(len(dates)):
    new_month = i == 0 or dates[i][:7] != dates[i - 1][:7]
    if new_month:
        r = tot_ret(data["DBC"]["a"], i, 252)
        if r is None:
            cur = None
        else:
            cur = {"DBC": 1.0} if r > 0 else {"BIL": 1.0}
    weights.append(cur if cur is not None else {})
res = run(data, weights, label="dbc_gate: commodity trend gate")
print(json.dumps(res))
