import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from bt_lib import align, run, null_basket, tot_ret

ASSETS = ["SPY", "TLT", "GLD", "DBC"]
data = align(ASSETS + ["BIL"])
dates = data["dates"]

weights = []
cur = None
for i in range(len(dates)):
    if i == 0 or dates[i][:7] != dates[i-1][:7] or cur is None:
        w = {}
        ok = True
        for sym in ASSETS:
            rets = [tot_ret(data[sym]["a"], i, n) for n in (126, 189, 252)]
            if any(r is None for r in rets):
                ok = False
                break
            tgt = sym if sum(rets)/3 > 0 else "BIL"
            w[tgt] = w.get(tgt, 0) + 0.25
        cur = w if ok else None
    weights.append(cur if cur is not None else {})

res = run(data, weights, label="tsmom_ens_4a")
nb = null_basket(ASSETS)
out = dict(res)
out["null_sharpe"] = nb["strategy"]["sharpe"]
print(json.dumps(out))
