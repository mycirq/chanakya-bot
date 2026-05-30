"""Setup B — VWAP Pullback in a trend.

Stock has been on one side of VWAP for 30+ minutes (= 6 × 5-min candles)
indicating an intraday trend. Price pulls back to VWAP (touches ±0.3%). Enter
on the first 5-min candle that confirms the trend (closes back on the trend
side AND closes above the prior candle's close for long, below for short).

SL: 0.5% beyond the pullback extreme (low of pullback candle for long).
TP: today's HOD (long) or LOD (short).
Entry window: 10:30 – 14:00 (need time for trend, exit before square-off).
"""
from datetime import time
import pandas as pd

from trader.backtest.setups.base import Setup, Signal
from trader.backtest.indicators import vwap

ENTRY_WINDOW_START = time(10, 30)
ENTRY_WINDOW_END   = time(14, 0)
TREND_BARS         = 6           # 6 × 5min = 30 min of one-side-of-VWAP
VWAP_TOUCH_PCT     = 0.003       # 0.3%
SL_PAD_PCT         = 0.005       # 0.5% beyond pullback extreme


class VWAPPullbackSetup(Setup):
    name = "VWAP_PULLBACK"

    def __init__(self, symbol: str):
        super().__init__(symbol)
        self._fired_today: dict = {}

    def detect(self, history: pd.DataFrame, idx: int) -> Signal | None:
        candle = history.iloc[idx]
        ts = candle.name
        today = ts.date()
        if not (ENTRY_WINDOW_START <= ts.time() < ENTRY_WINDOW_END):
            return None
        if self._fired_today.get(today):
            return None
        if idx < 2:
            return None

        # Today's history so far (need at least TREND_BARS + a few)
        day_slice = history[(history.index.date == today) & (history.index <= ts)]
        if len(day_slice) < TREND_BARS + 1:
            return None

        v = vwap(day_slice)
        if v.isna().iloc[-1]:
            return None

        cur_close = float(candle["close"])
        prev_close = float(history.iloc[idx - 1]["close"])
        cur_vwap   = float(v.iloc[-1])
        prev_vwap  = float(v.iloc[-2])

        # Trend check: last TREND_BARS closes all on same side of VWAP
        recent = day_slice.iloc[-(TREND_BARS + 1):-1]   # exclude current candle
        recent_v = v.iloc[-(TREND_BARS + 1):-1]
        above = (recent["close"] > recent_v).all()
        below = (recent["close"] < recent_v).all()

        # Pullback check: this candle's low (long) or high (short) touched VWAP
        cur_low  = float(candle["low"])
        cur_high = float(candle["high"])
        touched_from_above = above and cur_low <= cur_vwap * (1 + VWAP_TOUCH_PCT)
        touched_from_below = below and cur_high >= cur_vwap * (1 - VWAP_TOUCH_PCT)

        # Confirmation: this candle closes back on the trend side AND above/below prev close
        if touched_from_above and cur_close > cur_vwap and cur_close > prev_close:
            entry = cur_close
            sl    = cur_low * (1 - SL_PAD_PCT)
            tp    = float(day_slice["high"].max())
            if tp <= entry or sl >= entry:
                return None
            self._fired_today[today] = True
            return Signal(
                symbol=self.symbol, direction="long",
                entry_price=entry, stop_loss=sl, take_profit=tp,
                triggered_at=ts, setup=self.name,
                reason=f"VWAP pullback in uptrend (VWAP {cur_vwap:.2f}, low {cur_low:.2f}, HOD {tp:.2f})",
                confidence=65,
            )
        if touched_from_below and cur_close < cur_vwap and cur_close < prev_close:
            entry = cur_close
            sl    = cur_high * (1 + SL_PAD_PCT)
            tp    = float(day_slice["low"].min())
            if tp >= entry or sl <= entry:
                return None
            self._fired_today[today] = True
            return Signal(
                symbol=self.symbol, direction="short",
                entry_price=entry, stop_loss=sl, take_profit=tp,
                triggered_at=ts, setup=self.name,
                reason=f"VWAP pullback in downtrend (VWAP {cur_vwap:.2f}, high {cur_high:.2f}, LOD {tp:.2f})",
                confidence=65,
            )
        return None
