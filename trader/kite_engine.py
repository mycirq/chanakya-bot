"""
Kite FnO trading engine — scan → signal → select contract → execute → report.
Runs only during NSE market hours (9:15–15:25 IST, Mon–Fri).
"""
import logging
from datetime import datetime

from trader.kite import (
    get_kite, is_authorized, is_market_open,
    get_open_positions, exit_position
)
from trader.kite_strategy import (
    get_index_signal, select_option_contract, calculate_premium_levels
)
from trader.kite_reporter import (
    post_kite_thesis, post_kite_opened, post_kite_closed,
    post_kite_drawdown_warning, post_kite_hard_stop, post_kite_daily_summary,
    post_kite_scan_result
)
from trader.config import (
    KITE_INDICES, KITE_MIN_SIGNAL_SCORE, KITE_HARD_STOP_INR,
    KITE_WARNING_INR, KITE_MAX_POSITIONS, KITE_CAPITAL_INR,
    KITE_MONTHLY_TARGET_PCT
)
from db import get_conn

logger = logging.getLogger(__name__)

_kite_paused = False


def is_kite_paused():   return _kite_paused
def pause_kite():
    global _kite_paused; _kite_paused = True;  logger.info("Kite trading PAUSED")
def resume_kite():
    global _kite_paused; _kite_paused = False; logger.info("Kite trading RESUMED")


# ── P&L helpers ────────────────────────────────────────────────────────────────

def get_kite_total_loss_inr() -> float:
    """Sum of all realized losses + current unrealized losses from open positions."""
    try:
        open_pos   = get_open_positions()
        unrealized = sum(p["pnl"] for p in open_pos if p["pnl"] < 0)
        conn = get_conn()
        if hasattr(conn, "cursor"):
            from psycopg2.extras import RealDictCursor
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute(
                "SELECT COALESCE(SUM(pnl_inr), 0) as total FROM kite_memory WHERE outcome='loss'"
            )
            realized = float(cur.fetchone()["total"])
            cur.close()
        else:
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl_inr), 0) as total FROM kite_memory WHERE outcome='loss'"
            ).fetchone()
            realized = float(row["total"]) if row else 0
        conn.close()
        return abs(min(realized + unrealized, 0))
    except Exception as e:
        logger.error(f"get_kite_total_loss_inr failed: {e}")
        return 0.0


def get_kite_capital() -> float:
    """Returns live available margin from Kite equity (FnO) segment."""
    try:
        margins = get_kite().margins()
        return float(margins["equity"]["net"])
    except Exception as e:
        logger.error(f"get_kite_capital failed: {e}")
        return KITE_CAPITAL_INR


# ── Save/close helpers ─────────────────────────────────────────────────────────

def _save_kite_position(underlying, direction, tradingsymbol, expiry,
                         option_type, entry_premium, tp, sl,
                         quantity, lot_size, score, reason):
    from db import get_conn
    strike = tradingsymbol  # full symbol encodes strike
    conn = get_conn()
    try:
        if hasattr(conn, "cursor"):
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO kite_positions
                (underlying, direction, tradingsymbol, option_type, entry_premium,
                 tp_premium, sl_premium, quantity, lot_size, signal_score, signal_reason)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """, (underlying, direction, tradingsymbol, option_type, entry_premium,
                  tp, sl, quantity, lot_size, score, reason))
            pid = cur.fetchone()[0]
            conn.commit(); cur.close()
        else:
            cur = conn.execute("""
                INSERT INTO kite_positions
                (underlying, direction, tradingsymbol, option_type, entry_premium,
                 tp_premium, sl_premium, quantity, lot_size, signal_score, signal_reason)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (underlying, direction, tradingsymbol, option_type, entry_premium,
                  tp, sl, quantity, lot_size, score, reason))
            pid = cur.lastrowid
            conn.commit()
        return pid
    finally:
        conn.close()


def _close_kite_position_record(position_id, close_premium, pnl_inr, close_reason):
    from db import get_conn
    conn = get_conn()
    try:
        if hasattr(conn, "cursor"):
            cur = conn.cursor()
            cur.execute("""
                UPDATE kite_positions
                SET status='closed', closed_at=NOW(), close_premium=%s,
                    pnl_inr=%s, close_reason=%s
                WHERE id=%s
            """, (close_premium, pnl_inr, close_reason, position_id))
            conn.commit(); cur.close()
        else:
            conn.execute("""
                UPDATE kite_positions
                SET status='closed', closed_at=datetime('now'), close_premium=?,
                    pnl_inr=?, close_reason=?
                WHERE id=?
            """, (close_premium, pnl_inr, close_reason, position_id))
            conn.commit()
    finally:
        conn.close()


def _record_kite_memory(underlying, direction, tradingsymbol, entry_premium,
                         close_premium, pnl_inr, quantity, score, reason, duration_min):
    from db import get_conn
    pnl_pct = (close_premium - entry_premium) / entry_premium * 100 if entry_premium else 0
    outcome = "win" if pnl_inr > 0 else "loss"
    conn = get_conn()
    try:
        if hasattr(conn, "cursor"):
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO kite_memory
                (underlying, direction, tradingsymbol, entry_premium, close_premium,
                 pnl_inr, pnl_pct, quantity, signal_score, signal_reason,
                 duration_minutes, outcome)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (underlying, direction, tradingsymbol, entry_premium, close_premium,
                  pnl_inr, pnl_pct, quantity, score, reason, duration_min, outcome))
            conn.commit(); cur.close()
        else:
            conn.execute("""
                INSERT INTO kite_memory
                (underlying, direction, tradingsymbol, entry_premium, close_premium,
                 pnl_inr, pnl_pct, quantity, signal_score, signal_reason,
                 duration_minutes, outcome)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (underlying, direction, tradingsymbol, entry_premium, close_premium,
                  pnl_inr, pnl_pct, quantity, score, reason, duration_min, outcome))
            conn.commit()
    finally:
        conn.close()


def _get_kite_open_positions_db():
    from db import get_conn
    conn = get_conn()
    try:
        if hasattr(conn, "cursor"):
            from psycopg2.extras import RealDictCursor
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT * FROM kite_positions WHERE status='open'")
            rows = [dict(r) for r in cur.fetchall()]
            cur.close()
        else:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM kite_positions WHERE status='open'"
            ).fetchall()]
        return rows
    finally:
        conn.close()


def _get_kite_trade_stats():
    from db import get_conn
    conn = get_conn()
    try:
        if hasattr(conn, "cursor"):
            from psycopg2.extras import RealDictCursor
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("""
                SELECT outcome, COUNT(*) as cnt, AVG(pnl_inr) as avg_pnl,
                       SUM(pnl_inr) as total_pnl
                FROM kite_memory GROUP BY outcome
            """)
            rows = [dict(r) for r in cur.fetchall()]; cur.close()
        else:
            rows = [dict(r) for r in conn.execute("""
                SELECT outcome, COUNT(*) as cnt, AVG(pnl_inr) as avg_pnl,
                       SUM(pnl_inr) as total_pnl
                FROM kite_memory GROUP BY outcome
            """).fetchall()]
        return rows
    finally:
        conn.close()


# ── Main scan ──────────────────────────────────────────────────────────────────

def run_kite_scan(app):
    """Main scan — called every 5 minutes during market hours."""
    global _kite_paused

    if _kite_paused:
        logger.info("Kite paused — skipping scan")
        return

    if not is_market_open():
        return

    if not is_authorized():
        logger.info("Kite not authorized — skipping scan (send /kite-auth <totp>)")
        return

    # Drawdown check
    total_loss = get_kite_total_loss_inr()
    if total_loss >= KITE_HARD_STOP_INR:
        _kite_paused = True
        _close_all_kite_positions(app)
        post_kite_hard_stop(app.client, total_loss)
        logger.warning(f"Kite HARD STOP — loss ₹{total_loss:,.0f}")
        return
    elif total_loss >= KITE_WARNING_INR:
        post_kite_drawdown_warning(app.client, total_loss)

    # Sync closed positions
    _sync_kite_positions(app)

    # Check open position count
    db_open = _get_kite_open_positions_db()
    if len(db_open) >= KITE_MAX_POSITIONS:
        logger.info(f"Kite max positions ({KITE_MAX_POSITIONS}) reached")
        return

    # Score ALL indices — always report
    all_scores = []
    for underlying in KITE_INDICES:
        score, direction, reason = get_index_signal(underlying)
        all_scores.append((underlying, score, direction, reason))

    post_kite_scan_result(app.client, all_scores, KITE_MIN_SIGNAL_SCORE)

    candidates = [(s, u, d, r) for u, s, d, r in all_scores if s >= KITE_MIN_SIGNAL_SCORE and d]
    if not candidates:
        logger.info("No Kite signals above threshold")
        return

    candidates.sort(key=lambda x: x[0], reverse=True)
    score, underlying, direction, reason = candidates[0]

    # Skip if already in this underlying
    open_symbols = {p["underlying"] for p in db_open}
    if underlying in open_symbols:
        logger.info(f"Already in {underlying}, skipping")
        return

    logger.info(f"Kite signal: {underlying} {direction} score={score}")

    # Select contract
    result = select_option_contract(underlying, direction, score)
    if not result:
        return
    tradingsymbol, token, lot_size, premium, quantity = result

    option_type = "CE" if direction == "long" else "PE"
    tp, sl = calculate_premium_levels(premium)
    expiry = str(_get_expiry_from_symbol(tradingsymbol))

    # Post thesis first (even if order fails, user sees the signal)
    post_kite_thesis(app.client, underlying, direction, tradingsymbol,
                     premium, tp, sl, quantity, lot_size, score, reason)

    # Place order
    order_id = _place_kite_order(tradingsymbol, quantity)
    if not order_id:
        logger.warning(f"Kite order FAILED for {tradingsymbol} — not saving position")
        return

    # Save to DB only on successful order
    pos_id = _save_kite_position(
        underlying, direction, tradingsymbol, expiry,
        option_type, premium, tp, sl, quantity, lot_size, score, reason
    )

    post_kite_opened(app.client, underlying, direction, tradingsymbol,
                     premium, tp, sl, quantity, lot_size, score, reason)
    logger.info(f"Kite trade opened: {tradingsymbol} pos_id={pos_id}")


def _place_kite_order(tradingsymbol, quantity):
    from trader.kite import place_option_order
    return place_option_order(tradingsymbol, "BUY", quantity)


def _get_expiry_from_symbol(tradingsymbol):
    """Best-effort expiry extraction from symbol name."""
    from trader.kite import get_nfo_instruments
    for inst in get_nfo_instruments():
        if inst.get("tradingsymbol") == tradingsymbol:
            return inst.get("expiry")
    return ""


# ── Position sync ──────────────────────────────────────────────────────────────

def _sync_kite_positions(app):
    """Detect TP/SL hits and positions closed by MIS auto-square-off."""
    db_positions = _get_kite_open_positions_db()
    if not db_positions:
        return

    exchange_open = {p["tradingsymbol"] for p in get_open_positions()}

    for pos in db_positions:
        sym = pos["tradingsymbol"]

        # Check TP/SL via current LTP
        from trader.kite import get_ltp
        ltp_data = get_ltp([f"NFO:{sym}"])
        current_premium = ltp_data.get(f"NFO:{sym}")

        close_reason = None
        if sym not in exchange_open:
            close_reason = "squared_off"
        elif current_premium:
            if current_premium >= float(pos["tp_premium"]):
                close_reason = "tp_hit"
                exit_position(sym, int(pos["quantity"]))
            elif current_premium <= float(pos["sl_premium"]):
                close_reason = "sl_hit"
                exit_position(sym, int(pos["quantity"]))

        if not close_reason:
            continue

        close_premium = current_premium or float(pos["entry_premium"])
        pnl_inr = (close_premium - float(pos["entry_premium"])) * int(pos["quantity"])

        opened_at = pos.get("opened_at")
        duration  = 0
        if opened_at:
            try:
                if isinstance(opened_at, str):
                    opened_at = datetime.fromisoformat(opened_at)
                duration = int((datetime.now() - opened_at.replace(tzinfo=None)).total_seconds() / 60)
            except Exception:
                pass

        _close_kite_position_record(pos["id"], close_premium, pnl_inr, close_reason)
        _record_kite_memory(
            pos["underlying"], pos["direction"], sym,
            float(pos["entry_premium"]), close_premium, pnl_inr,
            int(pos["quantity"]), pos.get("signal_score", 0),
            pos.get("signal_reason", ""), duration
        )
        post_kite_closed(app.client, pos["underlying"], sym, pos["direction"],
                         float(pos["entry_premium"]), close_premium,
                         pnl_inr, close_reason, duration)
        logger.info(f"Kite position closed: {sym} | P&L ₹{pnl_inr:+,.0f}")


def _close_all_kite_positions(app):
    for p in get_open_positions():
        exit_position(p["tradingsymbol"], p["quantity"])
        logger.warning(f"Emergency closed: {p['tradingsymbol']}")


# ── Daily summary ──────────────────────────────────────────────────────────────

def run_kite_daily_summary(app):
    stats   = _get_kite_trade_stats()
    capital = get_kite_capital()
    open_count = len(get_open_positions())
    post_kite_daily_summary(app.client, stats, capital, open_count)
