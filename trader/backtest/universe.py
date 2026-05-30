"""NSE F&O-eligible universe.

A stock is F&O-eligible if it has a corresponding futures contract on NFO.
NSE updates the list monthly; for backtest we use the current snapshot, which
introduces some survivorship bias (stocks dropped from F&O won't appear).
Acceptable for MVP — most de-listings are obvious dogs we'd skip anyway.
"""
import logging
import pandas as pd
from trader.backtest.config import UNIVERSE_FILE
from trader.backtest.kite_session import get_kite

logger = logging.getLogger(__name__)


def fetch_fno_universe() -> pd.DataFrame:
    """Hit Kite for fresh instruments + write parquet. Returns DataFrame."""
    kite = get_kite()
    # All NFO futures — their 'name' field is the underlying equity symbol
    nfo = kite.instruments("NFO")
    fno_symbols = {i["name"] for i in nfo if i.get("instrument_type") == "FUT"}
    logger.info(f"Found {len(fno_symbols)} F&O underlyings on NFO")

    # All NSE equity. Filter to ones in F&O list.
    nse = kite.instruments("NSE")
    rows = [
        {
            "tradingsymbol":    i["tradingsymbol"],
            "instrument_token": int(i["instrument_token"]),
            "name":             i.get("name", i["tradingsymbol"]),
            "lot_size":         int(i.get("lot_size", 1)),
            "tick_size":        float(i.get("tick_size", 0.05)),
        }
        for i in nse
        if i.get("instrument_type") == "EQ" and i["tradingsymbol"] in fno_symbols
    ]
    df = pd.DataFrame(rows).sort_values("tradingsymbol").reset_index(drop=True)
    df.to_parquet(UNIVERSE_FILE, index=False)
    logger.info(f"Wrote {len(df)} F&O-eligible NSE equity rows → {UNIVERSE_FILE}")
    return df


def load_universe(refresh: bool = False) -> pd.DataFrame:
    """Return cached universe DataFrame, refreshing from Kite if asked or missing."""
    if not refresh and UNIVERSE_FILE.exists():
        return pd.read_parquet(UNIVERSE_FILE)
    return fetch_fno_universe()
