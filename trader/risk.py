import logging
from trader.config import (
    MAX_POSITIONS, MAX_POSITION_PCT, MIN_RR_RATIO,
    HARD_STOP_USDT, WARNING_USDT, MAX_LEVERAGE
)

logger = logging.getLogger(__name__)


def get_trading_zone():
    """Returns 'high', 'limited', or 'dead' based on current IST time."""
    from datetime import datetime
    from trader.config import IST, ZONES
    now = datetime.now(IST)
    h, m = now.hour, now.minute
    minutes = h * 60 + m

    def in_range(start_h, start_m, end_h, end_m):
        s = start_h * 60 + start_m
        e = end_h * 60 + end_m
        if s <= e:
            return s <= minutes < e
        else:  # wraps midnight
            return minutes >= s or minutes < e

    for start_h, start_m, end_h, end_m in ZONES.get("high", []):
        if in_range(start_h, start_m, end_h, end_m):
            return "high"
    for start_h, start_m, end_h, end_m in ZONES.get("limited", []):
        if in_range(start_h, start_m, end_h, end_m):
            return "limited"
    return "dead"


def can_open_trade(open_positions, balance_usdt, total_loss_usdt):
    """
    Returns (allowed: bool, reason: str)
    """
    if total_loss_usdt >= HARD_STOP_USDT:
        return False, f"Hard stop hit (loss: ${total_loss_usdt:.2f})"

    zone = get_trading_zone()
    if zone == "dead":
        return False, "Dead zone — no new trades (23:30–05:30 IST)"
    if zone == "limited":
        return False, "Limited zone — managing existing positions only"

    if len(open_positions) >= MAX_POSITIONS:
        return False, f"Max positions ({MAX_POSITIONS}) reached"

    if balance_usdt < 20:
        return False, f"Insufficient balance (${balance_usdt:.2f} USDT)"

    return True, "ok"


def size_position(balance_usdt, sl_pct):
    """
    Calculate position margin in USDT.
    Risk 1.5% of balance per trade, adjusted for SL distance.
    sl_pct: stop loss as percentage distance from entry (e.g. 0.015 = 1.5%)
    """
    risk_per_trade = balance_usdt * 0.015  # risk 1.5% of wallet
    if sl_pct <= 0:
        return 0

    # margin = risk / (sl_pct * leverage)
    margin = risk_per_trade / (sl_pct * MAX_LEVERAGE)
    # cap at MAX_POSITION_PCT of balance
    max_margin = balance_usdt * MAX_POSITION_PCT
    margin = min(margin, max_margin)
    margin = max(margin, 5.0)  # Binance min ~5 USDT margin
    return round(margin, 2)


def check_drawdown_alert(total_loss_usdt):
    """Returns 'warning', 'hard_stop', or None."""
    if total_loss_usdt >= HARD_STOP_USDT:
        return "hard_stop"
    if total_loss_usdt >= WARNING_USDT:
        return "warning"
    return None


def validate_rr(entry, tp, sl, direction):
    """Validates risk:reward ratio meets minimum."""
    if direction == "long":
        reward = tp - entry
        risk   = entry - sl
    else:
        reward = entry - tp
        risk   = sl - entry
    if risk <= 0:
        return False, 0
    rr = reward / risk
    return rr >= MIN_RR_RATIO, round(rr, 2)
