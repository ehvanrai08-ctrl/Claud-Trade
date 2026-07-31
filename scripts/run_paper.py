#!/usr/bin/env python3
"""Run a strategy live against the Alpaca PAPER account."""
import argparse, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.executor import PaperExecutor
from src.risk import RiskConfig
from src.strategies import REGISTRY

p = argparse.ArgumentParser()
p.add_argument("--strategy", default="orb", choices=list(REGISTRY))
p.add_argument("--symbols", default="SPY,QQQ,AAPL,MSFT,NVDA")
p.add_argument("--feed", default="iex")
p.add_argument("--poll", type=int, default=60)
p.add_argument("--risk-per-trade", type=float, default=0.005)
p.add_argument("--max-daily-loss", type=float, default=0.02)
a = p.parse_args()

ex = PaperExecutor(
    REGISTRY[a.strategy](),
    [s.strip().upper() for s in a.symbols.split(",")],
    risk_cfg=RiskConfig(risk_per_trade=a.risk_per_trade,
                        max_daily_loss=a.max_daily_loss),
    feed=a.feed, poll_seconds=a.poll)
ex.run_session()
