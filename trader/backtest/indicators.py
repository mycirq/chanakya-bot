"""Intraday indicators (pure pandas, no external deps).

All functions take a per-day OHLCV DataFrame indexed by datetime
and return either a Series aligned to that index or a scalar.
"""
import pandas as pd


def vwap(df: pd.DataFrame) -> pd.Series:
    """Volume-weighted average price, cumulative from session start.
    df: columns [open, high, low, close, volume], indexed by datetime in IST.
    Resets each calendar date."""
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = tp * df["volume"]
    # Group by date so VWAP resets daily
    group = df.index.date
    return pv.groupby(group).cumsum() / df["volume"].groupby(group).cumsum()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range over rolling window."""
    high_low  = df["high"] - df["low"]
    high_pc   = (df["high"] - df["close"].shift(1)).abs()
    low_pc    = (df["low"]  - df["close"].shift(1)).abs()
    tr = pd.concat([high_low, high_pc, low_pc], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=period).mean()


def opening_range(day_df: pd.DataFrame, end_time) -> tuple[float, float] | tuple[None, None]:
    """Return (range_high, range_low) for candles between session open and end_time.
    day_df: single-day OHLCV. end_time: datetime.time."""
    or_candles = day_df[day_df.index.time < end_time]
    if or_candles.empty:
        return None, None
    return float(or_candles["high"].max()), float(or_candles["low"].min())


def rolling_volume_avg(df: pd.DataFrame, days: int = 5) -> pd.Series:
    """5-day rolling average of per-candle volume, aligned to candle index.
    Used for 'volume spike' confirmation."""
    # Average volume at this same time-of-day across the last N trading days.
    df = df.copy()
    df["time_of_day"] = df.index.time
    # For each row, lookup the rolling mean of same time-of-day from prior N days
    by_tod = df.groupby("time_of_day")["volume"].rolling(days, min_periods=1).mean()
    by_tod = by_tod.reset_index(level=0, drop=True).sort_index()
    return by_tod


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.where(delta > 0, 0.0).rolling(period).mean()
    loss  = (-delta.where(delta < 0, 0.0)).rolling(period).mean()
    rs    = gain / loss.replace(0, pd.NA)
    return 100 - (100 / (1 + rs))
