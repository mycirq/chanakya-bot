import requests
import logging
import urllib.parse

# Ticker suffix map for Indian stocks on Yahoo Finance
INDIA_SUFFIX = ".NS"

MARKET_CURRENCY = {
    "india": ("₹", "INR"),
    "us": ("$", "USD"),
    "crypto": ("$", "USD"),
    "realestate": ("₹", "INR"),
}


def get_price(ticker, market):
    """
    Returns (price: float, display: str, currency: str) or (None, None, None) on failure.
    """
    # Strip spaces — users type "TATA MOTORS" but Yahoo needs "TATAMOTORS"
    ticker = ticker.replace(" ", "")

    try:
        if market == "india":
            return _get_yfinance_price(
                ticker if "." in ticker else f"{ticker}{INDIA_SUFFIX}",
                "₹"
            )

        elif market == "us":
            return _get_yfinance_price(ticker, "$")

        elif market == "crypto":
            return _get_crypto_price(ticker)

        elif market == "realestate":
            return None, None, None

    except Exception as e:
        logging.error(f"Price fetch error for {ticker} ({market}): {e}")

    return None, None, None


def _get_yfinance_price(symbol, currency_symbol):
    import yfinance as yf
    ticker = yf.Ticker(symbol)
    price = ticker.fast_info.last_price
    if price and price > 0:
        return float(price), f"{currency_symbol}{price:,.2f}", currency_symbol
    return None, None, None


def _get_crypto_price(ticker):
    """Try CoinGecko by ID first, fallback to symbol search."""
    ticker_lower = ticker.lower()

    # Direct ID lookup
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ticker_lower}&vs_currencies=usd"
    resp = requests.get(url, timeout=10)
    data = resp.json()

    if ticker_lower in data:
        price = data[ticker_lower]["usd"]
        return float(price), f"${price:,.4f}", "$"

    # Fallback: search by symbol
    search_url = f"https://api.coingecko.com/api/v3/search?query={ticker_lower}"
    search_resp = requests.get(search_url, timeout=10)
    search_data = search_resp.json()

    coins = search_data.get("coins", [])
    if coins:
        coin_id = coins[0]["id"]
        price_url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd"
        price_resp = requests.get(price_url, timeout=10)
        price_data = price_resp.json()
        if coin_id in price_data:
            price = price_data[coin_id]["usd"]
            return float(price), f"${price:,.4f}", "$"

    return None, None, None


def search_tickers(query, market):
    """
    Search Yahoo Finance for matching tickers.
    Returns list of {"text": "...", "value": "..."} for Slack external_select.
    """
    if not query or len(query) < 1:
        return []

    try:
        if market == "crypto":
            return _search_crypto(query)

        encoded = urllib.parse.quote(query)
        url = f"https://query1.finance.yahoo.com/v1/finance/search?q={encoded}&quotesCount=10&newsCount=0&enableFuzzyQuery=false"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=8)
        data = resp.json()

        results = []
        for quote in data.get("quotes", []):
            symbol = quote.get("symbol", "")
            name = quote.get("shortname") or quote.get("longname") or symbol
            q_type = quote.get("quoteType", "")

            if market == "india":
                if not (symbol.endswith(".NS") or symbol.endswith(".BO")):
                    continue
                # Skip mutual funds / entries with no real name
                if name == symbol or q_type not in ("EQUITY", "ETF", "INDEX"):
                    continue
            elif market == "us":
                if "." in symbol:
                    continue
                if q_type not in ("EQUITY", "ETF"):
                    continue

            label = f"{name} ({symbol})"
            if len(label) > 75:
                label = label[:72] + "..."
            results.append({"text": label, "value": symbol})

            if len(results) >= 8:
                break

        return results

    except Exception as e:
        logging.warning(f"Ticker search failed for '{query}': {e}")
        return []


def _search_crypto(query):
    try:
        url = f"https://api.coingecko.com/api/v3/search?query={urllib.parse.quote(query)}"
        resp = requests.get(url, timeout=8)
        coins = resp.json().get("coins", [])
        return [
            {"text": f"{c['name']} ({c['symbol'].upper()})", "value": c["id"]}
            for c in coins[:8]
        ]
    except Exception as e:
        logging.warning(f"Crypto search failed: {e}")
        return []
