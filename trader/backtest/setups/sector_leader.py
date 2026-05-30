"""Setup C — Sector Leader (V0: relative strength vs broad market).

V0 simplification: use NIFTY 50 as proxy for "market is bullish/bearish today."
A leader is a stock outperforming NIFTY 50 intraday with momentum confirmation.

  Long signal:
    NIFTY 50 day's return > +0.5%
    AND stock day's return > +1.5% (i.e., leading the market)
    AND current 5-min close breaks prior 30-min high
    AND current bar volume > 1.2× same-time 5-day average

  Short signal: symmetric (NIFTY 50 down >0.5%, stock down >1.5%, breaks 30-min low).

SL: 1.5% from entry.  TP: 3× the risk (4.5%) OR market reversal, whichever first.
Entry window: 10:00 – 14:30 (need market direction to develop, exit before close).

V1 upgrade path: replace NIFTY 50 proxy with stock's actual sector index
(NIFTYBANK / NIFTYIT / NIFTYAUTO / etc) using a stock→sector mapping table.
"""
from datetime import time
import pandas as pd

from trader.backtest.setups.base import Setup, Signal
from trader.backtest.indicators import rolling_volume_avg

ENTRY_WINDOW_START = time(10, 0)
ENTRY_WINDOW_END   = time(14, 30)
MARKET_THRESHOLD   = 0.005       # 0.5% on NIFTY 50
STOCK_THRESHOLD    = 0.015       # 1.5% on stock (outperformance)
BREAKOUT_LOOKBACK_MIN = 30       # break of prior 30-min extreme
VOLUME_MULT        = 1.2
SL_PCT             = 0.015       # 1.5%
RR                 = 3.0         # TP at 3× risk


class SectorLeaderSetup(Setup):
    name = "SECTOR_LEADER"

    def __init__(self, symbol: str):
        super().__init__(symbol)
        self._fired_today: dict = {}

    def detect(
        self, history: pd.DataFrame, idx: int,
        market_context: dict | None = None,
    ) -> Signal | None:
        if market_context is None or "market_return_pct" not in market_context:
            return None  # engine must supply NIFTY 50 return for current bar

        candle = history.iloc[idx]
        ts = candle.name
        today = ts.date()
        if not (ENTRY_WINDOW_START <= ts.time() < ENTRY_WINDOW_END):
            return None
        if self._fired_today.get(today):
            return None

        day_slice = history[(history.index.date == today) & (history.index <= ts)]
        if len(day_slice) < 6:
            return None

        day_open = float(day_slice.iloc[0]["open"])
        cur_close = float(candle["close"])
        stock_return = (cur_close - day_open) / day_open
        market_return = float(market_context["market_return_pct"])

        # 30-min lookback (6 × 5-min bars) — excluding current bar
        prior = day_slice.iloc[-7:-1] if len(day_slice) >= 7 else day_slice.iloc[:-1]
        prior_high = float(prior["high"].max())
        prior_low  = float(prior["low"].min())

        # Volume confirmation
        vol_avg = rolling_volume_avg(history.iloc[: idx + 1], days=5)
        avg_now = vol_avg.iloc[-1] if not vol_avg.empty else None
        if avg_now is None or pd.isna(avg_now) or avg_now <= 0:
            return None
        vol_ok = float(candle["volume"]) >= VOLUME_MULT * float(avg_now)
        if not vol_ok:
            return None

        # Long: bullish market + stock leading + breaks 30-min high
        if (market_return > MARKET_THRESHOLD
            and stock_return > STOCK_THRESHOLD
            and cur_close > prior_high):
            entry = cur_close
            sl    = entry * (1 - SL_PCT)
            tp    = entry * (1 + SL_PCT * RR)
            self._fired_today[today] = True
            return Signal(
                symbol=self.symbol, direction="long",
                entry_price=entry, stop_loss=sl, take_profit=tp,
                triggered_at=ts, setup=self.name,
                reason=f"Sector leader: NIFTY +{market_return*100:.1f}%, stock +{stock_return*100:.1f}%, broke {prior_high:.2f}",
                confidence=60,
            )
        # Short: bearish market + stock leading down + breaks 30-min low
        if (market_return < -MARKET_THRESHOLD
            and stock_return < -STOCK_THRESHOLD
            and cur_close < prior_low):
            entry = cur_close
            sl    = entry * (1 + SL_PCT)
            tp    = entry * (1 - SL_PCT * RR)
            self._fired_today[today] = True
            return Signal(
                symbol=self.symbol, direction="short",
                entry_price=entry, stop_loss=sl, take_profit=tp,
                triggered_at=ts, setup=self.name,
                reason=f"Sector leader (short): NIFTY {market_return*100:.1f}%, stock {stock_return*100:.1f}%, broke {prior_low:.2f}",
                confidence=60,
            )
        return None
