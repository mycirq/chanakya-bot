"""
Main trading engine — orchestrates scan → signal → risk → execute → report.
"""
import logging
from datetime import datetime

from trader.binance import (
    get_futures_balance, get_open_positions, get_top_futures_pairs,
    fetch_ohlcv, place_order, close_position, cancel_open_orders
)
from trader.strategy import compute_indicators, score_signal, calculate_tp_sl
from trader.risk import can_open_trade, size_position, check_drawdown_alert, get_trading_zone
from trader.memory import (
    save_position, close_position_record, record_memory,
    get_open_positions_db, get_recent_memory, get_trade_stats
)
from trader.reporter import (
    post_pre_trade_thesis, post_trade_opened, post_trade_closed,
    post_drawdown_warning, post_hard_stop, post_daily_summary,
    post_crypto_scan_result
)
from trader.config import MIN_SIGNAL_SCORE, MAX_LEVERAGE, TOP_PAIRS_COUNT
from db import get_conn

logger = logging.getLogger(__name__)

_paused = False


def is_paused():
    return _paused


def pause_trading():
    global _paused
    _paused = True
    logger.info("Trading PAUSED")


def resume_trading():
    global _paused
    _paused = False
    logger.info("Trading RESUMED")


def get_total_loss_usdt():
    """Sum of all realized losses + current unrealized losses."""
    try:
        open_pos = get_open_positions()
        unrealized = sum(p["unrealized_pnl"] for p in open_pos if p["unrealized_pnl"] < 0)
        conn = get_conn()
        if hasattr(conn, 'cursor'):
            from psycopg2.extras import RealDictCursor
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT COALESCE(SUM(pnl_usdt), 0) as total FROM trade_memory WHERE outcome='loss'")
            realized = float(cur.fetchone()["total"])
            cur.close()
        else:
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl_usdt), 0) as total FROM trade_memory WHERE outcome='loss'"
            ).fetchone()
            realized = float(row["total"]) if row else 0
        conn.close()
        return abs(min(realized + unrealized, 0))
    except Exception as e:
        logger.error(f"get_total_loss_usdt failed: {e}")
        return 0.0


def run_scan(app):
    """Main scan loop — called every 15 minutes by scheduler."""
    global _paused

    if _paused:
        logger.info("Trading paused — skipping scan")
        return

    zone = get_trading_zone()
    logger.info(f"Running scan | zone: {zone}")

    # Check drawdown
    total_loss = get_total_loss_usdt()
    alert = check_drawdown_alert(total_loss)
    if alert == "hard_stop":
        _paused = True
        _close_all_positions(app)
        post_hard_stop(app.client, total_loss)
        logger.warning(f"HARD STOP triggered — loss: ${total_loss:.2f}")
        return
    elif alert == "warning":
        post_drawdown_warning(app.client, total_loss)

    # Check monitor open positions (update DB from exchange)
    _sync_open_positions(app)

    if zone in ("limited", "dead"):
        logger.info(f"Zone {zone} — not opening new positions")
        return

    # Get state
    balance    = get_futures_balance()
    open_pos   = get_open_positions()
    db_pos     = get_open_positions_db()
    open_syms  = {p["symbol"] for p in open_pos}

    allowed, allow_reason = can_open_trade(open_pos, balance, total_loss)

    # Scan top pairs — always score all, report results
    pairs = get_top_futures_pairs(TOP_PAIRS_COUNT)
    all_scores = []
    dfs = {}

    for symbol in pairs:
        if symbol in open_syms:
            continue
        ohlcv = fetch_ohlcv(symbol, "1h", 220)
        df    = compute_indicators(ohlcv)
        score, direction, sig_reason = score_signal(df)
        all_scores.append((score, symbol, direction, sig_reason))
        if df is not None:
            dfs[symbol] = df

    if not allowed:
        logger.info(f"Cannot open trade: {allow_reason}")
        post_crypto_scan_result(app.client, all_scores, MIN_SIGNAL_SCORE, zone, skip_reason=allow_reason)
        return

    post_crypto_scan_result(app.client, all_scores, MIN_SIGNAL_SCORE, zone)

    candidates = [
        (s, sym, d, r, dfs[sym]) for s, sym, d, r in all_scores
        if s >= MIN_SIGNAL_SCORE and d and sym in dfs
    ]

    if not candidates:
        logger.info("No signals above threshold this scan")
        return

    # Sort by score descending, take top signal
    candidates.sort(key=lambda x: x[0], reverse=True)
    score, symbol, direction, sig_reason, df = candidates[0]

    logger.info(f"Best signal: {symbol} {direction} score={score} — {sig_reason}")

    # Re-check we can still open (race condition guard)
    allowed, block_reason = can_open_trade(get_open_positions(), balance, total_loss)
    if not allowed:
        return

    # Get entry price
    ticker = fetch_ohlcv(symbol, "1m", 2)
    if not ticker:
        return
    entry_price = float(ticker[-1][4])  # last close

    # Calculate TP / SL
    tp_price, sl_price, rr = calculate_tp_sl(entry_price, direction, df)
    if rr < 1.5:
        logger.info(f"RR {rr} below minimum for {symbol}, skipping")
        return

    # Size position
    sl_pct = abs(entry_price - sl_price) / entry_price
    margin = size_position(balance, sl_pct)
    if margin < 5:
        logger.info(f"Margin too small: ${margin:.2f}, skipping")
        return

    # Post thesis BEFORE executing
    post_pre_trade_thesis(
        app.client, symbol, direction, entry_price,
        tp_price, sl_price, margin, MAX_LEVERAGE, score, sig_reason, rr
    )

    # Place order
    order = place_order(symbol, direction, margin, entry_price,
                        tp_price, sl_price, MAX_LEVERAGE)
    if not order:
        return

    # Estimate liquidation price (isolated, 5x)
    liq_buffer = entry_price * 0.20  # ~20% from entry at 5x isolated
    liq_price  = (entry_price - liq_buffer) if direction == "long" else (entry_price + liq_buffer)

    # Save to DB
    pos_id = save_position(
        symbol=symbol, direction=direction,
        entry_price=entry_price, tp_price=tp_price,
        sl_price=sl_price, liq_price=liq_price,
        margin_usdt=margin, leverage=MAX_LEVERAGE,
        size=margin * MAX_LEVERAGE / entry_price,
        signal_score=score, signal_reason=sig_reason
    )

    # Report to Slack
    post_trade_opened(
        app.client, symbol, direction, entry_price,
        tp_price, sl_price, liq_price, margin,
        MAX_LEVERAGE, score, sig_reason,
        abs(tp_price - entry_price) / entry_price * 100
    )
    logger.info(f"Trade opened: {symbol} {direction} @ {entry_price} | pos_id={pos_id}")


def _sync_open_positions(app):
    """Check if any DB-open positions have been closed on exchange (TP/SL hit)."""
    db_positions = get_open_positions_db()
    if not db_positions:
        return

    exchange_open = {p["symbol"] for p in get_open_positions()}

    for pos in db_positions:
        symbol = pos["symbol"]
        if symbol not in exchange_open:
            # Position closed on exchange (TP or SL hit)
            ohlcv = fetch_ohlcv(symbol, "1m", 2)
            close_price = float(ohlcv[-1][4]) if ohlcv else float(pos["entry_price"])
            entry_price = float(pos["entry_price"])

            pnl_usdt = (close_price - entry_price) * float(pos["size"])
            if pos["direction"] == "short":
                pnl_usdt = -pnl_usdt

            opened_at = pos.get("opened_at")
            duration = 0
            if opened_at:
                try:
                    if isinstance(opened_at, str):
                        opened_at = datetime.fromisoformat(opened_at)
                    duration = int((datetime.now() - opened_at.replace(tzinfo=None)).total_seconds() / 60)
                except Exception:
                    duration = 0

            close_reason = "tp_hit" if pnl_usdt > 0 else "sl_hit"

            close_position_record(pos["id"], close_price, pnl_usdt, close_reason)
            record_memory(
                symbol=symbol, direction=pos["direction"],
                entry_price=entry_price, close_price=close_price,
                pnl_usdt=pnl_usdt, signal_score=pos.get("signal_score", 0),
                signal_reason=pos.get("signal_reason", ""),
                zone=get_trading_zone(), duration_minutes=duration
            )
            post_trade_closed(
                app.client, symbol, pos["direction"],
                entry_price, close_price, pnl_usdt, close_reason, duration
            )
            logger.info(f"Position synced closed: {symbol} | PnL: {pnl_usdt:+.2f} USDT")


def _close_all_positions(app):
    """Emergency close all open positions."""
    positions = get_open_positions()
    for p in positions:
        symbol = p["symbol"]
        close_position(symbol, p["side"])
        cancel_open_orders(symbol)
        logger.warning(f"Emergency closed: {symbol}")


def run_daily_summary(app):
    """Posted at 9 PM IST."""
    stats   = get_trade_stats()
    balance = get_futures_balance()
    open_pos = get_open_positions()
    post_daily_summary(app.client, stats, balance, len(open_pos))
