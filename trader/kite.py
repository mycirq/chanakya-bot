"""
KiteConnect wrapper — authentication, market data, order placement.
All FnO operations go through here.
"""
import os
import logging
import requests
import urllib.parse
from datetime import datetime, date, timedelta
from kiteconnect import KiteConnect

from trader.config import IST, KITE_INDICES

logger = logging.getLogger(__name__)

_kite: KiteConnect | None = None
_instruments_cache: list | None = None


# ── Singleton ──────────────────────────────────────────────────────────────────

def get_kite() -> KiteConnect:
    """Returns authenticated KiteConnect instance using stored token."""
    global _kite
    if _kite is None:
        _kite = KiteConnect(api_key=os.environ["KITE_API_KEY"])
    token = _get_stored_token()
    if token:
        _kite.set_access_token(token)
    return _kite


# ── Token storage ──────────────────────────────────────────────────────────────

def _get_stored_token() -> str | None:
    from db import get_conn
    try:
        conn = get_conn()
        if hasattr(conn, "cursor"):
            from psycopg2.extras import RealDictCursor
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT value FROM kite_config WHERE key = 'access_token'")
            row = cur.fetchone()
            cur.execute("SELECT value FROM kite_config WHERE key = 'token_date'")
            date_row = cur.fetchone()
            cur.close()
        else:
            row = conn.execute(
                "SELECT value FROM kite_config WHERE key = 'access_token'"
            ).fetchone()
            date_row = conn.execute(
                "SELECT value FROM kite_config WHERE key = 'token_date'"
            ).fetchone()
            row = dict(row) if row else None
            date_row = dict(date_row) if date_row else None
        conn.close()

        if not row or not date_row:
            return None

        token_date = date_row["value"]  # YYYY-MM-DD
        now_ist = datetime.now(IST)
        today_str = now_ist.strftime("%Y-%m-%d")

        # Token valid same day until midnight; Kite invalidates at 6 AM next day
        if token_date < today_str:
            return None
        if token_date == today_str and now_ist.hour >= 6 and now_ist.date() > date.fromisoformat(token_date):
            return None

        return row["value"]
    except Exception as e:
        logger.error(f"_get_stored_token failed: {e}")
        return None


def _store_token(access_token: str):
    from db import get_conn
    today = datetime.now(IST).strftime("%Y-%m-%d")
    try:
        conn = get_conn()
        if hasattr(conn, "cursor"):
            cur = conn.cursor()
            for k, v in [("access_token", access_token), ("token_date", today)]:
                cur.execute("""
                    INSERT INTO kite_config (key, value, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
                """, (k, v))
            conn.commit(); cur.close()
        else:
            for k, v in [("access_token", access_token), ("token_date", today)]:
                conn.execute("""
                    INSERT OR REPLACE INTO kite_config (key, value, updated_at)
                    VALUES (?, ?, datetime('now'))
                """, (k, v))
            conn.commit()
        conn.close()
        logger.info("Kite access token stored")
    except Exception as e:
        logger.error(f"_store_token failed: {e}")


# ── Login ──────────────────────────────────────────────────────────────────────

def auto_login() -> str:
    """Auto-login using stored TOTP secret — no user input needed."""
    import pyotp
    secret = os.environ.get("KITE_TOTP_SECRET", "")
    if not secret:
        raise Exception("KITE_TOTP_SECRET not set in env vars")
    totp = pyotp.TOTP(secret).now()
    logger.info(f"Auto-generated TOTP for login")
    return login_with_totp(totp)


def login_with_totp(totp_value: str) -> str:
    """
    Full automated login flow using TOTP.
    Returns access_token and stores it in DB.
    """
    api_key    = os.environ["KITE_API_KEY"]
    api_secret = os.environ["KITE_API_SECRET"]
    user_id    = os.environ["KITE_USER_ID"]
    password   = os.environ["KITE_PASSWORD"]

    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    })

    # Step 1: Login — do NOT visit connect/login first
    login_resp = sess.post("https://kite.zerodha.com/api/login", data={
        "user_id":  user_id,
        "password": password,
    })
    login_data = login_resp.json()
    logger.info(f"Kite login response: {login_data.get('status')} {login_data.get('message','')}")
    if login_data.get("status") != "success":
        raise Exception(f"Kite login failed: {login_data.get('message', login_data)}")
    request_id = login_data["data"]["request_id"]
    # Use whatever 2FA type the account has configured (totp, app_code, etc.)
    twofa_type = login_data["data"].get("twofa_type", "totp")
    logger.info(f"Kite twofa_type from login response: {twofa_type}")

    # Step 2: Submit TOTP
    twofa_resp = sess.post("https://kite.zerodha.com/api/twofa", data={
        "user_id":     user_id,
        "request_id":  request_id,
        "twofa_value": str(totp_value).strip(),
        "twofa_type":  twofa_type,
    })
    twofa_data = twofa_resp.json()
    logger.info(f"Kite 2FA response: {twofa_data.get('status')} {twofa_data.get('message','')}")
    if twofa_data.get("status") != "success":
        raise Exception(f"Kite TOTP failed: {twofa_data.get('message', twofa_data)}")

    # Step 3: Follow connect/login redirect chain to extract request_token
    # Zerodha redirects through several hops before hitting the redirect_url
    base_url = "https://kite.zerodha.com"
    url = f"{base_url}/connect/login?v=3&api_key={api_key}"
    request_token = None

    for hop in range(10):
        r = sess.get(url, allow_redirects=False)
        status = r.status_code
        location = r.headers.get("Location", "")

        # Resolve relative redirects
        if location and location.startswith("/"):
            location = base_url + location

        logger.info(f"Hop {hop}: status={status} url={url[:80]} → location={location[:80] if location else 'none'}")

        # Check if request_token is in the redirect target
        if location and "request_token" in location:
            parsed = urllib.parse.urlparse(location)
            request_token = urllib.parse.parse_qs(parsed.query).get("request_token", [None])[0]
            break

        # Check if request_token landed in the current URL (shouldn't happen but guard)
        if "request_token" in url:
            parsed = urllib.parse.urlparse(url)
            request_token = urllib.parse.parse_qs(parsed.query).get("request_token", [None])[0]
            break

        if not location:
            # Check if this is the authorize/consent page — auto-submit it
            if "connect/authorize" in url:
                logger.info("Hit authorize/consent page — trying to auto-authorize")
                parsed_url = urllib.parse.urlparse(url)
                url_params = urllib.parse.parse_qs(parsed_url.query)
                sess_id    = url_params.get("sess_id", [None])[0]

                body = r.text
                logger.info(f"Authorize page body snippet: {body[200:600]}")

                # Try common patterns to find the form action endpoint
                import re
                # Look for action="..." in form tags
                form_action = re.search(r'action=["\']([^"\']+)["\']', body)
                # Look for fetch/axios calls to an API endpoint in JS
                api_endpoint = re.search(r'["\'](/connect/[^"\'?]+)["\']', body)

                # Try methods in order: GET with action=allow, PUT, then parse form
                endpoints_to_try = [
                    ("GET", f"{url}&action=allow"),
                    ("GET", f"{url}&skip=0&allow=1"),
                ]
                if form_action:
                    act = form_action.group(1)
                    if act.startswith("/"):
                        act = base_url + act
                    endpoints_to_try.insert(0, ("POST", act))

                for method, endpoint in endpoints_to_try:
                    if method == "GET":
                        ar = sess.get(endpoint, allow_redirects=False)
                    else:
                        ar = sess.post(endpoint, data={"api_key": api_key, "sess_id": sess_id}, allow_redirects=False)
                    loc = ar.headers.get("Location", "")
                    if loc and loc.startswith("/"):
                        loc = base_url + loc
                    logger.info(f"Auth attempt {method} {endpoint[:70]} → {ar.status_code} loc={loc[:60] if loc else 'none'}")
                    if loc and "request_token" in loc:
                        parsed = urllib.parse.urlparse(loc)
                        request_token = urllib.parse.parse_qs(parsed.query).get("request_token", [None])[0]
                        break
                    if loc:
                        url = loc
                        break
                if request_token:
                    break
            else:
                logger.error(f"Redirect chain stopped at hop {hop} with no Location. Body: {r.text[:300]}")
            break

        url = location

    if not request_token:
        raise Exception(f"Could not get request_token after following redirects. Check logs for details.")

    logger.info(f"Got request_token: {request_token[:10]}...")

    # Step 4: Generate access token
    kite = KiteConnect(api_key=api_key)
    session_data = kite.generate_session(request_token, api_secret=api_secret)
    access_token = session_data["access_token"]

    _store_token(access_token)

    # Update singleton
    global _kite
    if _kite is None:
        _kite = KiteConnect(api_key=api_key)
    _kite.set_access_token(access_token)

    logger.info("Kite login successful — access token stored")
    return access_token


def is_authorized() -> bool:
    return _get_stored_token() is not None


# ── Market hours ───────────────────────────────────────────────────────────────

def is_market_open() -> bool:
    """True if NSE FnO market is open (Mon–Fri, 9:15–15:25 IST)."""
    now = datetime.now(IST)
    if now.weekday() >= 5:          # Saturday=5, Sunday=6
        return False
    h, m = now.hour, now.minute
    minutes = h * 60 + m
    open_min  = 9 * 60 + 15
    close_min = 15 * 60 + 25
    return open_min <= minutes <= close_min


# ── Market data ────────────────────────────────────────────────────────────────

def get_ltp(quote_keys: list[str]) -> dict:
    """Returns {exchange:symbol -> ltp} for given list."""
    try:
        data = get_kite().ltp(quote_keys)
        return {k: v["last_price"] for k, v in data.items()}
    except Exception as e:
        logger.error(f"get_ltp failed: {e}")
        return {}


def get_ohlcv(instrument_token: int, interval: str = "5minute", days: int = 5) -> list:
    """
    Returns OHLCV as [[timestamp_ms, open, high, low, close, volume], ...]
    Compatible with compute_indicators() in strategy.py.
    """
    try:
        to_date   = datetime.now(IST)
        from_date = to_date - timedelta(days=days)
        candles   = get_kite().historical_data(
            instrument_token, from_date, to_date, interval, continuous=False
        )
        return [
            [int(c["date"].timestamp() * 1000),
             c["open"], c["high"], c["low"], c["close"], c["volume"]]
            for c in candles
        ]
    except Exception as e:
        logger.error(f"get_ohlcv failed for token {instrument_token}: {e}")
        return []


# ── Instruments ────────────────────────────────────────────────────────────────

def get_nfo_instruments() -> list:
    """Download and cache NFO instrument list."""
    global _instruments_cache
    if _instruments_cache is None:
        try:
            _instruments_cache = get_kite().instruments("NFO")
            logger.info(f"Loaded {len(_instruments_cache)} NFO instruments")
        except Exception as e:
            logger.error(f"get_nfo_instruments failed: {e}")
            return []
    return _instruments_cache


def get_nearest_weekly_expiry(underlying: str) -> date:
    """
    Returns nearest weekly expiry (Thursday).
    If today is Thursday after 14:30 IST, returns next Thursday.
    """
    now = datetime.now(IST)
    today = now.date()
    days_to_thursday = (3 - today.weekday()) % 7  # Thursday=3
    nearest = today + timedelta(days=days_to_thursday)

    # If it's Thursday and market is almost done, use next week
    if days_to_thursday == 0 and now.hour >= 14 and now.minute >= 30:
        nearest = today + timedelta(days=7)

    return nearest


def find_option(underlying: str, strike: float, option_type: str, expiry: date):
    """
    Find NFO option instrument.
    Returns (tradingsymbol, instrument_token, lot_size) or None.
    option_type: 'CE' or 'PE'
    """
    instruments = get_nfo_instruments()
    for inst in instruments:
        if (inst.get("name") == underlying
                and inst.get("instrument_type") == option_type
                and inst.get("expiry") == expiry
                and abs(float(inst.get("strike", 0)) - strike) < 0.01):
            return (
                inst["tradingsymbol"],
                inst["instrument_token"],
                int(inst.get("lot_size", KITE_INDICES.get(underlying, {}).get("lot_size", 25))),
            )
    return None


# ── Orders ─────────────────────────────────────────────────────────────────────

def place_option_order(tradingsymbol: str, transaction_type: str,
                       quantity: int, order_type: str = "MARKET",
                       price: float = 0.0) -> str | None:
    """
    Place NFO option order.
    transaction_type: 'BUY' or 'SELL'
    quantity: number of lots * lot_size (total contracts)
    Returns order_id or None.
    """
    kite = get_kite()
    try:
        params = {
            "tradingsymbol": tradingsymbol,
            "exchange":      kite.EXCHANGE_NFO,
            "transaction_type": transaction_type,
            "quantity":      quantity,
            "order_type":    order_type,
            "product":       kite.PRODUCT_MIS,   # intraday
            "validity":      kite.VALIDITY_DAY,
        }
        if order_type == "LIMIT" and price:
            params["price"] = price

        order_id = kite.place_order(kite.VARIETY_REGULAR, **params)
        logger.info(f"Option order placed: {tradingsymbol} {transaction_type} qty={quantity} | order_id={order_id}")
        return order_id
    except Exception as e:
        logger.error(f"place_option_order failed for {tradingsymbol}: {e}")
        return None


def get_open_positions() -> list:
    """Returns open NFO positions (non-zero net qty)."""
    try:
        data = get_kite().positions()
        positions = []
        for p in data.get("net", []):
            if p.get("exchange") == "NFO" and int(p.get("quantity", 0)) != 0:
                positions.append({
                    "tradingsymbol": p["tradingsymbol"],
                    "quantity":      int(p["quantity"]),
                    "average_price": float(p.get("average_price", 0)),
                    "last_price":    float(p.get("last_price", 0)),
                    "pnl":           float(p.get("pnl", 0)),
                    "product":       p.get("product"),
                })
        return positions
    except Exception as e:
        logger.error(f"get_open_positions failed: {e}")
        return []


def exit_position(tradingsymbol: str, quantity: int) -> bool:
    """Market square-off a position."""
    try:
        kite = get_kite()
        # Determine side: positive qty = long (need to sell), negative = short (need to buy)
        side = kite.TRANSACTION_TYPE_SELL if quantity > 0 else kite.TRANSACTION_TYPE_BUY
        kite.place_order(kite.VARIETY_REGULAR,
                         tradingsymbol=tradingsymbol,
                         exchange=kite.EXCHANGE_NFO,
                         transaction_type=side,
                         quantity=abs(quantity),
                         order_type=kite.ORDER_TYPE_MARKET,
                         product=kite.PRODUCT_MIS,
                         validity=kite.VALIDITY_DAY)
        logger.info(f"Exited position: {tradingsymbol}")
        return True
    except Exception as e:
        logger.error(f"exit_position failed for {tradingsymbol}: {e}")
        return False
