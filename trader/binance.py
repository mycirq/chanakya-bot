import os
import time
import logging
import ccxt
from trader.config import MAX_LEVERAGE

logger = logging.getLogger(__name__)

_exchange = None

# ── API health tracking ──────────────────────────────────────────────────
# Counts consecutive Binance API failures across all fetch functions. When
# we cross the threshold, post a single Slack alert so a proxy outage like
# the May 2026 incident (16h silent) gets noticed within ~30 min instead
# of by accident.
_consecutive_api_failures = 0
_API_FAILURE_ALERT_THRESHOLD = 5
_alert_sent = False
_last_alert_at = 0.0


def _post_health_alert(text):
    """Post a single health alert to Slack. Best-effort, never raises."""
    global _last_alert_at
    if time.time() - _last_alert_at < 600:  # 10-min rate limit
        return
    try:
        from slack_sdk import WebClient
        client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
        channel = os.environ.get("ALERT_CHANNEL_ID", "C0B3SHS7NKH")  # crypto-trades
        client.chat_postMessage(channel=channel, text=text)
        _last_alert_at = time.time()
    except Exception as e:
        logger.warning(f"Health alert post failed: {type(e).__name__}: {e}")


def _track_api_failure(fn_name, error_type):
    global _consecutive_api_failures, _alert_sent
    _consecutive_api_failures += 1
    if _consecutive_api_failures >= _API_FAILURE_ALERT_THRESHOLD and not _alert_sent:
        _post_health_alert(
            f":rotating_light: Binance API down — {_consecutive_api_failures} consecutive "
            f"failures (last: `{fn_name}` → `{error_type}`). Likely proxy issue. "
            f"Bot cannot place/close trades until resolved."
        )
        _alert_sent = True


def _track_api_success():
    global _consecutive_api_failures, _alert_sent
    if _alert_sent:
        _post_health_alert(
            f":white_check_mark: Binance API recovered after {_consecutive_api_failures} failures."
        )
    _consecutive_api_failures = 0
    _alert_sent = False

def get_exchange():
    global _exchange
    if _exchange is None:
        _exchange = ccxt.binanceusdm({
            "apiKey":  os.environ["BINANCE_API_KEY"],
            "secret":  os.environ["BINANCE_API_SECRET"],
            "options": {
                "defaultType":             "future",
                "adjustForTimeDifference": True,
                "recvWindow":              10000,
            },
            "proxies": {
                "http":  os.environ["PROXY_URL"],
                "https": os.environ["PROXY_URL"],
            },
        })
    return _exchange


def get_futures_balance():
    """Returns available USDT in USDT-M futures wallet via direct fapi endpoint."""
    last_err = None
    for attempt in range(2):
        try:
            result = get_exchange().fapiPrivateV2GetBalance()
            _track_api_success()
            for item in result:
                if item.get("asset") == "USDT":
                    return float(item.get("availableBalance", 0))
            return 0.0
        except Exception as e:
            last_err = e
            logger.error(f"Balance fetch failed (attempt {attempt+1}): {type(e).__name__}: {e}")
            if attempt == 0:
                time.sleep(3)
    _track_api_failure("get_futures_balance", type(last_err).__name__ if last_err else "unknown")
    return 0.0


def get_open_positions(strict=False):
    """Returns list of open USDT-M futures positions.

    When strict=True, returns None if the fetch fails (so callers can tell a
    genuinely-flat account, which returns [], apart from an API/proxy failure).
    """
    last_err = None
    for attempt in range(2):
        try:
            positions = get_exchange().fetch_positions()
            _track_api_success()
            open_pos = []
            for p in positions:
                contracts = abs(float(p.get("contracts") or 0))
                if contracts > 0:
                    open_pos.append({
                        "symbol":         p["symbol"],
                        "side":           p["side"],          # 'long' or 'short'
                        "size":           contracts,
                        "entry_price":    float(p["entryPrice"]       or 0),
                        "mark_price":     float(p["markPrice"]        or 0),
                        "unrealized_pnl": float(p["unrealizedPnl"]    or 0),
                        "leverage":       float(p["leverage"]         or MAX_LEVERAGE),
                        "liq_price":      float(p["liquidationPrice"] or 0),
                        "margin":         float(p["initialMargin"]    or 0),
                    })
            return open_pos
        except Exception as e:
            last_err = e
            logger.error(f"Positions fetch failed (attempt {attempt+1}): {type(e).__name__}: {e}")
            if attempt == 0:
                time.sleep(3)
    _track_api_failure("get_open_positions", type(last_err).__name__ if last_err else "unknown")
    return None if strict else []


def get_realized_pnl_since(symbol, since_ms):
    """Sum of REALIZED_PNL income for `symbol` since `since_ms` (epoch ms).

    This is Binance's authoritative realized P&L for the position — used when
    reconciling a closed position so we record the true fill result instead of
    estimating from a candle price. Returns None on failure (caller should retry).
    """
    try:
        ex = get_exchange()
        market_id = ex.market(symbol)["id"]  # e.g. 'BTCUSDT'
        total = 0.0
        cur = int(since_ms)
        while True:
            batch = ex.fapiPrivateGetIncome({
                "incomeType": "REALIZED_PNL", "symbol": market_id,
                "startTime": cur, "limit": 1000,
            })
            if not batch:
                break
            total += sum(float(i["income"]) for i in batch)
            if len(batch) < 1000:
                break
            cur = int(batch[-1]["time"]) + 1
        return total
    except Exception as e:
        logger.warning(f"Realized PnL fetch failed for {symbol}: {type(e).__name__}: {e}")
        return None


def set_leverage(symbol, leverage):
    try:
        get_exchange().set_leverage(leverage, symbol)
    except Exception as e:
        logger.warning(f"Set leverage failed for {symbol}: {e}")


def set_margin_mode(symbol):
    try:
        get_exchange().set_margin_mode("isolated", symbol)
    except Exception as e:
        logger.warning(f"Set margin mode failed for {symbol}: {e}")


def _rollback_position(symbol, side):
    """Market-close any open position + cancel orphan protective orders.
    Used when entry fills but protection (SL) can't be placed."""
    try:
        cancel_open_orders(symbol)
    except Exception as e:
        logger.warning(f"Cancel orders during rollback failed for {symbol}: {e}")
    try:
        close_position(symbol, side)
    except Exception as e:
        logger.error(f"Rollback close failed for {symbol}: {e}")


def place_order(symbol, side, usdt_margin, entry_price, tp_price, sl_price, leverage):
    """
    Place a futures order with TP and SL atomically.
    Either entry + SL both succeed, or the entry is rolled back via market close.
    Returns {"order": <entry_order>, "real_liq_price": float|None,
             "tp_placed": bool, "sl_placed": True} on success, or None.
    """
    import time

    # Coarse pre-check using rough 1/leverage liq estimate. Real check
    # happens post-fill against Binance's actual liquidationPrice.
    coarse_liq_distance = entry_price / leverage
    if side == "long":
        if sl_price <= entry_price - coarse_liq_distance * 0.95:
            logger.error(f"SL {sl_price} past coarse liq for {symbol} long — aborting pre-entry")
            return None
    else:
        if sl_price >= entry_price + coarse_liq_distance * 0.95:
            logger.error(f"SL {sl_price} past coarse liq for {symbol} short — aborting pre-entry")
            return None

    ex = get_exchange()
    set_margin_mode(symbol)
    set_leverage(symbol, leverage)

    order_side = "buy" if side == "long" else "sell"
    close_side = "sell" if side == "long" else "buy"
    notional   = usdt_margin * leverage
    amount     = ex.amount_to_precision(symbol, notional / entry_price)

    # ── Step 1: entry ────────────────────────────────────────────────────
    limit_price = entry_price * 1.0005 if side == "long" else entry_price * 0.9995
    limit_price = ex.price_to_precision(symbol, limit_price)
    try:
        entry_order = ex.create_order(symbol, "limit", order_side, amount, limit_price, {
            "timeInForce":  "GTC",
            "positionSide": "BOTH",
        })
    except Exception as e:
        logger.error(f"Entry order failed for {symbol}: {type(e).__name__}: {e}")
        return None
    logger.info(f"Entry placed: {symbol} {side} {amount} @ {limit_price}")

    # ── Step 2: wait for fill (15s) ──────────────────────────────────────
    fill_deadline = time.time() + 15
    filled = False
    while time.time() < fill_deadline:
        try:
            status = ex.fetch_order(entry_order["id"], symbol)
            if status.get("status") == "closed":  # ccxt: closed = fully filled
                filled = True
                break
        except Exception as e:
            logger.warning(f"fetch_order failed for {symbol}: {type(e).__name__}: {e}")
        time.sleep(2)

    if not filled:
        logger.warning(f"Entry not filled in 15s for {symbol} — cancelling")
        try:
            ex.cancel_order(entry_order["id"], symbol)
        except Exception as e:
            logger.warning(f"Cancel of unfilled entry failed for {symbol}: {e}")
        return None

    # ── Step 3: fetch REAL liq price from Binance (accounts for actual MMR) ──
    real_liq = None
    try:
        positions = ex.fetch_positions([symbol])
        for p in positions:
            if abs(float(p.get("contracts") or 0)) > 0:
                real_liq = float(p.get("liquidationPrice") or 0) or None
                break
    except Exception as e:
        logger.warning(f"Real-liq fetch failed for {symbol}: {type(e).__name__}: {e}")

    # ── Step 4: re-check SL against REAL liq with 20% safety buffer ──────
    if real_liq:
        if side == "long":
            min_sl = real_liq + (entry_price - real_liq) * 0.20
            if sl_price <= min_sl:
                logger.error(f"SL {sl_price} past real liq buffer {min_sl:.6f} (real_liq={real_liq:.6f}) for {symbol} long — rolling back")
                _rollback_position(symbol, side)
                return None
        else:
            max_sl = real_liq - (real_liq - entry_price) * 0.20
            if sl_price >= max_sl:
                logger.error(f"SL {sl_price} past real liq buffer {max_sl:.6f} (real_liq={real_liq:.6f}) for {symbol} short — rolling back")
                _rollback_position(symbol, side)
                return None

    # ── Step 5: place TP (non-fatal if rejected) ─────────────────────────
    tp_placed = False
    try:
        ex.create_order(symbol, "take_profit_market", close_side, amount, None, {
            "stopPrice":    ex.price_to_precision(symbol, tp_price),
            "closePosition": True,
            "workingType":  "MARK_PRICE",
            "positionSide": "BOTH",
        })
        tp_placed = True
    except Exception as e:
        logger.error(f"TP placement failed for {symbol}: {type(e).__name__}: {e}")

    # ── Step 6: place SL (FATAL if rejected — never leave position naked) ──
    try:
        ex.create_order(symbol, "stop_market", close_side, amount, None, {
            "stopPrice":    ex.price_to_precision(symbol, sl_price),
            "closePosition": True,
            "workingType":  "MARK_PRICE",
            "positionSide": "BOTH",
        })
    except Exception as e:
        logger.error(f"SL placement failed for {symbol}: {type(e).__name__}: {e} — rolling back")
        _rollback_position(symbol, side)
        return None

    logger.info(f"Entry + SL{' + TP' if tp_placed else ' (no TP)'} confirmed for {symbol}")
    return {
        "order":          entry_order,
        "real_liq_price": real_liq,
        "tp_placed":      tp_placed,
        "sl_placed":      True,
    }


def close_position(symbol, side):
    """Market close an open position."""
    ex = get_exchange()
    try:
        positions = ex.fetch_positions([symbol])
        for p in positions:
            contracts = float(p.get("contracts") or 0)
            if contracts > 0:
                close_side = "sell" if p["side"] == "long" else "buy"
                ex.create_order(symbol, "market", close_side, contracts, None,
                                {"reduceOnly": True})
                logger.info(f"Closed position: {symbol}")
                return True
    except Exception as e:
        logger.error(f"Close position failed for {symbol}: {e}")
    return False


def cancel_open_orders(symbol):
    """Cancel all open TP/SL orders for a symbol."""
    try:
        get_exchange().cancel_all_orders(symbol)
    except Exception as e:
        logger.warning(f"Cancel orders failed for {symbol}: {e}")


TRADFI_TOKENS = {
    "NVDA", "AAPL", "TSLA", "COIN", "MSFT", "GOOG", "AMZN", "META",
    "NFLX", "AMD", "INTC", "PLTR", "MSTR", "SQ", "PYPL", "UBER",
    "ABNB", "RIVN", "NIO", "BABA", "JD", "PDD",
}


def get_top_futures_pairs(n=30, min_quote_volume_usd=50_000_000):
    """Get top N USDT-M perpetual pairs by 24h volume.

    min_quote_volume_usd: hard floor on 24h USDT volume. Default $50M filters
    out illiquid memecoin-tier symbols (BSB/NIL/ESPORTS class) that historically
    produced the bulk of our losses.
    """
    try:
        ex = get_exchange()
        tickers = ex.fetch_tickers()
        usdt_perp = {
            k: v for k, v in tickers.items()
            if k.endswith("/USDT:USDT")
            and v.get("quoteVolume")
            and float(v["quoteVolume"]) >= min_quote_volume_usd
            and k.split("/")[0] not in TRADFI_TOKENS
        }
        sorted_pairs = sorted(usdt_perp.items(),
                              key=lambda x: x[1]["quoteVolume"], reverse=True)
        return [p[0] for p in sorted_pairs[:n]]
    except Exception as e:
        logger.error(f"Failed to fetch top pairs: {e}")
        return []


def fetch_ohlcv(symbol, timeframe="1h", limit=200):
    """Fetch OHLCV candles for a symbol."""
    try:
        data = get_exchange().fetch_ohlcv(symbol, timeframe, limit=limit)
        return data  # list of [timestamp, open, high, low, close, volume]
    except Exception as e:
        logger.warning(f"OHLCV fetch failed for {symbol}: {e}")
        return []
