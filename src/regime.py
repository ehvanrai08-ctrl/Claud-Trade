"""Daily volatility-regime classification.

Why this exists: an intraday strategy's edge is almost never uniform across
market conditions. ORB wants range expansion; VWAP reversion wants chop. Trading
either one blind across all regimes averages a good edge and a bad edge together
and reports the mediocre middle -- which is how a real edge gets thrown away for
looking unremarkable.

Fed by DAILY bars, which is exactly what the free Alpha Vantage tier provides.
The regime label for day D uses data through D-1 ONLY. It is a filter you can
compute before the open, which is the only kind that is tradeable.
"""
import numpy as np
import pandas as pd


def load_daily(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["timestamp"]).sort_values("timestamp")
    return df.set_index("timestamp")


def classify(daily: pd.DataFrame, atr_n: int = 14, lookback: int = 60) -> pd.DataFrame:
    """Label each session's expected volatility regime using only prior data."""
    df = daily.copy()
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - pc).abs(),
                    (df["low"] - pc).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(atr_n, min_periods=atr_n).mean()
    df["atr_pct"] = df["atr"] / df["close"] * 100

    # SHIFT(1) is the whole point: today's label may not peek at today's range.
    prior = df["atr_pct"].shift(1)
    rank = prior.rolling(lookback, min_periods=20).rank(pct=True)
    df["vol_rank"] = rank
    df["regime"] = pd.cut(rank, [0, 0.33, 0.66, 1.0],
                          labels=["low_vol", "normal", "high_vol"])

    # Gap and prior-day direction: cheap context, computable pre-open.
    df["prior_range_pct"] = ((df["high"] - df["low"]) / df["close"] * 100).shift(1)
    df["gap_pct"] = (df["open"] / df["close"].shift() - 1) * 100
    return df


def summary(df: pd.DataFrame) -> pd.DataFrame:
    d = df.dropna(subset=["regime"]).copy()
    d["next_range_pct"] = (d["high"] - d["low"]) / d["close"] * 100
    return d.groupby("regime", observed=True).agg(
        sessions=("close", "size"),
        median_range_pct=("next_range_pct", "median"),
        mean_range_pct=("next_range_pct", "mean"),
        median_atr_pct=("atr_pct", "median"))


if __name__ == "__main__":
    import sys
    df = classify(load_daily(sys.argv[1] if len(sys.argv) > 1 else "data/spy_daily_sample.csv"))
    print(summary(df).to_string())
