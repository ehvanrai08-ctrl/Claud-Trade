import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from bt_lib import align, run, null_basket, tot_ret

ASSETS = ["SPY", "TLT", "GLD", "DBC", "UUP"]
data = align(ASSETS + ["BIL"])
dates = data["dates"]

weights = []
cur = None
for i in range(len(dates)):
    new_month = i == 0 or dates[i][:7] != dates[i - 1][:7]
    if new_month:
        w = {}
        ok = True
        for sym in ASSETS:
            r = tot_ret(data[sym]["a"], i, 252)
            if r is None:
                ok = False
                break
            tgt = sym if r > 0 else "BIL"
            w[tgt] = w.get(tgt, 0.0) + 0.2
        cur = w if ok else None
    weights.append(cur if cur is not None else {})

res = run(data, weights, label="tsmom12_5a")
nb = null_basket(ASSETS)
out = dict(res)
out["null_sharpe"] = nb["strategy"]["sharpe"]
print(json.dumps(out))
