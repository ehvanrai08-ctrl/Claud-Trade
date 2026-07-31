"""Alpaca market data -> tidy OHLCV frames, cached to parquet.

IMPORTANT DATA CAVEAT: Alpaca's free tier serves the IEX feed, which is a
single exchange carrying only a low-single-digit percentage of consolidated
US equity volume. Consequences:
  * `volume` is NOT consolidated volume, so any relative-volume filter is
    measuring IEX participation, not market participation.
  * highs/lows can miss prints that occurred away from IEX, so stop-touch
    logic in a backtest may disagree with reality.
Set feed="sip" (paid subscription) before you trust volume-sensitive results.
"""
import os
from datetime import datetime, timedelta
import pandas as pd

NY = "America/New_York"


def get_client():
    from alpaca.data.historical import StockHistoricalDataClient
    key, sec = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    if not key or not sec:
        raise RuntimeError("Set ALPACA_API_KEY and ALPACA_SECRET_KEY (paper keys).")
    return StockHistoricalDataClient(key, sec)


def fetch_bars(symbols, start, end, timeframe_minutes=1, feed="iex",
               cache_dir="data") -> dict[str, pd.DataFrame]:
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    os.makedirs(cache_dir, exist_ok=True)
    if isinstance(symbols, str):
        symbols = [symbols]
    out = {}
    client = get_client()

    for sym in symbols:
        cache = f"{cache_dir}/{sym}_{timeframe_minutes}m_{start}_{end}_{feed}.parquet"
        if os.path.exists(cache):
            out[sym] = pd.read_parquet(cache)
            continue
        req = StockBarsRequest(
            symbol_or_symbols=sym,
            timeframe=TimeFrame(timeframe_minutes, TimeFrameUnit.Minute),
            start=pd.Timestamp(start).to_pydatetime(),
            end=pd.Timestamp(end).to_pydatetime(),
            feed=feed, adjustment="all")
        df = client.get_stock_bars(req).df
        if df.empty:
            print(f"  {sym}: no data"); continue
        df = df.reset_index()
        df = df[df["symbol"] == sym] if "symbol" in df.columns else df
        df = df.set_index("timestamp").tz_convert(NY)
        df = df[["open", "high", "low", "close", "volume"]].astype(float)
        df = regular_hours(df)
        df.to_parquet(cache)
        out[sym] = df
        print(f"  {sym}: {len(df):,} bars {df.index[0].date()} -> {df.index[-1].date()}")
    return out


def regular_hours(df: pd.DataFrame) -> pd.DataFrame:
    """9:30-16:00 ET only. Extended-hours bars have different liquidity and
    will contaminate any intraday statistic you compute."""
    return df.between_time("09:30", "16:00")


def train_test_split(bars: dict, split: float = 0.7):
    """Chronological in-sample / out-of-sample split. Never random-split time
    series -- it leaks future information into the training set."""
    tr, te = {}, {}
    for s, df in bars.items():
        days = sorted(set(df.index.date))
        cut = days[int(len(days) * split)]
        tr[s] = df[df.index.date < cut]
        te[s] = df[df.index.date >= cut]
    return tr, te
