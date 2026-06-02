"""
Main trading engine — orchestrates scan → signal → risk → execute → report.
"""
import logging
from datetime import datetime

from trader.binance import (
    get_futures_balance, get_open_positions, get_top_futures_pairs,
    fetch_ohlcv, place_order, close_position, cancel_open_orders,
    get_realized_pnl_since
)
from trader.strategy import compute_indicators, score_signal, calculate_tp_sl
from trader.risk import (
    can_open_trade, size_position, check_drawdown_alert, get_trading_zone,
    is_at_capacity, should_replace_position, margin_safe_for_new_trade,
    check_correlation, get_dynamic_max_positions, find_weakest_position
)
from trader.memory import (
    save_position, close_position_record, record_memory,
    get_open_positions_db, get_recent_memory, get_trade_stats
)
from trader.reporter import (
    post_pre_trade_thesis, post_trade_opened, post_trade_closed,
    post_drawdown_warning, post_hard_stop, post_daily_summary,
    post_crypto_scan_result, post_position_replaced
)
from trader.config import MIN_SIGNAL_SCORE, MAX_LEVERAGE, TOP_PAIRS_COUNT, CAPITAL_USDT
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
    """Current drawdown from this month's starting balance (0 if at/above it).

    This is the figure the hard-stop and margin guard use, so it must reflect real
    equity drawdown — NOT a running sum of lifetime losing trades. Summing all-time
    losses ignored wins, grew without bound, and throttled a profitable account
    (it read $224 while the wallet was up +$136). Drawdown-from-baseline self-heals:
    when the account is at or above its month-start balance, drawdown is 0.

    Falls back to configured starting capital if no month snapshot exists, and to 0
    if the balance can't be fetched (an API blip must not trigger a false freeze).
    """
    try:
        balance = get_futures_balance()
        if not balance or balance <= 0:
            return 0.0
        from trader.memory import get_month_snapshot
        snap = get_month_snapshot()
        baseline = float(snap["start_balance"]) if snap and snap.get("start_balance") else CAPITAL_USDT
        if baseline <= 0:
            baseline = CAPITAL_USDT
        return max(0.0, baseline - balance)
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
        # Volatility guard — skip pairs where ATR(14)/price > 4%. These move so
        # fast that SL placement loses the race vs price action (e.g. ESPORTS).
        if df is not None and len(df) > 0:
            last = df.iloc[-1]
            atr_pct = (float(last["high"]) - float(last["low"])) / float(last["close"])
            if atr_pct > 0.04:
                logger.info(f"Skipping {symbol}: volatility {atr_pct*100:.1f}% > 4%")
                continue
        score, direction, sig_reason = score_signal(df)
        all_scores.append((score, symbol, direction, sig_reason))
        if df is not None:
            dfs[symbol] = df

    if not allowed:
        logger.info(f"Cannot open trade: {allow_reason}")
        post_crypto_scan_result(app.client, all_scores, MIN_SIGNAL_SCORE, zone, skip_reason=allow_reason)
        return

    at_cap, dyn_max = is_at_capacity(open_pos, total_loss)
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

    # ── At capacity? Try replacement ──────────────────────────────────
    replacing = False
    replaced_pos = None
    if at_cap:
        do_replace, weakest, replace_reason = should_replace_position(score, open_pos)
        if not do_replace:
            logger.info(f"At capacity ({len(open_pos)}/{dyn_max}) — {replace_reason}")
            return
        logger.info(f"Position replacement: {replace_reason}")
        replacing = True
        replaced_pos = weakest

    # ── Correlation check ─────────────────────────────────────────────
    corr_ok, corr_reason = check_correlation(symbol, direction, open_pos)
    if not corr_ok:
        logger.info(f"Correlation blocked: {corr_reason}")
        return

    # Re-check we can still open (race condition guard)
    open_pos = get_open_positions()
    allowed, block_reason = can_open_trade(open_pos, balance, total_loss)
    if not allowed:
        return

    # Get entry price
    ticker = fetch_ohlcv(symbol, "1m", 2)
    if not ticker:
        return
    entry_price = float(ticker[-1][4])  # last close

    # Calculate TP / SL
    tp_price, sl_price, rr = calculate_tp_sl(entry_price, direction, df, MAX_LEVERAGE)
    if tp_price is None or sl_price is None:
        logger.info(f"TP/SL inverted for {symbol}, skipping")
        return
    if rr < 1.5:
        logger.info(f"RR {rr} below minimum for {symbol}, skipping")
        return

    # Size position
    sl_pct = abs(entry_price - sl_price) / entry_price
    margin = size_position(balance, sl_pct)
    if margin < 5:
        logger.info(f"Margin too small: ${margin:.2f}, skipping")
        return

    # ── Margin safety check ───────────────────────────────────────────
    margin_ok, margin_reason = margin_safe_for_new_trade(open_pos, balance, total_loss, margin)
    if not margin_ok:
        logger.info(f"Margin guard blocked: {margin_reason}")
        return

    # ── Execute replacement: close weakest first ──────────────────────
    if replacing and replaced_pos:
        old_sym = replaced_pos["symbol"]
        _, old_pnl_pct = find_weakest_position(open_pos)
        close_position(old_sym, replaced_pos["side"])
        cancel_open_orders(old_sym)
        post_position_replaced(
            app.client, old_sym, old_pnl_pct,
            symbol, direction, score, "Stronger signal replaced weaker position"
        )
        logger.info(f"Replaced {old_sym} ({old_pnl_pct:+.1f}%) for {symbol}")
        # Sync the closed position in DB
        _sync_replaced_position(app, replaced_pos)

    # Post thesis BEFORE executing
    post_pre_trade_thesis(
        app.client, symbol, direction, entry_price,
        tp_price, sl_price, margin, MAX_LEVERAGE, score, sig_reason, rr
    )

    # Place order (atomic: entry + SL both confirmed, or fully rolled back)
    result = place_order(symbol, direction, margin, entry_price,
                         tp_price, sl_price, MAX_LEVERAGE)
    if not result:
        return

    # Prefer Binance's actual liq price; fall back to rough estimate if unavailable
    liq_price = result.get("real_liq_price") or (
        entry_price * 0.80 if direction == "long" else entry_price * 1.20
    )

    # Save to DB. If this throws, the position is on Binance with SL set — safe
    # but invisible to the bot. Market-close to keep DB and exchange in sync.
    try:
        pos_id = save_position(
            symbol=symbol, direction=direction,
            entry_price=entry_price, tp_price=tp_price,
            sl_price=sl_price, liq_price=liq_price,
            margin_usdt=margin, leverage=MAX_LEVERAGE,
            size=margin * MAX_LEVERAGE / entry_price,
            signal_score=score, signal_reason=sig_reason
        )
    except Exception as e:
        logger.error(f"save_position failed for {symbol}: {type(e).__name__}: {e} — market-closing to avoid orphan")
        from trader.binance import _rollback_position
        _rollback_position(symbol, direction)
        return

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

    # strict=True → None on API/proxy failure, [] only on a genuinely-flat account.
    # This lets us reconcile a real close without falsely closing on a fetch error.
    exchange_positions = get_open_positions(strict=True)
    if exchange_positions is None:
        logger.warning(f"Exchange positions fetch failed — skipping sync ({len(db_positions)} in DB, will retry next scan)")
        return

    exchange_open = {p["symbol"] for p in exchange_positions}

    for pos in db_positions:
        symbol = pos["symbol"]
        if symbol not in exchange_open:
            # Position closed on exchange (TP or SL hit).
            entry_price = float(pos["entry_price"])

            opened_at = pos.get("opened_at")
            duration = 0
            opened_dt = None
            if opened_at:
                try:
                    if isinstance(opened_at, str):
                        opened_at = datetime.fromisoformat(opened_at)
                    opened_dt = opened_at.replace(tzinfo=None)
                    duration = int((datetime.now() - opened_dt).total_seconds() / 60)
                except Exception:
                    duration = 0

            # Use Binance's authoritative realized P&L for this position rather than
            # estimating from a candle (which fabricated huge phantom losses before).
            since_ms = int((opened_dt.timestamp() - 300) * 1000) if opened_dt else 0
            pnl_usdt = get_realized_pnl_since(symbol, since_ms) if since_ms else None
            if pnl_usdt is None:
                logger.warning(f"Realized P&L unavailable for {symbol} — leaving open, will retry next scan")
                continue

            # Close price is for display only; best-effort from the latest candle.
            ohlcv = fetch_ohlcv(symbol, "1m", 2)
            close_price = float(ohlcv[-1][4]) if ohlcv else entry_price

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


def _sync_replaced_position(app, exchange_pos):
    """Close the replaced position's DB record immediately."""
    db_positions = get_open_positions_db()
    symbol = exchange_pos["symbol"]
    for pos in db_positions:
        if pos["symbol"] == symbol:
            entry_price = float(pos["entry_price"])
            mark_price = float(exchange_pos.get("mark_price", entry_price))
            pnl_usdt = (mark_price - entry_price) * float(pos["size"])
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
                    pass

            close_position_record(pos["id"], mark_price, pnl_usdt, "replaced")
            record_memory(
                symbol=symbol, direction=pos["direction"],
                entry_price=entry_price, close_price=mark_price,
                pnl_usdt=pnl_usdt, signal_score=pos.get("signal_score", 0),
                signal_reason=pos.get("signal_reason", ""),
                zone=get_trading_zone(), duration_minutes=duration
            )
            logger.info(f"Replaced position synced: {symbol} | PnL: {pnl_usdt:+.2f} USDT")
            break


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
