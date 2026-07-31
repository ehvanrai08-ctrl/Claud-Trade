"""Opening Range Breakout -- mechanically defined, no chart-reading judgement.

Hypothesis: the first N minutes establish an initial balance; a decisive break
of it on expanding volume initiates a directional day often enough to pay for
the days it does not.

This is a HYPOTHESIS, not a claim. Run it before believing it.
"""
from datetime import time
import numpy as np
import pandas as pd
from .base import Strategy
from ..engine import Signal


class OpeningRangeBreakout(Strategy):
    name = "orb"

    def __init__(self, or_minutes=15, target_r=2.0, max_entries=1,
                 min_or_atr=0.5, max_or_atr=3.0, rvol_min=1.0,
                 cutoff=time(11, 30)):
        self.or_minutes = or_minutes
        self.target_r = target_r
        self.max_entries = max_entries
        self.min_or_atr = min_or_atr   # skip a meaninglessly tight opening range
        self.max_or_atr = max_or_atr   # skip a day already blown out on the open
        self.rvol_min = rvol_min
        self.cutoff = cutoff

    def new_day(self, symbol, day_bars):
        self.entries_today = 0
        self.or_end_i = min(self.or_minutes, len(day_bars)) - 1
        w = day_bars.iloc[: self.or_end_i + 1]
        self.or_high = float(w["high"].max())
        self.or_low = float(w["low"].min())
        self.times = day_bars.index

    def on_bar(self, symbol, bars, i):
        if self.entries_today >= self.max_entries or i <= self.or_end_i:
            return None
        if self.times[i].time() >= self.cutoff:
            return None

        atr = bars["atr"].iat[i]
        if not np.isfinite(atr) or atr <= 0:
            return None
        or_range = self.or_high - self.or_low
        if or_range <= 0 or not (self.min_or_atr <= or_range / atr <= self.max_or_atr):
            return None

        rvol = bars["rvol"].iat[i]
        if np.isfinite(rvol) and rvol < self.rvol_min:
            return None

        c = bars["close"].iat[i]
        if c > self.or_high:
            self.entries_today += 1
            return Signal("long", self.or_low, c + self.target_r * (c - self.or_low), "orb_long")
        if c < self.or_low:
            self.entries_today += 1
            return Signal("short", self.or_high, c - self.target_r * (self.or_high - c), "orb_short")
        return None
