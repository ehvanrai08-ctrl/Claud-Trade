import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from bt_lib import align, run, null_basket, realized_vol

SYMS = ["SPY", "TLT", "GLD", "DBC"]
data = align(SYMS)
dates = data["dates"]

weights = []
cur = {}
for i in range(len(dates)):
    new_month = i == 0 or dates[i][:7] != dates[i-1][:7]
    if new_month:
        vols = {}
        ok = True
        for s in SYMS:
            v = realized_vol(data[s]["a"], i, 60)
            if v is None or v <= 0:
                ok = False
                break
            vols[s] = v
        if ok:
            inv = {s: 1.0 / vols[s] for s in SYMS}
            tot = sum(inv.values())
            cur = {s: inv[s] / tot for s in SYMS}
        else:
            cur = {}
    weights.append(dict(cur))

res = run(data, weights, label="riskpar4_iv")
res["null"] = null_basket(SYMS)
print(json.dumps(res))
