"""
One-time migration: Insert orphaned BSB/USDT position into trade_positions.
BSB order was placed on Binance but DB save crashed due to numpy float64.
Run once on Railway, then delete this file.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import get_conn


def run():
    conn = get_conn()
    if not hasattr(conn, "cursor"):
        print("SQLite — skipping, this is for Railway PostgreSQL only")
        conn.close()
        return

    from psycopg2.extras import RealDictCursor
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # Check if BSB already exists
    cur.execute("SELECT id FROM trade_positions WHERE symbol='BSB/USDT:USDT' AND status='open'")
    if cur.fetchone():
        print("BSB position already exists in DB — skipping")
        cur.close()
        conn.close()
        return

    # BSB/USDT long @ 1.073, 5x leverage
    # Values from the failed trade that was placed on Binance
    entry_price = 1.073
    leverage = 5
    margin_usdt = 38.0  # ~20% of ~190 available (typical bot sizing)
    size = margin_usdt * leverage / entry_price  # notional / price
    tp_price = entry_price * 1.06   # ~6% TP
    sl_price = entry_price * 0.97   # ~3% SL
    liq_price = entry_price * 0.80  # ~20% from entry at 5x isolated

    cur.execute("""
        INSERT INTO trade_positions
        (symbol, direction, entry_price, tp_price, sl_price, liq_price,
         margin_usdt, leverage, size, signal_score, signal_reason, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'open')
        RETURNING id
    """, (
        "BSB/USDT:USDT", "long", entry_price, tp_price, sl_price, liq_price,
        margin_usdt, leverage, size, 60, "Manual insert — orphaned position from numpy crash"
    ))
    pid = cur.fetchone()["id"]
    conn.commit()
    cur.close()
    conn.close()
    print(f"BSB position inserted with id={pid}")


if __name__ == "__main__":
    run()
