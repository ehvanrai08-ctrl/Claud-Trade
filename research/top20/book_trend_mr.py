import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from bt_lib import align, run, sma, tot_ret

ASSETS = ["SPY", "TLT", "GLD", "DBC", "UUP"]
data = align(ASSETS + ["BIL"])
dates = data["dates"]
spy = data["SPY"]

weights = []
cur = None       # tsmom sub-book weights (full-book scale), or None during warm-up
holding = False  # IBS sub-book state
for i in range(len(dates)):
    # (a) TSMOM 12m sleeve, monthly rebalance
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

    # (b) IBS SPY sub-book
    s = sma(spy["a"], i, 200)
    if s is None or cur is None:
        weights.append({})
        continue
    h, l, c = spy["h"][i], spy["l"][i], spy["c"][i]
    ibs = (c - l) / (h - l) if h != l else 0.5
    if holding:
        if ibs > 0.80:
            holding = False
    else:
        if ibs < 0.20 and spy["a"][i] > s:
            holding = True

    # combine: 50% each sub-book; IBS idle capital parks in BIL
    comb = {}
    for sym, wt in cur.items():
        comb[sym] = comb.get(sym, 0.0) + 0.5 * wt
    ibs_tgt = "SPY" if holding else "BIL"
    comb[ibs_tgt] = comb.get(ibs_tgt, 0.0) + 0.5
    weights.append(comb)

res = run(data, weights, label="book_trend_mr")
out = dict(res)
out["null_sharpe"] = None
print(json.dumps(out))
