"""Setup A — Opening Range Breakout (ORB).

Mark the high/low of the first 15 min (3 × 5-min candles, 9:15–9:30).

Tuned parameters (v2, after first backtest showed 40% WR with too many
late-window false breakouts):
  - Volume confirmation 2.5× (was 1.5×) — fewer, higher-conviction signals
  - Entry window 9:30-10:00 only (was 9:30-11:00) — 10am+ ORB had 15% WR
  - 2-candle confirmation — require two consecutive closes beyond range
    before triggering (prev candle ALSO closed beyond range)

SL: opposite end of opening range.
TP: 2× the range width from entry.
"""
from datetime import time
import pandas as pd

from trader.backtest.setups.base import Setup, Signal
from trader.backtest.config import OPENING_RANGE_END
from trader.backtest.indicators import rolling_volume_avg

ENTRY_WINDOW_END = time(10, 0)
VOLUME_MULT      = 2.5


class ORBSetup(Setup):
    name = "ORB"

    def __init__(self, symbol: str):
        super().__init__(symbol)
        self._range_cache: dict = {}   # date -> (high, low)
        self._fired_today: dict = {}   # date -> True if we've already signaled today

    def _get_range(self, history: pd.DataFrame, today) -> tuple[float, float] | None:
        if today in self._range_cache:
            return self._range_cache[today]
        day_df = history[history.index.date == today]
        or_candles = day_df[day_df.index.time < OPENING_RANGE_END]
        if or_candles.empty:
            return None
        r = (float(or_candles["high"].max()), float(or_candles["low"].min()))
        self._range_cache[today] = r
        return r

    def detect(self, history: pd.DataFrame, idx: int) -> Signal | None:
        candle = history.iloc[idx]
        ts = candle.name  # datetime
        today = ts.date()

        # Only act within the entry window
        if ts.time() < OPENING_RANGE_END or ts.time() >= ENTRY_WINDOW_END:
            return None
        # One signal per day per symbol
        if self._fired_today.get(today):
            return None

        rng = self._get_range(history.iloc[: idx + 1], today)
        if rng is None:
            return None
        orh, orl = rng
        if orh <= orl:
            return None

        # Volume confirmation: this bar's volume vs 5-day same-time-of-day avg
        vol_avg = rolling_volume_avg(history.iloc[: idx + 1], days=5)
        avg_now = vol_avg.iloc[-1] if not vol_avg.empty else None
        if avg_now is None or pd.isna(avg_now) or avg_now <= 0:
            return None
        vol_ok = float(candle["volume"]) >= VOLUME_MULT * float(avg_now)
        if not vol_ok:
            return None

        close = float(candle["close"])
        range_width = orh - orl

        # 2-candle confirmation: previous candle must ALSO close beyond range
        # in the same direction. Reduces false-breakout fakeouts.
        if idx == 0:
            return None
        prev_close = float(history.iloc[idx - 1]["close"])
        # The previous candle must be inside or just beyond the breakout side.
        # We require: prev_close is between range and current close (a clear
        # 2-bar push beyond the range).
        if close > orh:
            if prev_close <= orh:
                return None  # current bar IS the first to break — wait for confirmation
            entry = close
            sl    = orl
            tp    = entry + 2 * range_width
            self._fired_today[today] = True
            return Signal(
                symbol=self.symbol, direction="long",
                entry_price=entry, stop_loss=sl, take_profit=tp,
                triggered_at=ts, setup=self.name,
                reason=f"ORB confirmed breakout above {orh:.2f} (range {range_width:.2f}, vol {candle['volume']/avg_now:.1f}x)",
                confidence=75,
            )
        if close < orl:
            if prev_close >= orl:
                return None
            entry = close
            sl    = orh
            tp    = entry - 2 * range_width
            self._fired_today[today] = True
            return Signal(
                symbol=self.symbol, direction="short",
                entry_price=entry, stop_loss=sl, take_profit=tp,
                triggered_at=ts, setup=self.name,
                reason=f"ORB confirmed breakdown below {orl:.2f} (range {range_width:.2f}, vol {candle['volume']/avg_now:.1f}x)",
                confidence=75,
            )
        return None
