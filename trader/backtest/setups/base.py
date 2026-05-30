"""Common interface for setup logic. Same module is imported by both the
backtest replay engine and the live equity engine, so setups MUST be pure
functions over the candle history — no Kite/DB/Slack calls."""
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import pandas as pd

Direction = Literal["long", "short"]


@dataclass(frozen=True)
class Signal:
    """A setup-triggered entry signal."""
    symbol: str
    direction: Direction
    entry_price: float
    stop_loss: float
    take_profit: float
    triggered_at: datetime
    setup: str            # 'ORB' | 'VWAP_PULLBACK' | 'SECTOR_LEADER'
    reason: str           # short human description
    confidence: int       # 0-100, higher = stronger signal


class Setup:
    """Stateful setup detector. Engine calls .on_candle once per new bar.

    Subclasses override .detect() returning a Signal or None.
    State is kept per-symbol (engine creates one instance per symbol per setup).
    """
    name: str = "BASE"

    def __init__(self, symbol: str):
        self.symbol = symbol

    def detect(self, history: pd.DataFrame, idx: int) -> Signal | None:
        """history is the symbol's full DataFrame; idx is the current candle index.
        Setups only see history[:idx+1] (no look-ahead) — engine enforces."""
        raise NotImplementedError
