"""
Trade memory — stores every closed trade with full context.
Fed to Claude API before each decision so the bot learns over time.
"""
import logging
from db import get_conn

logger = logging.getLogger(__name__)


def init_month_snapshot(balance_usdt, target_pct):
    """Called on 1st of each month — saves starting balance + target."""
    conn = get_conn()
    if hasattr(conn, 'cursor'):
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS month_snapshots (
                id SERIAL PRIMARY KEY,
                month VARCHAR(7) NOT NULL,
                start_balance DECIMAL(20,8),
                target_pct DECIMAL(10,2),
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            INSERT INTO month_snapshots (month, start_balance, target_pct)
            VALUES (%s, %s, %s)
            ON CONFLICT DO NOTHING
        """, (get_current_month(), balance_usdt, target_pct))
        conn.commit(); cur.close()
    else:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS month_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                month TEXT NOT NULL UNIQUE,
                start_balance REAL,
                target_pct REAL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            INSERT OR IGNORE INTO month_snapshots (month, start_balance, target_pct)
            VALUES (?, ?, ?)
        """, (get_current_month(), balance_usdt, target_pct))
        conn.commit()
    conn.close()


def get_month_snapshot():
    """Get this month's starting balance + target."""
    conn = get_conn()
    month = get_current_month()
    if hasattr(conn, 'cursor'):
        from psycopg2.extras import RealDictCursor
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM month_snapshots WHERE month = %s", (month,))
        row = cur.fetchone()
        cur.close()
    else:
        row = conn.execute(
            "SELECT * FROM month_snapshots WHERE month = ?", (month,)
        ).fetchone()
        row = dict(row) if row else None
    conn.close()
    return row


def get_current_month():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m")


def update_monthly_target(target_pct):
    """Owner updates target for current month."""
    conn = get_conn()
    month = get_current_month()
    if hasattr(conn, 'cursor'):
        cur = conn.cursor()
        cur.execute("""
            UPDATE month_snapshots SET target_pct = %s WHERE month = %s
        """, (target_pct, month))
        conn.commit(); cur.close()
    else:
        conn.execute(
            "UPDATE month_snapshots SET target_pct = ? WHERE month = ?",
            (target_pct, month)
        )
        conn.commit()
    conn.close()


def init_kite_month_snapshot(balance_inr, target_pct):
    conn = get_conn()
    month = get_current_month()
    if hasattr(conn, "cursor"):
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO kite_month_snapshots (month, start_balance, target_pct)
            VALUES (%s, %s, %s) ON CONFLICT DO NOTHING
        """, (month, balance_inr, target_pct))
        conn.commit(); cur.close()
    else:
        conn.execute("""
            INSERT OR IGNORE INTO kite_month_snapshots (month, start_balance, target_pct)
            VALUES (?, ?, ?)
        """, (month, balance_inr, target_pct))
        conn.commit()
    conn.close()


def get_kite_month_snapshot():
    conn = get_conn()
    month = get_current_month()
    if hasattr(conn, "cursor"):
        from psycopg2.extras import RealDictCursor
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM kite_month_snapshots WHERE month = %s", (month,))
        row = cur.fetchone(); cur.close()
    else:
        row = conn.execute(
            "SELECT * FROM kite_month_snapshots WHERE month = ?", (month,)
        ).fetchone()
        row = dict(row) if row else None
    conn.close()
    return row


def update_kite_monthly_target(target_pct):
    conn = get_conn()
    month = get_current_month()
    if hasattr(conn, "cursor"):
        cur = conn.cursor()
        cur.execute(
            "UPDATE kite_month_snapshots SET target_pct = %s WHERE month = %s",
            (target_pct, month)
        )
        conn.commit(); cur.close()
    else:
        conn.execute(
            "UPDATE kite_month_snapshots SET target_pct = ? WHERE month = ?",
            (target_pct, month)
        )
        conn.commit()
    conn.close()


def get_kite_trade_stats():
    conn = get_conn()
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
    conn.close()
    return rows


def init_trader_db():
    """Create all trader tables if they don't exist."""
    conn = get_conn()
    if hasattr(conn, 'cursor'):
        # PostgreSQL
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trade_config (
                id SERIAL PRIMARY KEY,
                market VARCHAR(20) NOT NULL,
                budget_usdt DECIMAL(20,8),
                target_pct DECIMAL(10,2),
                hard_stop_usdt DECIMAL(20,8),
                active BOOLEAN DEFAULT TRUE,
                paused BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trade_positions (
                id SERIAL PRIMARY KEY,
                symbol VARCHAR(50) NOT NULL,
                direction VARCHAR(10) NOT NULL,
                entry_price DECIMAL(20,8),
                tp_price DECIMAL(20,8),
                sl_price DECIMAL(20,8),
                liq_price DECIMAL(20,8),
                margin_usdt DECIMAL(20,8),
                leverage INTEGER,
                size DECIMAL(20,8),
                signal_score INTEGER,
                signal_reason TEXT,
                status VARCHAR(20) DEFAULT 'open',
                opened_at TIMESTAMP DEFAULT NOW(),
                closed_at TIMESTAMP,
                close_price DECIMAL(20,8),
                pnl_usdt DECIMAL(20,8),
                close_reason VARCHAR(50)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trade_memory (
                id SERIAL PRIMARY KEY,
                symbol VARCHAR(50),
                direction VARCHAR(10),
                entry_price DECIMAL(20,8),
                close_price DECIMAL(20,8),
                pnl_usdt DECIMAL(20,8),
                pnl_pct DECIMAL(10,4),
                signal_score INTEGER,
                signal_reason TEXT,
                zone VARCHAR(20),
                duration_minutes INTEGER,
                outcome VARCHAR(20),
                lesson TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS month_snapshots (
                id SERIAL PRIMARY KEY,
                month VARCHAR(7) NOT NULL UNIQUE,
                start_balance DECIMAL(20,8),
                target_pct DECIMAL(10,2),
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # Kite FnO tables
        cur.execute("""
            CREATE TABLE IF NOT EXISTS kite_config (
                key VARCHAR(50) PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS kite_positions (
                id SERIAL PRIMARY KEY,
                underlying VARCHAR(20) NOT NULL,
                direction VARCHAR(10) NOT NULL,
                tradingsymbol VARCHAR(50) NOT NULL,
                option_type VARCHAR(2),
                entry_premium DECIMAL(10,2),
                tp_premium DECIMAL(10,2),
                sl_premium DECIMAL(10,2),
                quantity INTEGER,
                lot_size INTEGER,
                signal_score INTEGER,
                signal_reason TEXT,
                status VARCHAR(20) DEFAULT 'open',
                opened_at TIMESTAMP DEFAULT NOW(),
                closed_at TIMESTAMP,
                close_premium DECIMAL(10,2),
                pnl_inr DECIMAL(12,2),
                close_reason VARCHAR(50)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS kite_memory (
                id SERIAL PRIMARY KEY,
                underlying VARCHAR(20),
                direction VARCHAR(10),
                tradingsymbol VARCHAR(50),
                entry_premium DECIMAL(10,2),
                close_premium DECIMAL(10,2),
                pnl_inr DECIMAL(12,2),
                pnl_pct DECIMAL(10,4),
                quantity INTEGER,
                signal_score INTEGER,
                signal_reason TEXT,
                duration_minutes INTEGER,
                outcome VARCHAR(20),
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS kite_month_snapshots (
                id SERIAL PRIMARY KEY,
                month VARCHAR(7) NOT NULL UNIQUE,
                start_balance DECIMAL(12,2),
                target_pct DECIMAL(10,2),
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        conn.commit()
        cur.close()
    else:
        # SQLite
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trade_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market TEXT NOT NULL,
                budget_usdt REAL,
                target_pct REAL,
                hard_stop_usdt REAL,
                active INTEGER DEFAULT 1,
                paused INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trade_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry_price REAL,
                tp_price REAL,
                sl_price REAL,
                liq_price REAL,
                margin_usdt REAL,
                leverage INTEGER,
                size REAL,
                signal_score INTEGER,
                signal_reason TEXT,
                status TEXT DEFAULT 'open',
                opened_at TEXT DEFAULT (datetime('now')),
                closed_at TEXT,
                close_price REAL,
                pnl_usdt REAL,
                close_reason TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trade_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT,
                direction TEXT,
                entry_price REAL,
                close_price REAL,
                pnl_usdt REAL,
                pnl_pct REAL,
                signal_score INTEGER,
                signal_reason TEXT,
                zone TEXT,
                duration_minutes INTEGER,
                outcome TEXT,
                lesson TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS month_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                month TEXT NOT NULL UNIQUE,
                start_balance REAL,
                target_pct REAL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kite_config (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kite_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                underlying TEXT NOT NULL,
                direction TEXT NOT NULL,
                tradingsymbol TEXT NOT NULL,
                option_type TEXT,
                entry_premium REAL,
                tp_premium REAL,
                sl_premium REAL,
                quantity INTEGER,
                lot_size INTEGER,
                signal_score INTEGER,
                signal_reason TEXT,
                status TEXT DEFAULT 'open',
                opened_at TEXT DEFAULT (datetime('now')),
                closed_at TEXT,
                close_premium REAL,
                pnl_inr REAL,
                close_reason TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kite_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                underlying TEXT,
                direction TEXT,
                tradingsymbol TEXT,
                entry_premium REAL,
                close_premium REAL,
                pnl_inr REAL,
                pnl_pct REAL,
                quantity INTEGER,
                signal_score INTEGER,
                signal_reason TEXT,
                duration_minutes INTEGER,
                outcome TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kite_month_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                month TEXT NOT NULL UNIQUE,
                start_balance REAL,
                target_pct REAL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
    conn.close()
    logger.info("Trader DB tables initialized")


def save_position(symbol, direction, entry_price, tp_price, sl_price,
                  liq_price, margin_usdt, leverage, size, signal_score, signal_reason):
    conn = get_conn()
    if hasattr(conn, 'cursor'):
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO trade_positions
            (symbol, direction, entry_price, tp_price, sl_price, liq_price,
             margin_usdt, leverage, size, signal_score, signal_reason)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (symbol, direction, entry_price, tp_price, sl_price, liq_price,
              margin_usdt, leverage, size, signal_score, signal_reason))
        pid = cur.fetchone()[0]
        conn.commit()
        cur.close()
    else:
        cur = conn.execute("""
            INSERT INTO trade_positions
            (symbol, direction, entry_price, tp_price, sl_price, liq_price,
             margin_usdt, leverage, size, signal_score, signal_reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (symbol, direction, entry_price, tp_price, sl_price, liq_price,
              margin_usdt, leverage, size, signal_score, signal_reason))
        pid = cur.lastrowid
        conn.commit()
    conn.close()
    return pid


def close_position_record(position_id, close_price, pnl_usdt, close_reason):
    conn = get_conn()
    if hasattr(conn, 'cursor'):
        cur = conn.cursor()
        cur.execute("""
            UPDATE trade_positions
            SET status='closed', closed_at=NOW(), close_price=%s,
                pnl_usdt=%s, close_reason=%s
            WHERE id=%s
        """, (close_price, pnl_usdt, close_reason, position_id))
        conn.commit()
        cur.close()
    else:
        conn.execute("""
            UPDATE trade_positions
            SET status='closed', closed_at=datetime('now'), close_price=?,
                pnl_usdt=?, close_reason=?
            WHERE id=?
        """, (close_price, pnl_usdt, close_reason, position_id))
        conn.commit()
    conn.close()


def record_memory(symbol, direction, entry_price, close_price, pnl_usdt,
                  signal_score, signal_reason, zone, duration_minutes):
    """Save a closed trade to memory for future learning."""
    pnl_pct  = ((close_price - entry_price) / entry_price * 100) if entry_price else 0
    if direction == "short":
        pnl_pct = -pnl_pct
    outcome  = "win" if pnl_usdt > 0 else "loss"
    lesson   = _derive_lesson(outcome, signal_score, signal_reason, zone, duration_minutes)

    conn = get_conn()
    if hasattr(conn, 'cursor'):
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO trade_memory
            (symbol, direction, entry_price, close_price, pnl_usdt, pnl_pct,
             signal_score, signal_reason, zone, duration_minutes, outcome, lesson)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (symbol, direction, entry_price, close_price, pnl_usdt, pnl_pct,
              signal_score, signal_reason, zone, duration_minutes, outcome, lesson))
        conn.commit()
        cur.close()
    else:
        conn.execute("""
            INSERT INTO trade_memory
            (symbol, direction, entry_price, close_price, pnl_usdt, pnl_pct,
             signal_score, signal_reason, zone, duration_minutes, outcome, lesson)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (symbol, direction, entry_price, close_price, pnl_usdt, pnl_pct,
              signal_score, signal_reason, zone, duration_minutes, outcome, lesson))
        conn.commit()
    conn.close()


def get_recent_memory(limit=50):
    """Fetch last N trades for Claude context."""
    conn = get_conn()
    if hasattr(conn, 'cursor'):
        from psycopg2.extras import RealDictCursor
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT * FROM trade_memory ORDER BY created_at DESC LIMIT %s
        """, (limit,))
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
    else:
        rows = [dict(r) for r in conn.execute("""
            SELECT * FROM trade_memory ORDER BY created_at DESC LIMIT ?
        """, (limit,)).fetchall()]
    conn.close()
    return rows


def get_open_positions_db():
    conn = get_conn()
    if hasattr(conn, 'cursor'):
        from psycopg2.extras import RealDictCursor
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM trade_positions WHERE status='open'")
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
    else:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM trade_positions WHERE status='open'"
        ).fetchall()]
    conn.close()
    return rows


def get_trade_stats():
    """Win rate, avg P&L, best/worst pairs from memory."""
    conn = get_conn()
    if hasattr(conn, 'cursor'):
        from psycopg2.extras import RealDictCursor
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT outcome, COUNT(*) as cnt, AVG(pnl_usdt) as avg_pnl,
                   SUM(pnl_usdt) as total_pnl
            FROM trade_memory GROUP BY outcome
        """)
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
    else:
        rows = [dict(r) for r in conn.execute("""
            SELECT outcome, COUNT(*) as cnt, AVG(pnl_usdt) as avg_pnl,
                   SUM(pnl_usdt) as total_pnl
            FROM trade_memory GROUP BY outcome
        """).fetchall()]
    conn.close()
    return rows


def _derive_lesson(outcome, score, reason, zone, duration):
    """Generate a plain-English lesson from trade context."""
    parts = []
    if outcome == "loss" and score >= 70:
        parts.append("High-score signal still lost — check market regime before entry.")
    if outcome == "win" and score < 65:
        parts.append("Low-score signal won — may have caught momentum early.")
    if zone == "limited" and outcome == "loss":
        parts.append("Loss during limited zone — avoid new entries in overlap gaps.")
    if duration and duration < 30 and outcome == "loss":
        parts.append("Quick loss — possible stop hunt, widen SL slightly.")
    if duration and duration > 300 and outcome == "win":
        parts.append("Long-duration winner — trend trade, consider trailing SL next time.")
    return " ".join(parts) if parts else "No specific lesson."
