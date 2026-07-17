import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from bt_lib import align, run
# Turn-of-Month effect: hold SPY only on the last trading day of the month
# through the first 3 trading days of the next month; BIL otherwise.
data = align(["SPY", "BIL"])
dates = data["dates"]
# find month-end indices (day i where dates[i][:7] != dates[i+1][:7])
is_month_end = [i for i in range(len(dates)-1) if dates[i][:7] != dates[i+1][:7]]
in_window = set()
for me in is_month_end:
    for k in range(0, 4):  # month-end day itself + first 3 days of next month
        if me + k < len(dates):
            in_window.add(me + k)
weights = [({"SPY": 1.0} if i in in_window else {"BIL": 1.0}) for i in range(len(dates))]
res = run(data, weights, label="agentloop_turn_of_month")
print(json.dumps(res))
