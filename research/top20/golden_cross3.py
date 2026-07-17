import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from bt_lib import align, run, null_basket, sma

syms = ["SPY", "TLT", "GLD"]
data = align(syms + ["BIL"])
weights = []
for i in range(len(data["dates"])):
    w = {}
    ok = True
    for s in syms:
        s50 = sma(data[s]["a"], i, 50)
        s200 = sma(data[s]["a"], i, 200)
        if s50 is None or s200 is None:
            ok = False
            break
        tgt = s if s50 > s200 else "BIL"
        w[tgt] = w.get(tgt, 0.0) + 1.0 / 3.0
    weights.append(w if ok else {})
res = run(data, weights, label="golden_cross3")
nb = null_basket(syms)
out = {"result": res, "null_sharpe": nb["strategy"]["sharpe"] if "strategy" in nb else nb.get("sharpe")}
print(json.dumps(out))
