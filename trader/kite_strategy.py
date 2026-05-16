"""
FnO signal scoring and option contract selection.
Reuses compute_indicators/score_signal from strategy.py for index signals,
then adds strike selection and premium-based TP/SL.
"""
import logging
from trader.config import (
    KITE_INDICES, KITE_PREMIUM_TP_PCT, KITE_PREMIUM_SL_PCT,
    KITE_MIN_SIGNAL_SCORE, KITE_MAX_POSITION_PCT, KITE_CAPITAL_INR
)
from trader.strategy import compute_indicators, score_signal
from trader.kite import (
    get_ohlcv, get_ltp, find_option, get_nearest_weekly_expiry
)

logger = logging.getLogger(__name__)


def get_index_signal(underlying: str):
    """
    Fetch 5-min OHLCV for index, score the signal.
    Returns (score, direction, reason) or (0, None, reason).
    """
    cfg = KITE_INDICES.get(underlying)
    if not cfg:
        return 0, None, f"unknown underlying {underlying}"

    # Use 5-min candles, last 3 days (enough for EMA200 on 5min)
    ohlcv = get_ohlcv(cfg["token"], interval="5minute", days=3)
    if not ohlcv:
        return 0, None, "no OHLCV data"

    df = compute_indicators(ohlcv)
    if df is None or len(df) < 5:
        return 0, None, "insufficient candles after indicator calc"

    return score_signal(df)


def get_atm_strike(underlying: str, ltp: float) -> float:
    """Round LTP to nearest valid strike for the underlying."""
    gap = KITE_INDICES[underlying]["strike_gap"]
    return round(ltp / gap) * gap


def select_option_contract(underlying: str, direction: str, score: int):
    """
    Pick the right option contract.
    Returns (tradingsymbol, token, lot_size, premium, quantity) or None.
    direction: 'long' (buy call) or 'short' (buy put).
    """
    cfg = KITE_INDICES[underlying]
    quote_key = cfg["quote"]

    # Get current index LTP
    ltp_data = get_ltp([quote_key])
    index_ltp = ltp_data.get(quote_key)
    if not index_ltp:
        logger.warning(f"Could not get LTP for {quote_key}")
        return None

    expiry = get_nearest_weekly_expiry(underlying)
    atm    = get_atm_strike(underlying, index_ltp)

    # Strong signal (>80) → ATM, moderate (65-80) → 1 OTM (cheaper, more upside)
    if score >= 80:
        strike = atm
    else:
        gap = cfg["strike_gap"]
        strike = atm + gap if direction == "long" else atm - gap  # OTM

    option_type = "CE" if direction == "long" else "PE"
    result = find_option(underlying, strike, option_type, expiry)
    if not result:
        # Fallback to ATM if OTM not found
        result = find_option(underlying, atm, option_type, expiry)
    if not result:
        logger.warning(f"Option contract not found: {underlying} {strike} {option_type} {expiry}")
        return None

    tradingsymbol, token, lot_size = result

    # Get option premium
    opt_ltp_data = get_ltp([f"NFO:{tradingsymbol}"])
    premium = opt_ltp_data.get(f"NFO:{tradingsymbol}")
    if not premium or premium <= 0:
        logger.warning(f"Could not get premium for {tradingsymbol}")
        return None

    # Calculate lots: risk max 20% of capital per trade
    max_per_trade = KITE_CAPITAL_INR * KITE_MAX_POSITION_PCT
    lots = max(1, int(max_per_trade / (premium * lot_size)))
    quantity = lots * lot_size

    return tradingsymbol, token, lot_size, premium, quantity


def calculate_premium_levels(premium: float):
    """
    Returns (tp_premium, sl_premium) based on entry premium.
    TP: +50% on premium, SL: -30% on premium.
    """
    tp = round(premium * (1 + KITE_PREMIUM_TP_PCT), 2)
    sl = round(premium * (1 - KITE_PREMIUM_SL_PCT), 2)
    return tp, sl
