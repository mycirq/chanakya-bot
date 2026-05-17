import os
import logging
import ccxt
from trader.config import MAX_LEVERAGE

logger = logging.getLogger(__name__)

_exchange = None

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
                "http":  "socks5h://92.4.80.136:1080",
                "https": "socks5h://92.4.80.136:1080",
            },
        })
    return _exchange


def get_futures_balance():
    """Returns available USDT in USDT-M futures wallet via direct fapi endpoint."""
    try:
        # Calls /fapi/v2/balance — pure futures, never touches spot API
        result = get_exchange().fapiPrivateV2GetBalance()
        for item in result:
            if item.get("asset") == "USDT":
                return float(item.get("availableBalance", 0))
        return 0.0
    except Exception as e:
        logger.error(f"Balance fetch failed: {e}")
        return 0.0


def get_open_positions():
    """Returns list of open USDT-M futures positions."""
    try:
        positions = get_exchange().fetch_positions()
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
        logger.error(f"Positions fetch failed: {e}")
        return []


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


def place_order(symbol, side, usdt_margin, entry_price, tp_price, sl_price, leverage):
    """
    Place a futures order with TP and SL.
    side: 'long' or 'short'
    usdt_margin: margin in USDT (not notional)
    Returns order dict or None.
    """
    ex = get_exchange()
    try:
        set_margin_mode(symbol)
        set_leverage(symbol, leverage)

        market = ex.market(symbol)
        notional = usdt_margin * leverage
        amount = notional / entry_price
        amount = ex.amount_to_precision(symbol, amount)

        order_side = "buy" if side == "long" else "sell"
        close_side = "sell" if side == "long" else "buy"

        # Limit entry — slightly aggressive to ensure fill, cheaper than market
        # Long: limit at ask (entry_price + 0.05%), Short: limit at bid (entry_price - 0.05%)
        limit_price = entry_price * 1.0005 if side == "long" else entry_price * 0.9995
        limit_price = ex.price_to_precision(symbol, limit_price)
        order = ex.create_order(symbol, "limit", order_side, amount, limit_price, {
            "timeInForce":  "GTC",
            "positionSide": "BOTH",   # one-way mode
        })
        logger.info(f"Entry order placed: {symbol} {side} {amount} @ {limit_price}")

        # TP (take profit)
        ex.create_order(symbol, "take_profit_market", close_side, amount, None, {
            "stopPrice":    ex.price_to_precision(symbol, tp_price),
            "closePosition": True,
            "workingType":  "MARK_PRICE",
            "positionSide": "BOTH",
        })

        # SL (stop loss)
        ex.create_order(symbol, "stop_market", close_side, amount, None, {
            "stopPrice":    ex.price_to_precision(symbol, sl_price),
            "closePosition": True,
            "workingType":  "MARK_PRICE",
            "positionSide": "BOTH",
        })

        logger.info(f"TP @ {tp_price}, SL @ {sl_price} set for {symbol}")
        return order

    except Exception as e:
        logger.error(f"Order placement failed for {symbol}: {e}")
        return None


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


def get_top_futures_pairs(n=30):
    """Get top N USDT-M perpetual pairs by 24h volume."""
    try:
        ex = get_exchange()
        tickers = ex.fetch_tickers()
        usdt_perp = {
            k: v for k, v in tickers.items()
            if k.endswith("/USDT:USDT") and v.get("quoteVolume")
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
