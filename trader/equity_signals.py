"""
Equity intraday signal — technical CONFIRMATION on a research-vetted watchlist name.

The edge thesis is the overnight research watchlist (news/events). This module is
the confirmation layer: it only fires when the 5-min technicals agree, and it sizes
every trade off a fixed rupee risk (₹750 half-size) with a hard SL distance cap.
"""
import logging
import math

import pandas_ta as ta

from trader.strategy import compute_indicators, score_signal
from trader.equity_kite import get_equity_ohlcv, get_equity_ltp
from trader.config import (
    EQUITY_RISK_PER_TRADE_INR, EQUITY_MAX_SL_PCT, EQUITY_MIN_SIGNAL_SCORE,
)

logger = logging.getLogger(__name__)


def evaluate_equity(symbol: str, bias: str | None = None) -> dict | None:
    """
    Returns a trade plan dict, or None to skip.
      bias: 'long' / 'short' / 'neutral' from the watchlist thesis. A directional
            bias gates the signal — we only take trades that agree with it.
    Plan: {symbol, direction, score, reason, entry, sl, tp, quantity, risk_inr}
    """
    ohlcv = get_equity_ohlcv(symbol, "5minute", days=10)
    df = compute_indicators(ohlcv)
    if df is None or len(df) < 5:
        return None

    score, direction, reason = score_signal(df)
    logger.info(f"equity {symbol}: score={score} dir={direction} bias={bias} (need {EQUITY_MIN_SIGNAL_SCORE})")
    if not direction or score < EQUITY_MIN_SIGNAL_SCORE:
        return None

    # Bias gate removed (user choice 2026-06-03): the watchlist selects WHICH stocks
    # are in play; we trade whichever direction the intraday technicals point, even
    # if that opposes the news bias. `bias` is kept for logging/context only.

    last = df.iloc[-1]
    # Prefer a live LTP for the entry reference; fall back to last 5-min close.
    ltp = get_equity_ltp([symbol]).get(symbol.upper())
    entry = float(ltp or last["close"])

    # SL distance: 1.5×ATR, floored at 0.5% and capped at EQUITY_MAX_SL_PCT of price.
    try:
        atr = float(ta.atr(df["high"], df["low"], df["close"], length=14).iloc[-1])
    except Exception:
        atr = entry * 0.01
    sl_dist = max(atr * 1.5, entry * 0.005)
    sl_dist = min(sl_dist, entry * EQUITY_MAX_SL_PCT)
    if sl_dist <= 0:
        return None

    if direction == "long":
        sl = round(entry - sl_dist, 2)
        tp = round(entry + sl_dist * 2.0, 2)   # 2:1 reward:risk
    else:
        sl = round(entry + sl_dist, 2)
        tp = round(entry - sl_dist * 2.0, 2)

    risk_per_share = abs(entry - sl)
    if risk_per_share <= 0:
        return None
    quantity = math.floor(EQUITY_RISK_PER_TRADE_INR / risk_per_share)
    if quantity < 1:
        logger.info(f"{symbol}: risk/share ₹{risk_per_share:.2f} too large for ₹{EQUITY_RISK_PER_TRADE_INR} cap — skip")
        return None

    return {
        "symbol": symbol.upper(),
        "direction": direction,
        "score": score,
        "reason": reason,
        "entry": round(entry, 2),
        "sl": sl,
        "tp": tp,
        "quantity": quantity,
        "risk_inr": round(quantity * risk_per_share, 2),
    }
