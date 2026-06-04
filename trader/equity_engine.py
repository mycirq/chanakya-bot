"""
Kite Equity Intraday engine — market-hours scan → confirm → execute → manage.

Watchlist-ONLY: trades only the research-vetted names for today (equity_watchlist).
Half-size observation caps. Every entry carries an exchange-side SL-M stop. Hard
square-off at 3:15 PM IST (before Zerodha's 3:20 auto-square). Daily-loss circuit
halts the day. Fully independent of the crypto and Kite-FnO verticals.
"""
import os
import logging
from datetime import datetime

from trader.kite import get_kite, is_authorized
from trader.equity_kite import (
    get_equity_positions, get_equity_capital, place_equity_order,
    exit_equity_position, get_equity_ltp,
)
from trader.equity_signals import evaluate_equity
from trader.equity_research import get_watchlist
from trader.equity_reporter import (
    post_equity_thesis, post_equity_opened, post_equity_closed,
    post_equity_squareoff, post_equity_circuit, post_equity_daily_summary,
)
from trader.config import (
    IST, EQUITY_MAX_POSITIONS, EQUITY_DAILY_LOSS_INR, EQUITY_CAPITAL_INR,
    EQUITY_MARKET_OPEN, EQUITY_SQUAREOFF, EQUITY_NO_NEW_AFTER,
)
from db import get_conn

logger = logging.getLogger(__name__)

_equity_paused = False
_circuit_tripped_date = None   # date the daily circuit halted trading
_attempted = {}                # {date: set(symbols)} — one order attempt per name per day


def is_equity_paused(): return _equity_paused
def pause_equity():
    global _equity_paused; _equity_paused = True;  logger.info("Equity trading PAUSED")
def resume_equity():
    global _equity_paused; _equity_paused = False; logger.info("Equity trading RESUMED")


def _dry_run() -> bool:
    return os.environ.get("EQUITY_DRY_RUN", "").lower() in ("1", "true", "yes")


def _today():
    return datetime.now(IST).date()


def _mins_now():
    n = datetime.now(IST)
    return n.hour * 60 + n.minute


def _market_open_for_scan() -> bool:
    n = datetime.now(IST)
    if n.weekday() >= 5:
        return False
    o = EQUITY_MARKET_OPEN[0] * 60 + EQUITY_MARKET_OPEN[1]
    c = EQUITY_SQUAREOFF[0] * 60 + EQUITY_SQUAREOFF[1]
    return o <= _mins_now() <= c


# ── DB helpers ───────────────────────────────────────────────────────────────

def _save_position(plan, fill_price, entry_id, sl_id):
    conn = get_conn()
    try:
        if hasattr(conn, "cursor"):
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO equity_positions
                (symbol, direction, entry_price, sl_price, tp_price, quantity, risk_inr,
                 entry_order_id, sl_order_id, signal_score, signal_reason)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """, (plan["symbol"], plan["direction"], fill_price, plan["sl"], plan["tp"],
                  plan["quantity"], plan["risk_inr"], entry_id, sl_id,
                  plan["score"], plan["reason"]))
            pid = cur.fetchone()[0]; conn.commit(); cur.close()
        else:
            cur = conn.execute("""
                INSERT INTO equity_positions
                (symbol, direction, entry_price, sl_price, tp_price, quantity, risk_inr,
                 entry_order_id, sl_order_id, signal_score, signal_reason)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (plan["symbol"], plan["direction"], fill_price, plan["sl"], plan["tp"],
                  plan["quantity"], plan["risk_inr"], entry_id, sl_id,
                  plan["score"], plan["reason"]))
            pid = cur.lastrowid; conn.commit()
        return pid
    finally:
        conn.close()


def _close_record(pid, close_price, pnl_inr, reason):
    conn = get_conn()
    try:
        if hasattr(conn, "cursor"):
            cur = conn.cursor()
            cur.execute("""UPDATE equity_positions SET status='closed', closed_at=NOW(),
                           close_price=%s, pnl_inr=%s, close_reason=%s WHERE id=%s""",
                        (close_price, pnl_inr, reason, pid))
            conn.commit(); cur.close()
        else:
            conn.execute("""UPDATE equity_positions SET status='closed',
                            closed_at=datetime('now'), close_price=?, pnl_inr=?, close_reason=?
                            WHERE id=?""", (close_price, pnl_inr, reason, pid))
            conn.commit()
    finally:
        conn.close()


def _record_memory(pos, close_price, pnl_inr, duration):
    entry = float(pos["entry_price"])
    pnl_pct = (close_price - entry) / entry * 100 if entry else 0
    if pos["direction"] == "short":
        pnl_pct = -pnl_pct
    outcome = "win" if pnl_inr > 0 else "loss"
    conn = get_conn()
    try:
        if hasattr(conn, "cursor"):
            cur = conn.cursor()
            cur.execute("""INSERT INTO equity_memory
                (symbol, direction, entry_price, close_price, pnl_inr, pnl_pct, quantity,
                 signal_score, signal_reason, duration_minutes, outcome)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (pos["symbol"], pos["direction"], entry, close_price, pnl_inr, pnl_pct,
                 int(pos["quantity"]), pos.get("signal_score", 0),
                 pos.get("signal_reason", ""), duration, outcome))
            conn.commit(); cur.close()
        else:
            conn.execute("""INSERT INTO equity_memory
                (symbol, direction, entry_price, close_price, pnl_inr, pnl_pct, quantity,
                 signal_score, signal_reason, duration_minutes, outcome)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (pos["symbol"], pos["direction"], entry, close_price, pnl_inr, pnl_pct,
                 int(pos["quantity"]), pos.get("signal_score", 0),
                 pos.get("signal_reason", ""), duration, outcome))
            conn.commit()
    finally:
        conn.close()


def _open_db_positions():
    conn = get_conn()
    try:
        if hasattr(conn, "cursor"):
            from psycopg2.extras import RealDictCursor
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT * FROM equity_positions WHERE status='open'")
            rows = [dict(r) for r in cur.fetchall()]; cur.close()
        else:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM equity_positions WHERE status='open'").fetchall()]
        return rows
    finally:
        conn.close()


def _symbols_traded_today():
    """Symbols already opened today (open or closed) — one shot per name per day."""
    conn = get_conn()
    start = datetime.now(IST).strftime("%Y-%m-%d")
    try:
        if hasattr(conn, "cursor"):
            from psycopg2.extras import RealDictCursor
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT DISTINCT symbol FROM equity_positions WHERE opened_at::date = %s", (start,))
            rows = [r["symbol"] for r in cur.fetchall()]; cur.close()
        else:
            rows = [r[0] for r in conn.execute(
                "SELECT DISTINCT symbol FROM equity_positions WHERE date(opened_at)=?", (start,)).fetchall()]
        return set(rows)
    finally:
        conn.close()


# ── Day P&L from the exchange (authoritative; avoids TZ math) ──────────────────

def get_equity_day_pnl() -> float:
    """Sum of today's NSE MIS P&L (realized + unrealized) straight from Kite."""
    try:
        net = get_kite().positions().get("net", [])
        return sum(float(p.get("pnl", 0)) for p in net if p.get("exchange") == "NSE")
    except Exception as e:
        logger.error(f"get_equity_day_pnl failed: {e}")
        return 0.0


# ── Position sync ──────────────────────────────────────────────────────────────

def _sl_fill_price(sl_order_id):
    try:
        hist = get_kite().order_history(sl_order_id)
        if hist and hist[-1].get("status") == "COMPLETE":
            return float(hist[-1].get("average_price") or 0) or None
    except Exception:
        pass
    return None


def _sync_positions(app):
    db_open = _open_db_positions()
    if not db_open:
        return
    exch = {p["symbol"] for p in get_equity_positions()}
    for pos in db_open:
        sym = pos["symbol"]
        if sym in exch:
            continue  # still live
        # Closed on exchange — SL-M filled or otherwise flat.
        close_price = _sl_fill_price(pos.get("sl_order_id")) \
            or get_equity_ltp([sym]).get(sym) or float(pos["entry_price"])
        qty = int(pos["quantity"])
        pnl = (close_price - float(pos["entry_price"])) * qty
        if pos["direction"] == "short":
            pnl = -pnl
        duration = _duration_min(pos.get("opened_at"))
        reason = "sl_hit" if pnl < 0 else "target_or_exit"
        _close_record(pos["id"], close_price, pnl, reason)
        _record_memory(pos, close_price, pnl, duration)
        post_equity_closed(app.client, sym, pos["direction"], float(pos["entry_price"]),
                           close_price, pnl, reason, duration)
        logger.info(f"Equity synced closed: {sym} P&L ₹{pnl:+,.0f} ({reason})")


def _duration_min(opened_at):
    if not opened_at:
        return 0
    try:
        if isinstance(opened_at, str):
            opened_at = datetime.fromisoformat(opened_at)
        return int((datetime.now() - opened_at.replace(tzinfo=None)).total_seconds() / 60)
    except Exception:
        return 0


# ── Main scan ──────────────────────────────────────────────────────────────────

def run_equity_scan(app):
    global _circuit_tripped_date
    if _equity_paused:
        return
    if not _market_open_for_scan():
        return
    if not is_authorized():
        logger.info("Equity: Kite not authorized — skipping scan")
        return

    # Reconcile any closed positions first.
    _sync_positions(app)

    # Daily-loss circuit.
    day_pnl = get_equity_day_pnl()
    if -day_pnl >= EQUITY_DAILY_LOSS_INR:
        if _circuit_tripped_date != _today():
            _circuit_tripped_date = _today()
            _squareoff(app, reason="circuit")
            post_equity_circuit(app.client, -day_pnl, EQUITY_DAILY_LOSS_INR)
            logger.warning(f"Equity daily-loss circuit hit: ₹{-day_pnl:,.0f}")
        return
    if _circuit_tripped_date == _today():
        return  # already halted for the day

    # No new entries after the cutoff (let existing run to square-off).
    if _mins_now() >= EQUITY_NO_NEW_AFTER[0] * 60 + EQUITY_NO_NEW_AFTER[1]:
        return

    db_open = _open_db_positions()
    slots = EQUITY_MAX_POSITIONS - len(db_open)
    if slots <= 0:
        return

    watch = get_watchlist(_today())
    if not watch:
        logger.info("Equity: no watchlist for today — sitting out")
        return

    held = {p["symbol"] for p in db_open}
    traded = _symbols_traded_today()
    attempted = _attempted.setdefault(_today(), set())
    capital = get_equity_capital()

    candidates = []
    for w in watch:
        sym = w["symbol"]
        if sym in held or sym in traded or sym in attempted:
            continue
        plan = evaluate_equity(sym, bias=w.get("bias"))
        if plan:
            # Per-trade notional cap at capital (no reliance on MIS leverage stacking).
            if plan["quantity"] * plan["entry"] > capital:
                import math
                plan["quantity"] = math.floor(capital / plan["entry"])
                plan["risk_inr"] = round(plan["quantity"] * abs(plan["entry"] - plan["sl"]), 2)
            if plan["quantity"] >= 1:
                candidates.append(plan)

    if not candidates:
        logger.info("Equity: no watchlist name passed technical confirmation this scan")
        return

    candidates.sort(key=lambda p: p["score"], reverse=True)
    for plan in candidates[:slots]:
        _open_trade(app, plan)


def _open_trade(app, plan):
    if _dry_run():
        logger.info(f"[DRY_RUN] would open {plan['symbol']} {plan['direction']} "
                    f"qty={plan['quantity']} entry~{plan['entry']} SL={plan['sl']}")
        return
    # Mark attempted BEFORE placing — one shot per name per day, so a rejected order
    # can never retry every 5 min and bleed round-trip costs (the Jun-4 ₹554 lesson).
    _attempted.setdefault(_today(), set()).add(plan["symbol"])
    post_equity_thesis(app.client, plan)
    result = place_equity_order(plan["symbol"], plan["direction"], plan["quantity"], plan["sl"])
    if not result:
        logger.warning(f"Equity order failed for {plan['symbol']} — not saving")
        return
    fill = result["fill_price"]
    pid = _save_position(plan, fill, result["entry_order_id"], result["sl_order_id"])
    post_equity_opened(app.client, plan, fill)
    logger.info(f"Equity opened: {plan['symbol']} pos_id={pid} fill={fill}")


# ── Square-off + summary ───────────────────────────────────────────────────────

def _squareoff(app, reason="eod"):
    """Exit all open DB positions at market and cancel resting stops."""
    closed = []
    for pos in _open_db_positions():
        sym = pos["symbol"]
        qty = int(pos["quantity"])
        signed = qty if pos["direction"] == "long" else -qty
        if not _dry_run():
            exit_equity_position(sym, signed, pos.get("sl_order_id"))
        close_price = get_equity_ltp([sym]).get(sym) or float(pos["entry_price"])
        pnl = (close_price - float(pos["entry_price"])) * qty
        if pos["direction"] == "short":
            pnl = -pnl
        _close_record(pos["id"], close_price, pnl, f"squared_off_{reason}")
        _record_memory(pos, close_price, pnl, _duration_min(pos.get("opened_at")))
        closed.append({"symbol": sym, "pnl": pnl})
    if closed:
        post_equity_squareoff(app.client, closed)
        logger.info(f"Equity square-off ({reason}): {len(closed)} positions")


def run_equity_squareoff(app):
    """Scheduled hard square-off at 3:15 PM IST."""
    if datetime.now(IST).weekday() >= 5:
        return
    _squareoff(app, reason="315")


def run_equity_daily_summary(app):
    if datetime.now(IST).weekday() >= 5:
        return
    start = datetime.now(IST).strftime("%Y-%m-%d")
    conn = get_conn()
    try:
        if hasattr(conn, "cursor"):
            from psycopg2.extras import RealDictCursor
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("""SELECT outcome, COUNT(*) c, COALESCE(SUM(pnl_inr),0) p
                           FROM equity_memory WHERE created_at::date=%s GROUP BY outcome""", (start,))
            rows = {r["outcome"]: r for r in cur.fetchall()}; cur.close()
        else:
            rows = {}
            for r in conn.execute("""SELECT outcome, COUNT(*) c, COALESCE(SUM(pnl_inr),0) p
                                     FROM equity_memory WHERE date(created_at)=? GROUP BY outcome""",
                                  (start,)).fetchall():
                rows[r[0]] = {"outcome": r[0], "c": r[1], "p": r[2]}
    finally:
        conn.close()
    wins = int(rows.get("win", {}).get("c", 0))
    losses = int(rows.get("loss", {}).get("c", 0))
    day_pnl = float(rows.get("win", {}).get("p", 0)) + float(rows.get("loss", {}).get("p", 0))
    post_equity_daily_summary(app.client, day_pnl, wins + losses, wins, losses, get_equity_capital())
