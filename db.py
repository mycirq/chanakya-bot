import os
import logging
from datetime import datetime, timedelta

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///chanakya.db")

if DATABASE_URL.startswith("sqlite"):
    import sqlite3

    def get_conn():
        conn = sqlite3.connect("chanakya.db")
        conn.row_factory = sqlite3.Row
        return conn

    def init_db():
        conn = get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                market TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                channel_name TEXT NOT NULL,
                user_id TEXT NOT NULL,
                entry_price REAL NOT NULL,
                expected_roi REAL NOT NULL,
                analysis TEXT,
                tracking_days INTEGER DEFAULT 10,
                suggested_at TEXT DEFAULT (datetime('now')),
                review_at TEXT NOT NULL,
                reviewed INTEGER DEFAULT 0,
                exit_price REAL,
                actual_roi REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS posted_news (
                url TEXT PRIMARY KEY,
                posted_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
        conn.close()
        logging.info("SQLite DB initialized")

    def is_news_posted(url):
        conn = get_conn()
        row = conn.execute("SELECT url FROM posted_news WHERE url = ?", (url,)).fetchone()
        conn.close()
        return row is not None

    def mark_news_posted(urls):
        conn = get_conn()
        conn.executemany("INSERT OR IGNORE INTO posted_news (url) VALUES (?)", [(u,) for u in urls])
        conn.commit()
        conn.close()

    def is_duplicate(user_id, ticker, market):
        conn = get_conn()
        row = conn.execute("""
            SELECT id FROM suggestions
            WHERE user_id = ? AND ticker = ? AND market = ? AND reviewed = 0
        """, (user_id, ticker, market)).fetchone()
        conn.close()
        return row is not None

    def save_suggestion(ticker, market, channel_id, channel_name, user_id,
                        entry_price, expected_roi, analysis, tracking_days):
        conn = get_conn()
        review_at = (datetime.now() + timedelta(days=tracking_days)).isoformat()
        cur = conn.execute("""
            INSERT INTO suggestions
            (ticker, market, channel_id, channel_name, user_id, entry_price,
             expected_roi, analysis, tracking_days, review_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (ticker, market, channel_id, channel_name, user_id, entry_price,
              expected_roi, analysis, tracking_days, review_at))
        conn.commit()
        suggestion_id = cur.lastrowid
        conn.close()
        return suggestion_id

    def get_due_suggestions():
        conn = get_conn()
        rows = conn.execute("""
            SELECT * FROM suggestions
            WHERE reviewed = 0 AND review_at <= datetime('now')
        """).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_active_suggestions(user_id=None, market=None):
        conn = get_conn()
        query = "SELECT * FROM suggestions WHERE reviewed = 0"
        params = []
        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)
        if market:
            query += " AND market = ?"
            params.append(market)
        query += " ORDER BY suggested_at DESC"
        rows = conn.execute(query, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_distinct_users():
        conn = get_conn()
        rows = conn.execute("""
            SELECT DISTINCT user_id FROM suggestions WHERE reviewed = 0
        """).fetchall()
        conn.close()
        return [r["user_id"] for r in rows]

    def update_suggestion(suggestion_id, exit_price, actual_roi):
        conn = get_conn()
        conn.execute("""
            UPDATE suggestions
            SET reviewed = 1, exit_price = ?, actual_roi = ?
            WHERE id = ?
        """, (exit_price, actual_roi, suggestion_id))
        conn.commit()
        conn.close()

    def remove_suggestion(suggestion_id, user_id):
        """Delete a suggestion — only if it belongs to the user."""
        conn = get_conn()
        conn.execute("""
            DELETE FROM suggestions WHERE id = ? AND user_id = ?
        """, (suggestion_id, user_id))
        conn.commit()
        conn.close()

else:
    import psycopg2
    from psycopg2.extras import RealDictCursor

    def get_conn():
        url = DATABASE_URL
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        return psycopg2.connect(url)

    def init_db():
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS suggestions (
                id SERIAL PRIMARY KEY,
                ticker VARCHAR(50) NOT NULL,
                market VARCHAR(20) NOT NULL,
                channel_id VARCHAR(50) NOT NULL,
                channel_name VARCHAR(100) NOT NULL,
                user_id VARCHAR(50) NOT NULL,
                entry_price DECIMAL(20,8) NOT NULL,
                expected_roi DECIMAL(10,2) NOT NULL,
                analysis TEXT,
                tracking_days INTEGER DEFAULT 10,
                suggested_at TIMESTAMP DEFAULT NOW(),
                review_at TIMESTAMP NOT NULL,
                reviewed BOOLEAN DEFAULT FALSE,
                exit_price DECIMAL(20,8),
                actual_roi DECIMAL(10,2)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS posted_news (
                url TEXT PRIMARY KEY,
                posted_at TIMESTAMP DEFAULT NOW()
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        logging.info("PostgreSQL DB initialized")

    def is_news_posted(url):
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT url FROM posted_news WHERE url = %s", (url,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row is not None

    def mark_news_posted(urls):
        conn = get_conn()
        cur = conn.cursor()
        for url in urls:
            cur.execute("INSERT INTO posted_news (url) VALUES (%s) ON CONFLICT DO NOTHING", (url,))
        conn.commit()
        cur.close()
        conn.close()

    def is_duplicate(user_id, ticker, market):
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT id FROM suggestions
            WHERE user_id = %s AND ticker = %s AND market = %s AND reviewed = FALSE
        """, (user_id, ticker, market))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row is not None

    def save_suggestion(ticker, market, channel_id, channel_name, user_id,
                        entry_price, expected_roi, analysis, tracking_days):
        conn = get_conn()
        cur = conn.cursor()
        review_at = datetime.now() + timedelta(days=tracking_days)
        cur.execute("""
            INSERT INTO suggestions
            (ticker, market, channel_id, channel_name, user_id, entry_price,
             expected_roi, analysis, tracking_days, review_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (ticker, market, channel_id, channel_name, user_id, entry_price,
              expected_roi, analysis, tracking_days, review_at))
        suggestion_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        return suggestion_id

    def get_due_suggestions():
        conn = get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT * FROM suggestions
            WHERE reviewed = FALSE AND review_at <= NOW()
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [dict(r) for r in rows]

    def get_active_suggestions(user_id=None, market=None):
        conn = get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        query = "SELECT * FROM suggestions WHERE reviewed = FALSE"
        params = []
        if user_id:
            query += " AND user_id = %s"
            params.append(user_id)
        if market:
            query += " AND market = %s"
            params.append(market)
        query += " ORDER BY suggested_at DESC"
        cur.execute(query, params)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [dict(r) for r in rows]

    def get_distinct_users():
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT user_id FROM suggestions WHERE reviewed = FALSE
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [r[0] for r in rows]

    def update_suggestion(suggestion_id, exit_price, actual_roi):
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            UPDATE suggestions
            SET reviewed = TRUE, exit_price = %s, actual_roi = %s
            WHERE id = %s
        """, (exit_price, actual_roi, suggestion_id))
        conn.commit()
        cur.close()
        conn.close()

    def remove_suggestion(suggestion_id, user_id):
        """Delete a suggestion — only if it belongs to the user."""
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            DELETE FROM suggestions WHERE id = %s AND user_id = %s
        """, (suggestion_id, user_id))
        conn.commit()
        cur.close()
        conn.close()
