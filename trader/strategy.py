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
    bb_cols = bb.columns.tolist()
    df["bb_upper"] = bb[[c for c in bb_cols if c.startswith("BBU_")][0]]
    df["bb_lower"] = bb[[c for c in bb_cols if c.startswith("BBL_")][0]]
    df["bb_mid"]   = bb[[c for c in bb_cols if c.startswith("BBM_")][0]]
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


# Minimum SL distance from entry — guards against Binance "would trigger immediately"
# rejection when price has already moved between signal calc and order placement.
MIN_SL_DISTANCE_PCT = 0.005  # 0.5%


def calculate_tp_sl(entry, direction, df, leverage=5):
    """
    Calculate TP and SL based on ATR and BB.
    SL is capped so it never exceeds liquidation price.
    At 5x isolated, liq is ~18% from entry — we cap SL at 15% max.
    Returns (tp_price, sl_price, rr_ratio) or (None, None, 0) if inputs invalid.
    """
    last = df.iloc[-1]
    atr  = float(ta.atr(df["high"], df["low"], df["close"], length=14).iloc[-1])

    # Max SL distance: 80% of distance to liquidation
    # At isolated margin, liq ≈ 1/leverage from entry (minus fees)
    # Use 80% of that as absolute max to stay safely above liq
    max_sl_pct = (1.0 / leverage) * 0.80  # e.g. 5x → 16% max SL distance

    if direction == "long":
        # bb_lower can lag ABOVE entry if 1m price gapped down — clamp so SL
        # is always strictly below entry. Without this, max() would pick
        # bb_lower and SL ends up on the wrong side of entry, inverting TP too.
        bb_anchor = min(float(last["bb_lower"]), entry * (1 - MIN_SL_DISTANCE_PCT))
        sl_price = max(entry - atr * 1.5, bb_anchor)
        sl_floor = entry * (1 - max_sl_pct)
        if sl_price < sl_floor:
            sl_price = sl_floor
        tp_price = entry + (entry - sl_price) * 2.0  # 2:1 RR minimum
    else:
        # bb_upper can lag BELOW entry if 1m price spiked up — clamp so SL is
        # always strictly above entry. (See LONG comment above for rationale.)
        bb_anchor = max(float(last["bb_upper"]), entry * (1 + MIN_SL_DISTANCE_PCT))
        sl_price = min(entry + atr * 1.5, bb_anchor)
        sl_ceil = entry * (1 + max_sl_pct)
        if sl_price > sl_ceil:
            sl_price = sl_ceil
        tp_price = entry - (sl_price - entry) * 2.0

    # Post-condition: SL on loss side, TP on profit side. If violated, refuse
    # the trade rather than place an inverted order that Binance will reject
    # and leave the entry naked.
    if direction == "long" and not (sl_price < entry < tp_price):
        logger.error(f"calculate_tp_sl produced inverted LONG: entry={entry} sl={sl_price} tp={tp_price}")
        return None, None, 0
    if direction == "short" and not (tp_price < entry < sl_price):
        logger.error(f"calculate_tp_sl produced inverted SHORT: entry={entry} sl={sl_price} tp={tp_price}")
        return None, None, 0

    rr = abs(tp_price - entry) / abs(sl_price - entry) if abs(sl_price - entry) > 0 else 0
    return tp_price, sl_price, round(rr, 2)
