import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from bt_lib import align, run
# Cross-Asset Correlation Regime Filter: when SPY/TLT 60d rolling correlation
# turns positive (bonds stop hedging equities — a stress regime), de-risk to
# 50/50 SPY/BIL; otherwise stay full SPY (normal diversification regime).
data = align(["SPY", "TLT", "BIL"])
s, t = data["SPY"]["a"], data["TLT"]["a"]
n = len(data["dates"])
weights = []
for i in range(n):
    if i < 60:
        weights.append({}); continue
    rs = [s[j]/s[j-1]-1 for j in range(i-59, i+1)]
    rt = [t[j]/t[j-1]-1 for j in range(i-59, i+1)]
    ms, mt = sum(rs)/60, sum(rt)/60
    cov = sum((a-ms)*(b-mt) for a,b in zip(rs,rt))/59
    sdS = (sum((a-ms)**2 for a in rs)/59)**0.5
    sdT = (sum((b-mt)**2 for b in rt)/59)**0.5
    corr = cov/(sdS*sdT) if sdS>0 and sdT>0 else 0
    weights.append({"SPY": 0.5, "BIL": 0.5} if corr > 0 else {"SPY": 1.0})
res = run(data, weights, label="agentloop_crossasset_corr")
print(json.dumps(res))
