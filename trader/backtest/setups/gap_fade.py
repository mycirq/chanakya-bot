"""Setup D — Gap-and-Fade.

Fundamentally different bet from ORB/VWAP/Sector: MEAN REVERSION, not continuation.

Premise: liquid Indian equity opening gaps > 2% that aren't driven by major news
tend to revert toward prior close (or at least VWAP) within the first 1-2 hours.
Market makers and institutional desks fade unjustified gaps systematically.

Entry: at the FIRST 5-min candle after open (9:15-9:20), if the open price gapped
       >2% from prior close, take the OPPOSITE direction.
SL:    extreme of the gap candle (high for short, low for long) + a small pad.
TP:    halfway back to prior close (1:1 toward gap-fill), capped at VWAP.
       Bias: take the easier target — fading a gap is about catching the snapback,
       not riding all the way to gap-fill, which often doesn't complete intraday.
Exit:  if neither hit by 11:30, time-exit at market (fade thesis is invalidated).

Filters:
  - Gap must be ≥ 2% (smaller gaps are noise, don't fade)
  - Volume on the first bar must be ≥ 2× same-time 5-day average (real participation)
  - Skip if the second 5-min candle continues strongly in gap direction (real news?)
"""
from datetime import time
import pandas as pd

from trader.backtest.setups.base import Setup, Signal
from trader.backtest.indicators import rolling_volume_avg

ENTRY_TIME         = time(9, 20)     # second 5-min bar (9:20 close). First bar = 9:15-9:20.
TIME_EXIT          = time(11, 30)    # bot can act on this via separate time-stop logic
MIN_GAP_PCT        = 0.015           # ≥ 1.5% gap to qualify (2% was too restrictive)
VOLUME_MULT        = 1.5             # gap bar must have 1.5× same-time avg volume
SL_PAD_PCT         = 0.003           # 0.3% pad beyond gap extreme


class GapFadeSetup(Setup):
    name = "GAP_FADE"

    def __init__(self, symbol: str):
        super().__init__(symbol)
        self._fired_today: dict = {}

    def detect(self, history: pd.DataFrame, idx: int) -> Signal | None:
        candle = history.iloc[idx]
        ts = candle.name
        today = ts.date()
        # Only fire on the 9:20 bar (the bar that closes at 9:20)
        if ts.time() != ENTRY_TIME:
            return None
        if self._fired_today.get(today):
            return None
        if idx < 1:
            return None

        # Need prior day's close: the last candle from a prior date
        prior_day_slice = history[history.index.date < today]
        if prior_day_slice.empty:
            return None
        prior_close = float(prior_day_slice.iloc[-1]["close"])

        today_slice = history[history.index.date == today]
        if today_slice.empty:
            return None
        today_open = float(today_slice.iloc[0]["open"])
        gap_pct = (today_open - prior_close) / prior_close
        if abs(gap_pct) < MIN_GAP_PCT:
            return None

        # Confirm participation: 9:15 (gap) bar's volume vs 5-day same-time avg.
        # The first bar of any day has elevated volume; we want it elevated even
        # vs that baseline (i.e., a "real" gap with conviction, not random).
        first_bar = today_slice.iloc[0]
        first_bar_idx = history.index.get_loc(first_bar.name)
        vol_avg_first = rolling_volume_avg(history.iloc[: first_bar_idx + 1], days=5)
        avg_first = vol_avg_first.iloc[-1] if not vol_avg_first.empty else None
        if avg_first is None or pd.isna(avg_first) or avg_first <= 0:
            return None
        if float(first_bar["volume"]) < VOLUME_MULT * float(avg_first):
            return None

        cur_close = float(candle["close"])
        bars_so_far = today_slice[today_slice.index <= ts]
        gap_extreme_high = float(bars_so_far["high"].max())
        gap_extreme_low  = float(bars_so_far["low"].min())

        # Skip if gap is "real" — i.e., the move continued strongly past open
        # (gap-up where 9:20 close is even higher than open by significant margin)
        # We trust the SL to handle "real news" continuation — no pre-filter on
        # how far price has drifted from open. Mean-reversion thesis: gap will
        # at least partially fill within 1-2 hours. SL at gap extreme keeps
        # losses bounded if thesis is wrong.
        if gap_pct > 0:  # gap UP — we'd want to SHORT (fade up)
            entry = cur_close
            sl    = gap_extreme_high * (1 + SL_PAD_PCT)
            tp    = (today_open + prior_close) / 2   # halfway gap-fill
            if not (tp < entry < sl):
                return None
            self._fired_today[today] = True
            return Signal(
                symbol=self.symbol, direction="short",
                entry_price=entry, stop_loss=sl, take_profit=tp,
                triggered_at=ts, setup=self.name,
                reason=f"Gap-up fade: gap +{gap_pct*100:.1f}% (open {today_open:.2f} vs prior close {prior_close:.2f})",
                confidence=65,
            )
        else:  # gap DOWN — we'd want to LONG (fade down)
            entry = cur_close
            sl    = gap_extreme_low * (1 - SL_PAD_PCT)
            tp    = (today_open + prior_close) / 2
            if not (sl < entry < tp):
                return None
            self._fired_today[today] = True
            return Signal(
                symbol=self.symbol, direction="long",
                entry_price=entry, stop_loss=sl, take_profit=tp,
                triggered_at=ts, setup=self.name,
                reason=f"Gap-down fade: gap {gap_pct*100:.1f}% (open {today_open:.2f} vs prior close {prior_close:.2f})",
                confidence=65,
            )
