"""Historical 5-min candle fetcher with on-disk parquet cache.

Kite limits historical_data to 60 days per call for 5-minute interval, so we
chunk longer ranges. Rate limit is 3 req/s — we sleep 0.4s between calls.
Cached files are per-symbol-per-quarter so re-runs are cheap.
"""
import time
import logging
from datetime import datetime, timedelta, date
from pathlib import Path

import pandas as pd
import pytz

from trader.backtest.config import CANDLES_DIR, DEFAULT_INTERVAL
from trader.backtest.kite_session import get_kite

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# Kite per-call window limits per interval (days)
_MAX_DAYS_PER_CALL = {
    "minute":    60,
    "3minute":   90,
    "5minute":   90,
    "10minute": 100,
    "15minute": 200,
    "30minute": 200,
    "60minute": 400,
    "day":      2000,
}


def _cache_path(symbol: str, interval: str) -> Path:
    return CANDLES_DIR / f"{symbol}__{interval}.parquet"


def _ensure_dt_index(df: pd.DataFrame) -> pd.DataFrame:
    """pandas 3.0 silently downgrades DatetimeIndex to Index when concatenating
    DataFrames whose indexes have mismatched timezone awareness. This re-coerces
    to DatetimeIndex (UTC-naive) so downstream `.date` calls work."""
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, utc=True).tz_convert(IST).tz_localize(None)
    elif df.index.tz is not None:
        df.index = df.index.tz_convert(IST).tz_localize(None)
    return df


def _fetch_chunk(token: int, frm: datetime, to: datetime, interval: str) -> list[dict]:
    """One Kite API call. Returns raw candle dicts."""
    return get_kite().historical_data(token, frm, to, interval, continuous=False)


def fetch_symbol_history(
    symbol: str,
    instrument_token: int,
    from_date: date,
    to_date: date,
    interval: str = DEFAULT_INTERVAL,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """Returns DataFrame indexed by IST datetime with [open, high, low, close, volume].

    Cached on disk. If cache covers the requested range, no API call is made.
    If cache covers part of the range, only the missing tail is fetched.
    """
    cache = _cache_path(symbol, interval)
    cached: pd.DataFrame | None = None

    if cache.exists() and not force_refresh:
        cached = _ensure_dt_index(pd.read_parquet(cache))
        if not cached.empty:
            cached_min = cached.index.min().date()
            cached_max = cached.index.max().date()
            # Cache fully covers? Slice and return.
            if cached_min <= from_date and cached_max >= to_date:
                return cached.loc[
                    (cached.index.date >= from_date) & (cached.index.date <= to_date)
                ]
            # Cache partially covers — extend the fetch window
            from_date = min(from_date, cached_max + timedelta(days=1))

    # Chunk-fetch from Kite
    max_days = _MAX_DAYS_PER_CALL.get(interval, 60)
    chunks: list[dict] = []
    cur = datetime.combine(from_date, datetime.min.time()).replace(tzinfo=IST)
    end = datetime.combine(to_date,   datetime.max.time()).replace(tzinfo=IST)
    while cur < end:
        chunk_end = min(cur + timedelta(days=max_days), end)
        try:
            data = _fetch_chunk(instrument_token, cur, chunk_end, interval)
            chunks.extend(data)
            logger.debug(f"{symbol}: fetched {len(data)} candles {cur.date()}→{chunk_end.date()}")
        except Exception as e:
            logger.error(f"{symbol}: fetch failed {cur.date()}→{chunk_end.date()}: {type(e).__name__}: {e}")
        cur = chunk_end + timedelta(seconds=1)
        time.sleep(0.4)  # Kite rate limit: 3 req/s

    if not chunks:
        return cached if cached is not None else pd.DataFrame()

    new_df = pd.DataFrame(chunks)
    new_df["date"] = pd.to_datetime(new_df["date"])
    new_df = new_df.set_index("date")
    new_df = new_df[["open", "high", "low", "close", "volume"]].astype(
        {"open": "float64", "high": "float64", "low": "float64", "close": "float64", "volume": "int64"}
    )
    new_df = _ensure_dt_index(new_df)

    # Merge with cache and persist
    if cached is not None and not cached.empty:
        merged = pd.concat([cached, new_df])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        merged = _ensure_dt_index(merged)
    else:
        merged = new_df

    merged.to_parquet(cache, index=True)
    return merged.loc[
        (merged.index.date >= from_date) & (merged.index.date <= to_date)
    ]


def fetch_universe_history(
    universe_df: pd.DataFrame,
    from_date: date,
    to_date: date,
    interval: str = DEFAULT_INTERVAL,
    force_refresh: bool = False,
) -> dict[str, pd.DataFrame]:
    """Fetch all symbols in the universe. Returns {symbol: DataFrame}."""
    out: dict[str, pd.DataFrame] = {}
    n = len(universe_df)
    for idx, row in enumerate(universe_df.itertuples(index=False), start=1):
        try:
            df = fetch_symbol_history(
                row.tradingsymbol, row.instrument_token,
                from_date, to_date, interval, force_refresh
            )
            if not df.empty:
                out[row.tradingsymbol] = df
            if idx % 20 == 0:
                logger.info(f"Fetched {idx}/{n} symbols")
        except Exception as e:
            logger.error(f"Skipping {row.tradingsymbol}: {type(e).__name__}: {e}")
    logger.info(f"Done: {len(out)}/{n} symbols have data")
    return out
