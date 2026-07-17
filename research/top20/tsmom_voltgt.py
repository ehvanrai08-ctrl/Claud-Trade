import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from bt_lib import align, run, null_basket, tot_ret, realized_vol

UNIV = ["SPY", "TLT", "GLD", "DBC", "UUP"]
data = align(UNIV + ["BIL"])
dates = data["dates"]

weights = []
cur = None
for i in range(len(dates)):
    new_month = i == 0 or dates[i][:7] != dates[i - 1][:7]
    if new_month:
        sig = {}
        ok = True
        for s in UNIV:
            r = tot_ret(data[s]["a"], i, 252)
            v = realized_vol(data[s]["a"], i, 60)
            if r is None or v is None or v <= 0:
                ok = False
                break
            sig[s] = (r > 0, v)
        if not ok:
            cur = None
        else:
            on = [s for s in UNIV if sig[s][0]]
            w = {}
            if on:
                inv = {s: 1.0 / sig[s][1] for s in on}
                tot = sum(inv.values())
                budget = len(on) / 5.0
                for s in on:
                    w[s] = budget * inv[s] / tot
            rem = 1.0 - sum(w.values())
            if rem > 1e-9:
                w["BIL"] = rem
            cur = w
    weights.append({} if cur is None else dict(cur))

res = run(data, weights, label="tsmom_voltgt")
nb = null_basket(UNIV)
out = {"result": res, "null_sharpe": nb["strategy"]["sharpe"]}
print(json.dumps(out))
