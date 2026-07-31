"""Strategy interface.

Two-phase contract:

  prepare(day_bars) -> adds CAUSAL indicator columns once per session. Causal
      means rolling/expanding only: the value at bar i uses bars 0..i and
      nothing after. Any non-causal column here is look-ahead bias and will
      silently make every downstream number a lie.

  on_bar(symbol, bars, i) -> may read bars.iloc[:i+1] and NOTHING beyond i.

A strategy does not size positions, does not check risk, and does not know its
own P&L. Those belong to the risk layer and the engine.
"""
import numpy as np
import pandas as pd
from ..engine import Signal


class Strategy:
    name = "base"
    atr_period = 14
    rvol_period = 20

    def prepare(self, day_bars: pd.DataFrame) -> pd.DataFrame:
        df = day_bars.copy()
        prev_close = df["close"].shift()
        tr = pd.concat([df["high"] - df["low"],
                        (df["high"] - prev_close).abs(),
                        (df["low"] - prev_close).abs()], axis=1).max(axis=1)
        df["atr"] = tr.rolling(self.atr_period, min_periods=2).mean()
        tp = (df["high"] + df["low"] + df["close"]) / 3
        cv = df["volume"].cumsum()
        df["vwap"] = (tp * df["volume"]).cumsum() / cv.replace(0, np.nan)
        df["rvol"] = df["volume"] / df["volume"].rolling(
            self.rvol_period, min_periods=5).mean()
        return df

    def new_day(self, symbol: str, day_bars: pd.DataFrame) -> None:
        self.entries_today = 0

    def on_bar(self, symbol: str, bars: pd.DataFrame, i: int) -> Signal | None:
        raise NotImplementedError
