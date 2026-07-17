import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from bt_lib import align, run, null_basket, tot_ret
# Oil-Energy Lead-Lag: hold XLE when USO's own trailing 10d return is
# positive (oil momentum "leads" the energy sector), else BIL.
data = align(["XLE", "USO", "BIL"])
weights = []
for i in range(len(data["dates"])):
    r = tot_ret(data["USO"]["a"], i, 10)
    weights.append({} if r is None else ({"XLE": 1.0} if r > 0 else {"BIL": 1.0}))
res = run(data, weights, label="agentloop_oil_energy_leadlag")
res["null"] = null_basket(["XLE"])
print(json.dumps(res))
