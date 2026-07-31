"""Intraday VWAP reversion.

Hypothesis: price stretched far from session VWAP reverts toward it.

Higher frequency than ORB -- which is what was asked for -- but frequency cuts
both ways: costs scale linearly with trade count while edge does not. The cost
sensitivity sweep matters MORE for this strategy than for ORB.
"""
from datetime import time
import numpy as np
from .base import Strategy
from ..engine import Signal


class VWAPReversion(Strategy):
    name = "vwap_reversion"

    def __init__(self, stretch_atr=1.5, stop_pad_atr=0.25, max_entries=4,
                 warmup=20, start=time(10, 0), cutoff=time(15, 30)):
        self.stretch_atr = stretch_atr
        self.stop_pad_atr = stop_pad_atr
        self.max_entries = max_entries
        self.warmup = warmup
        self.start = start
        self.cutoff = cutoff

    def new_day(self, symbol, day_bars):
        self.entries_today = 0
        self.times = day_bars.index

    def on_bar(self, symbol, bars, i):
        if self.entries_today >= self.max_entries or i < self.warmup:
            return None
        t = self.times[i].time()
        if not (self.start <= t < self.cutoff):
            return None

        atr = bars["atr"].iat[i]
        vwap = bars["vwap"].iat[i]
        if not (np.isfinite(atr) and np.isfinite(vwap)) or atr <= 0:
            return None

        c, o = bars["close"].iat[i], bars["open"].iat[i]
        stretch = (c - vwap) / atr

        if stretch <= -self.stretch_atr and c > o and vwap > c:
            self.entries_today += 1
            return Signal("long", bars["low"].iat[i] - self.stop_pad_atr * atr, vwap, "vwap_long")
        if stretch >= self.stretch_atr and c < o and vwap < c:
            self.entries_today += 1
            return Signal("short", bars["high"].iat[i] + self.stop_pad_atr * atr, vwap, "vwap_short")
        return None
