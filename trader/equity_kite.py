"""
Kite Equity Intraday — data + order placement (third vertical, independent of FnO).

Order model: MIS (intraday) entry as a MARKET order, immediately protected by a
separate SL-M (stop-loss market) order on the opposite side. This is ATOMIC — if
the protective stop cannot be placed, the entry is market-exited and rolled back,
so we never hold a naked intraday position. (Cover Orders are deprecated/flaky on
Zerodha, so we build the stop ourselves — same guarantee, supported product.)
"""
import time
import logging
from datetime import datetime

from trader.kite import get_kite, get_ohlcv
from trader.config import IST

logger = logging.getLogger(__name__)

_equity_instruments = None   # cache of NSE instrument dicts
_token_by_symbol = None      # {tradingsymbol: instrument_token}
_tick_by_symbol = None       # {tradingsymbol: tick_size}  (NSE varies: 0.05 / 0.50 / ...)

# Zerodha rejects plain MARKET orders via API ("market protection" error), so we
# place marketable LIMIT orders: priced through the touch by this buffer they fill
# immediately at the best available price, within a protective band.
ENTRY_BAND = 0.005   # 0.5% through for entries
EXIT_BAND  = 0.015   # 1.5% through for exits/rollback (ensure fill)
DEFAULT_TICK = 0.05


def _tick(symbol: str) -> float:
    if _tick_by_symbol is None:
        get_equity_instruments()
    return (_tick_by_symbol or {}).get(symbol.upper(), DEFAULT_TICK) or DEFAULT_TICK


def _round_tick(symbol: str, price: float) -> float:
    t = _tick(symbol)
    return round(round(float(price) / t) * t, 2)


# ── Instruments ──────────────────────────────────────────────────────────────

def get_equity_instruments() -> list:
    """Download + cache NSE cash-segment instruments (EQ series only)."""
    global _equity_instruments, _token_by_symbol, _tick_by_symbol
    if _equity_instruments is None:
        try:
            all_nse = get_kite().instruments("NSE")
            _equity_instruments = [
                i for i in all_nse
                if i.get("segment") == "NSE" and i.get("instrument_type") == "EQ"
            ]
            _token_by_symbol = {i["tradingsymbol"]: i["instrument_token"]
                                for i in _equity_instruments}
            _tick_by_symbol = {i["tradingsymbol"]: float(i.get("tick_size") or DEFAULT_TICK)
                               for i in _equity_instruments}
            logger.info(f"Loaded {len(_equity_instruments)} NSE equity instruments")
        except Exception as e:
            logger.error(f"get_equity_instruments failed: {e}")
            return []
    return _equity_instruments


def resolve_token(symbol: str):
    """NSE tradingsymbol → instrument_token (None if unknown)."""
    if _token_by_symbol is None:
        get_equity_instruments()
    return (_token_by_symbol or {}).get(symbol.upper())


# ── Market data ──────────────────────────────────────────────────────────────

def get_equity_ohlcv(symbol: str, interval: str = "5minute", days: int = 5) -> list:
    token = resolve_token(symbol)
    if not token:
        logger.warning(f"No instrument token for {symbol}")
        return []
    return get_ohlcv(token, interval, days)


def get_equity_ltp(symbols: list[str]) -> dict:
    """Returns {SYMBOL: ltp}. Accepts bare NSE symbols."""
    keys = [f"NSE:{s.upper()}" for s in symbols]
    try:
        data = get_kite().ltp(keys)
        return {k.split(":", 1)[1]: v["last_price"] for k, v in data.items()}
    except Exception as e:
        logger.error(f"get_equity_ltp failed: {e}")
        return {}


# ── Capital / positions ────────────────────────────────────────────────────────

def get_equity_capital() -> float:
    """Live available equity-segment cash (shared ₹1L Kite wallet)."""
    from trader.config import EQUITY_CAPITAL_INR
    try:
        return float(get_kite().margins()["equity"]["net"])
    except Exception as e:
        logger.error(f"get_equity_capital failed: {e}")
        return EQUITY_CAPITAL_INR


def get_equity_positions() -> list:
    """Open NSE MIS positions (non-zero net qty)."""
    try:
        data = get_kite().positions()
        out = []
        for p in data.get("net", []):
            if p.get("exchange") == "NSE" and int(p.get("quantity", 0)) != 0:
                out.append({
                    "symbol":        p["tradingsymbol"],
                    "quantity":      int(p["quantity"]),
                    "average_price": float(p.get("average_price", 0)),
                    "last_price":    float(p.get("last_price", 0)),
                    "pnl":           float(p.get("pnl", 0)),
                    "product":       p.get("product"),
                })
        return out
    except Exception as e:
        logger.error(f"get_equity_positions failed: {e}")
        return []


# ── Order helpers ────────────────────────────────────────────────────────────

def _wait_for_fill(order_id: str, timeout_s: int = 12):
    """Poll order history until COMPLETE. Returns avg fill price, or None."""
    kite = get_kite()
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            hist = kite.order_history(order_id)
            if hist:
                last = hist[-1]
                status = last.get("status")
                if status == "COMPLETE":
                    return float(last.get("average_price") or 0) or None
                if status in ("REJECTED", "CANCELLED"):
                    logger.warning(f"Entry {order_id} {status}: {last.get('status_message')}")
                    return None
        except Exception as e:
            logger.warning(f"order_history poll failed for {order_id}: {e}")
        time.sleep(1.5)
    logger.warning(f"Entry {order_id} not COMPLETE within {timeout_s}s")
    return None


def cancel_order(order_id: str) -> bool:
    try:
        get_kite().cancel_order(get_kite().VARIETY_REGULAR, order_id)
        return True
    except Exception as e:
        logger.warning(f"cancel_order failed for {order_id}: {e}")
        return False


def place_equity_order(symbol, direction, quantity, sl_price):
    """
    Atomic intraday entry + protective SL-M stop.
      direction: 'long' (BUY entry, SELL stop) or 'short' (SELL entry, BUY stop)
    Returns {entry_order_id, sl_order_id, fill_price} or None on any failure
    (with the entry rolled back if the stop could not be placed).
    """
    kite = get_kite()
    sym = symbol.upper()
    entry_side = kite.TRANSACTION_TYPE_BUY if direction == "long" else kite.TRANSACTION_TYPE_SELL
    exit_side  = kite.TRANSACTION_TYPE_SELL if direction == "long" else kite.TRANSACTION_TYPE_BUY

    ltp = get_equity_ltp([sym]).get(sym)
    if not ltp:
        logger.error(f"No LTP for {sym} — cannot price marketable limit entry")
        return None
    # Marketable LIMIT: buy slightly above / sell slightly below the touch.
    entry_limit = _round_tick(sym, ltp * (1 + ENTRY_BAND) if direction == "long"
                              else ltp * (1 - ENTRY_BAND))

    # 1) Entry — MIS marketable LIMIT (plain MARKET is blocked by the Kite API)
    try:
        entry_id = kite.place_order(
            kite.VARIETY_REGULAR,
            tradingsymbol=sym, exchange=kite.EXCHANGE_NSE,
            transaction_type=entry_side, quantity=int(quantity),
            order_type=kite.ORDER_TYPE_LIMIT, price=entry_limit,
            product=kite.PRODUCT_MIS, validity=kite.VALIDITY_DAY,
        )
    except Exception as e:
        logger.error(f"Entry order failed for {sym}: {type(e).__name__}: {e}")
        return None

    fill = _wait_for_fill(entry_id)
    if not fill:
        # Entry never confirmed filled — try to cancel; do not place a stop.
        cancel_order(entry_id)
        return None

    # 2) Protective stop — stop-LIMIT (SL) on the opposite side, MIS. SL-M is also
    #    rejected by the API as a market order, so we use SL with the limit set
    #    through the trigger (it fills like a market stop on liquid names).
    sl_trigger = _round_tick(sym, sl_price)
    sl_limit = _round_tick(sym, sl_trigger * (1 - EXIT_BAND)) if exit_side == kite.TRANSACTION_TYPE_SELL \
        else _round_tick(sym, sl_trigger * (1 + EXIT_BAND))
    try:
        sl_id = kite.place_order(
            kite.VARIETY_REGULAR,
            tradingsymbol=sym, exchange=kite.EXCHANGE_NSE,
            transaction_type=exit_side, quantity=int(quantity),
            order_type=kite.ORDER_TYPE_SL, price=sl_limit,
            trigger_price=sl_trigger, product=kite.PRODUCT_MIS,
            validity=kite.VALIDITY_DAY,
        )
    except Exception as e:
        logger.error(f"SL placement failed for {sym}: {type(e).__name__}: {e} — rolling back entry")
        _market_exit(sym, exit_side, quantity)
        return None

    logger.info(f"Equity order placed: {sym} {direction} qty={quantity} fill={fill} "
                f"SL@{sl_trigger} | entry={entry_id} sl={sl_id}")
    return {"entry_order_id": entry_id, "sl_order_id": sl_id, "fill_price": fill}


def _exit_limit(sym, exit_side):
    """Marketable LIMIT price that fills like a market exit, within EXIT_BAND."""
    kite = get_kite()
    ltp = get_equity_ltp([sym]).get(sym)
    if not ltp:
        return None
    # Exiting via BUY → price up through; via SELL → price down through.
    if exit_side == kite.TRANSACTION_TYPE_BUY:
        return _round_tick(sym, ltp * (1 + EXIT_BAND))
    return _round_tick(sym, ltp * (1 - EXIT_BAND))


def _market_exit(sym, exit_side, quantity):
    kite = get_kite()
    price = _exit_limit(sym, exit_side)
    try:
        kite.place_order(
            kite.VARIETY_REGULAR,
            tradingsymbol=sym, exchange=kite.EXCHANGE_NSE,
            transaction_type=exit_side, quantity=int(quantity),
            order_type=kite.ORDER_TYPE_LIMIT, price=price,
            product=kite.PRODUCT_MIS, validity=kite.VALIDITY_DAY,
        )
        logger.warning(f"Rolled back / exited {sym} @ limit {price}")
    except Exception as e:
        logger.error(f"Market exit failed for {sym}: {e}")


def exit_equity_position(symbol, quantity, sl_order_id=None) -> bool:
    """Square off a position (market) and cancel its resting SL-M order."""
    sym = symbol.upper()
    kite = get_kite()
    # quantity>0 → long → SELL to exit; quantity<0 → short → BUY to exit
    exit_side = kite.TRANSACTION_TYPE_SELL if quantity > 0 else kite.TRANSACTION_TYPE_BUY
    if sl_order_id:
        cancel_order(sl_order_id)
    price = _exit_limit(sym, exit_side)
    if not price:
        logger.error(f"exit_equity_position: no LTP for {sym} — cannot price exit")
        return False
    try:
        kite.place_order(
            kite.VARIETY_REGULAR,
            tradingsymbol=sym, exchange=kite.EXCHANGE_NSE,
            transaction_type=exit_side, quantity=abs(int(quantity)),
            order_type=kite.ORDER_TYPE_LIMIT, price=price,
            product=kite.PRODUCT_MIS, validity=kite.VALIDITY_DAY,
        )
        logger.info(f"Exited equity position: {sym} @ limit {price}")
        return True
    except Exception as e:
        logger.error(f"exit_equity_position failed for {sym}: {e}")
        return False
