#!/usr/bin/env python3
"""Backtest a strategy over real Alpaca data with in-sample/out-of-sample split.

    python scripts/run_backtest.py --strategy orb --symbols SPY,QQQ,AAPL \
        --start 2024-01-01 --end 2025-01-01
"""
import argparse, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.data import fetch_bars, train_test_split
from src.engine import Backtest
from src.risk import RiskConfig
from src.costs import BASE
from src.strategies import REGISTRY
from src.validate import report, cost_sensitivity

p = argparse.ArgumentParser()
p.add_argument("--strategy", default="orb", choices=list(REGISTRY))
p.add_argument("--symbols", default="SPY,QQQ,AAPL,MSFT,NVDA")
p.add_argument("--start", default="2024-01-01")
p.add_argument("--end", default="2025-01-01")
p.add_argument("--equity", type=float, default=100_000.0)
p.add_argument("--feed", default="iex")
p.add_argument("--split", type=float, default=0.7)
a = p.parse_args()

syms = [s.strip().upper() for s in a.symbols.split(",")]
print(f"fetching {syms} {a.start}..{a.end} (feed={a.feed})")
bars = fetch_bars(syms, a.start, a.end, feed=a.feed)
if not bars:
    sys.exit("no data returned")

train, test = train_test_split(bars, a.split)
Strat = REGISTRY[a.strategy]
rc = RiskConfig()

bt_in = Backtest(Strat(), BASE, rc, a.equity); tr_in = bt_in.run(train)
report(f"IN-SAMPLE  ({a.strategy})", tr_in, a.equity)

bt_out = Backtest(Strat(), BASE, rc, a.equity); tr_out = bt_out.run(test)
report(f"OUT-OF-SAMPLE  ({a.strategy})  <-- this is the one that counts", tr_out, a.equity)

print("\n" + "=" * 68 + "\nCOST SENSITIVITY (full period)\n" + "=" * 68)
cs = cost_sensitivity(Strat, bars, rc, a.equity)
print(cs[["trades", "expectancy_R", "net_pnl", "pnl_per_day", "round_turn_bps"]].to_string())

os.makedirs("results", exist_ok=True)
if not tr_out.empty:
    tr_out.to_csv(f"results/{a.strategy}_oos_trades.csv", index=False)
    print(f"\nsaved results/{a.strategy}_oos_trades.csv")
print("\nIf out-of-sample expectancy CI straddles zero, you do NOT have an edge yet.")
