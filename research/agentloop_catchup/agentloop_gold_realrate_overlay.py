import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from bt_lib import align, run, null_basket, sma
# Gold Trend Overlay with Real-Rate Proxy: real yields aren't in this ETF
# universe, so TIP/IEF ratio's trend proxies "falling real rates" (TIP
# outperforming nominal IEF when inflation-protection is bid, i.e. real
# yields falling) -- hold GLD only when BOTH gold's own 200d SMA trend is up
# AND the TIP/IEF ratio's 60d SMA is rising (real-rate tailwind), else BIL.
data = align(["GLD", "TIP", "IEF", "BIL"])
g, tip, ief = data["GLD"]["a"], data["TIP"]["a"], data["IEF"]["a"]
n = len(data["dates"])
ratio = [tip[i]/ief[i] for i in range(n)]
weights = []
for i in range(n):
    gsma = sma(g, i, 200)
    rsma_now = sma(ratio, i, 60)
    rsma_prev = sma(ratio, i-5, 60) if i >= 5 else None
    if gsma is None or rsma_now is None or rsma_prev is None:
        weights.append({}); continue
    on = (g[i] > gsma) and (rsma_now > rsma_prev)
    weights.append({"GLD": 1.0} if on else {"BIL": 1.0})
res = run(data, weights, label="agentloop_gold_realrate_overlay")
res["null"] = null_basket(["GLD"])
print(json.dumps(res))
