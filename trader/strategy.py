import logging
import pandas as pd
import pandas_ta as ta
from trader.config import (
    RSI_PERIOD, MACD_FAST, MACD_SLOW, MACD_SIGNAL,
    EMA_FAST, EMA_SLOW, BB_PERIOD, BB_STD, MIN_SIGNAL_SCORE
)

logger = logging.getLogger(__name__)


def compute_indicators(ohlcv):
    """Convert raw OHLCV to DataFrame with indicators."""
    if len(ohlcv) < EMA_SLOW + 10:
        return None

    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
    df["close"] = df["close"].astype(float)
    df["high"]  = df["high"].astype(float)
    df["low"]   = df["low"].astype(float)
    df["volume"]= df["volume"].astype(float)

    df["rsi"]  = ta.rsi(df["close"], length=RSI_PERIOD)
    macd       = ta.macd(df["close"], fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL)
    df["macd"] = macd[f"MACD_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}"]
    df["macd_signal"] = macd[f"MACDs_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}"]
    df["macd_hist"]   = macd[f"MACDh_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}"]
    df["ema_fast"] = ta.ema(df["close"], length=EMA_FAST)
    df["ema_slow"] = ta.ema(df["close"], length=EMA_SLOW)
    bb = ta.bbands(df["close"], length=BB_PERIOD, std=BB_STD)
    df["bb_upper"] = bb[f"BBU_{BB_PERIOD}_{float(BB_STD)}"]
    df["bb_lower"] = bb[f"BBL_{BB_PERIOD}_{float(BB_STD)}"]
    df["bb_mid"]   = bb[f"BBM_{BB_PERIOD}_{float(BB_STD)}"]
    df["vol_ma"]   = df["volume"].rolling(20).mean()

    return df.dropna()


def score_signal(df):
    """
    Score a pair's signal strength 0-100.
    Returns (score, direction, reasoning)
    direction: 'long', 'short', or None
    """
    if df is None or len(df) < 5:
        return 0, None, "insufficient data"

    last  = df.iloc[-1]
    prev  = df.iloc[-2]
    score = 0
    long_pts  = 0
    short_pts = 0
    reasons   = []

    # ── RSI ────────────────────────────────────────────────────────────────────
    rsi = last["rsi"]
    if rsi < 35:
        long_pts += 20
        reasons.append(f"RSI oversold ({rsi:.1f})")
    elif rsi > 65:
        short_pts += 20
        reasons.append(f"RSI overbought ({rsi:.1f})")
    elif 35 <= rsi <= 50 and prev["rsi"] < last["rsi"]:
        long_pts += 10
        reasons.append(f"RSI recovering ({rsi:.1f})")
    elif 50 <= rsi <= 65 and prev["rsi"] > last["rsi"]:
        short_pts += 10
        reasons.append(f"RSI fading ({rsi:.1f})")

    # ── MACD ───────────────────────────────────────────────────────────────────
    macd_cross_up   = prev["macd"] < prev["macd_signal"] and last["macd"] > last["macd_signal"]
    macd_cross_down = prev["macd"] > prev["macd_signal"] and last["macd"] < last["macd_signal"]
    if macd_cross_up:
        long_pts += 25
        reasons.append("MACD bullish crossover")
    elif macd_cross_down:
        short_pts += 25
        reasons.append("MACD bearish crossover")
    elif last["macd_hist"] > 0 and last["macd_hist"] > prev["macd_hist"]:
        long_pts += 10
        reasons.append("MACD histogram rising")
    elif last["macd_hist"] < 0 and last["macd_hist"] < prev["macd_hist"]:
        short_pts += 10
        reasons.append("MACD histogram falling")

    # ── EMA trend ──────────────────────────────────────────────────────────────
    price = last["close"]
    if price > last["ema_fast"] > last["ema_slow"]:
        long_pts += 20
        reasons.append("Price > EMA50 > EMA200 (uptrend)")
    elif price < last["ema_fast"] < last["ema_slow"]:
        short_pts += 20
        reasons.append("Price < EMA50 < EMA200 (downtrend)")

    # ── Bollinger Bands ────────────────────────────────────────────────────────
    if price <= last["bb_lower"]:
        long_pts += 15
        reasons.append("Price at lower BB (oversold)")
    elif price >= last["bb_upper"]:
        short_pts += 15
        reasons.append("Price at upper BB (overbought)")

    # ── Volume confirmation ────────────────────────────────────────────────────
    if last["volume"] > last["vol_ma"] * 1.5:
        if long_pts >= short_pts:
            long_pts += 10
        else:
            short_pts += 10
        reasons.append(f"Volume spike ({last['volume']/last['vol_ma']:.1f}x avg)")

    # ── Final scoring ──────────────────────────────────────────────────────────
    if long_pts > short_pts:
        score     = min(int((long_pts / 90) * 100), 100)
        direction = "long"
    elif short_pts > long_pts:
        score     = min(int((short_pts / 90) * 100), 100)
        direction = "short"
    else:
        return 0, None, "no clear signal"

    return score, direction, " | ".join(reasons)


def calculate_tp_sl(entry, direction, df):
    """
    Calculate TP and SL based on ATR and BB.
    Returns (tp_price, sl_price, rr_ratio)
    """
    last = df.iloc[-1]
    atr  = float(ta.atr(df["high"], df["low"], df["close"], length=14).iloc[-1])

    if direction == "long":
        sl_price = max(entry - atr * 1.5, last["bb_lower"])
        tp_price = entry + (entry - sl_price) * 2.0  # 2:1 RR minimum
    else:
        sl_price = min(entry + atr * 1.5, last["bb_upper"])
        tp_price = entry - (sl_price - entry) * 2.0

    rr = abs(tp_price - entry) / abs(sl_price - entry)
    return tp_price, sl_price, round(rr, 2)
