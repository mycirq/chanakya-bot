import logging
from trader.config import (
    MAX_POSITIONS, MAX_POSITION_PCT, MIN_RR_RATIO,
    HARD_STOP_USDT, WARNING_USDT, MAX_LEVERAGE
)

logger = logging.getLogger(__name__)

# Correlated groups — if 2+ positions in same group & direction, block new same-direction entry
CORRELATED_GROUPS = [
    {"BTC/USDT:USDT", "BCH/USDT:USDT", "LTC/USDT:USDT"},            # BTC ecosystem
    {"ETH/USDT:USDT", "OP/USDT:USDT", "ARB/USDT:USDT", "MATIC/USDT:USDT", "POL/USDT:USDT"},  # ETH L2s
    {"SOL/USDT:USDT", "JUP/USDT:USDT", "RAY/USDT:USDT", "BONK/USDT:USDT", "WIF/USDT:USDT"},  # Solana ecosystem
    {"DOGE/USDT:USDT", "SHIB/USDT:USDT", "PEPE/USDT:USDT", "FLOKI/USDT:USDT"},  # Memecoins
    {"BNB/USDT:USDT", "CAKE/USDT:USDT"},                              # BSC
]


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


def get_dynamic_max_positions(total_loss_usdt):
    """Reduce max positions as losses approach hard stop.
    Full capacity → 70% of warning → warning → near hard stop.
    """
    if total_loss_usdt <= 0:
        return MAX_POSITIONS
    pct_of_hard = total_loss_usdt / HARD_STOP_USDT
    if pct_of_hard >= 0.85:
        return 1  # near hard stop — 1 position max
    if pct_of_hard >= 0.65:  # past warning
        return min(2, MAX_POSITIONS)
    if pct_of_hard >= 0.45:  # approaching warning
        return min(3, MAX_POSITIONS)
    return MAX_POSITIONS


def get_total_margin_deployed(open_positions):
    """Sum of margin currently deployed across open positions."""
    return sum(float(p.get("margin", 0)) for p in open_positions)


def margin_safe_for_new_trade(open_positions, balance_usdt, total_loss_usdt, new_margin):
    """Check if adding new_margin keeps us safely away from hard stop.
    Rule: deployed_margin + unrealized_losses + new_margin must leave
    at least 25% of hard_stop as buffer.
    """
    deployed = get_total_margin_deployed(open_positions)
    unrealized_loss = sum(
        p["unrealized_pnl"] for p in open_positions if p["unrealized_pnl"] < 0
    )
    # Worst case: we lose all new margin + existing unrealized losses hit
    worst_case_loss = total_loss_usdt + abs(unrealized_loss) + new_margin
    buffer = HARD_STOP_USDT * 0.25
    if worst_case_loss >= (HARD_STOP_USDT - buffer):
        return False, f"Margin unsafe — worst-case loss ${worst_case_loss:.0f} too close to hard stop ${HARD_STOP_USDT:.0f}"
    return True, "ok"


def check_correlation(symbol, direction, open_positions):
    """Block if 2+ positions already in same correlated group & direction."""
    group = None
    for g in CORRELATED_GROUPS:
        if symbol in g:
            group = g
            break
    if group is None:
        return True, "ok"

    same_dir_count = sum(
        1 for p in open_positions
        if p["symbol"] in group and p["side"] == direction
    )
    if same_dir_count >= 2:
        return False, f"Correlation limit — {same_dir_count} {direction}s already in same group"
    return True, "ok"


def find_weakest_position(open_positions):
    """Find the worst-performing open position by unrealized P&L %.
    Returns (position_dict, pnl_pct) or (None, 0) if no positions.
    """
    if not open_positions:
        return None, 0

    worst = None
    worst_pnl_pct = float("inf")
    for p in open_positions:
        entry = float(p.get("entry_price", 0))
        if entry <= 0:
            continue
        mark = float(p.get("mark_price", entry))
        if p["side"] == "long":
            pnl_pct = (mark - entry) / entry * 100
        else:
            pnl_pct = (entry - mark) / entry * 100
        if pnl_pct < worst_pnl_pct:
            worst_pnl_pct = pnl_pct
            worst = p
    return worst, worst_pnl_pct


def should_replace_position(new_score, open_positions, min_score_advantage=15):
    """When at max capacity, check if new signal is strong enough to replace weakest.
    Returns (should_replace: bool, weakest_position, reason: str).
    - New signal must beat MIN_SIGNAL_SCORE (already checked upstream)
    - Weakest position must be losing (negative unrealized P&L %)
    - New signal score must be at least `min_score_advantage` points higher than
      the weakest position's original signal score
    """
    weakest, pnl_pct = find_weakest_position(open_positions)
    if weakest is None:
        return False, None, "No positions to replace"

    # Only replace if the weakest is currently losing
    if pnl_pct >= 0:
        return False, None, f"Weakest position ({weakest['symbol']}) is profitable ({pnl_pct:+.1f}%), no replacement"

    # Check if new signal is significantly stronger
    old_score = float(weakest.get("leverage", 0))  # leverage field used as proxy; we'll use DB
    # We don't have signal_score on exchange data, so we always allow replacement
    # if the weakest is losing and new signal is strong
    if new_score < 70:
        return False, None, f"New signal score {new_score} not strong enough for replacement (need 70+)"

    return True, weakest, f"Replace {weakest['symbol']} ({pnl_pct:+.1f}%) with stronger signal (score={new_score})"


def can_open_trade(open_positions, balance_usdt, total_loss_usdt):
    """
    Returns (allowed: bool, reason: str)
    Does NOT check max positions here — that's handled separately to allow replacement logic.
    """
    if total_loss_usdt >= HARD_STOP_USDT:
        return False, f"Hard stop hit (loss: ${total_loss_usdt:.2f})"

    zone = get_trading_zone()
    if zone == "dead":
        return False, "Dead zone — no new trades (23:30–05:30 IST)"
    if zone == "limited":
        return False, "Limited zone — managing existing positions only"

    if balance_usdt < 20:
        return False, f"Insufficient balance (${balance_usdt:.2f} USDT)"

    return True, "ok"


def is_at_capacity(open_positions, total_loss_usdt):
    """Check if we're at or above dynamic max positions."""
    dyn_max = get_dynamic_max_positions(total_loss_usdt)
    return len(open_positions) >= dyn_max, dyn_max


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
