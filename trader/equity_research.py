"""
Overnight equity research engine — builds tomorrow's intraday watchlist.

Runs in the evening (after market close). Pulls Indian market news, matches
headlines to the NSE F&O stock universe, scores by mention frequency + a simple
sentiment lexicon, and stores the top names (with a directional bias + thesis) in
equity_watchlist for the next trading session. This watchlist is the ENTRY GATE —
the market-hours engine only trades these names. No watchlist → no trading.
"""
import re
import logging
import feedparser
from datetime import datetime, timedelta

from trader.config import IST, EQUITY_WATCHLIST_SIZE
from db import get_conn

logger = logging.getLogger(__name__)

# Indian market news RSS feeds.
INDIA_FEEDS = [
    ("ET Markets",        "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("ET Stocks",         "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms"),
    ("LiveMint Markets",  "https://www.livemint.com/rss/markets"),
    ("LiveMint Companies", "https://www.livemint.com/rss/companies"),
    ("Business Standard", "https://www.business-standard.com/rss/markets-106.rss"),
    ("Moneycontrol Business", "https://www.moneycontrol.com/rss/business.xml"),
    ("Moneycontrol Markets",  "https://www.moneycontrol.com/rss/marketreports.xml"),
]

# Index underlyings to exclude from the stock universe.
_INDEX_NAMES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50",
                "SENSEX", "BANKEX", "NIFTYIT", "NIFTYINFRA"}

_POS = {"surge", "surges", "jump", "jumps", "gain", "gains", "rise", "rises", "profit",
        "beat", "beats", "upgrade", "upgraded", "record", "high", "bullish", "rally",
        "rallies", "wins", "win", "bags", "order", "orders", "approval", "approved",
        "soar", "soars", "outperform", "buy", "strong", "robust", "expansion", "stake"}
_NEG = {"fall", "falls", "drop", "drops", "decline", "declines", "loss", "losses",
        "downgrade", "downgraded", "fraud", "probe", "ban", "banned", "recall", "cut",
        "cuts", "miss", "misses", "slump", "slumps", "bearish", "sell", "weak", "plunge",
        "plunges", "lawsuit", "penalty", "fine", "raid", "default", "warning", "stake sale"}

_universe = None  # {symbol: set(aliases)}


def _build_universe():
    """F&O stock underlyings tradable on NSE cash, with company-name aliases."""
    global _universe
    if _universe is not None:
        return _universe
    from trader.kite import get_nfo_instruments
    from trader.equity_kite import get_equity_instruments

    fno = get_nfo_instruments()
    underlyings = {
        i["name"] for i in fno
        if i.get("instrument_type") == "FUT" and i.get("name") not in _INDEX_NAMES
    }
    eq_names = {i["tradingsymbol"]: (i.get("name") or "") for i in get_equity_instruments()}

    uni = {}
    for sym in underlyings:
        if sym not in eq_names:
            continue
        aliases = {sym.upper()}
        nm = eq_names[sym].upper().strip()
        if len(nm) >= 4:
            aliases.add(nm)
        uni[sym] = aliases
    _universe = uni
    logger.info(f"Equity research universe: {len(uni)} F&O stocks")
    return uni


def _fetch_headlines(max_per_feed=40):
    items = []
    for source, url in INDIA_FEEDS:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:max_per_feed]:
                title = (e.get("title") or "").strip()
                summary = re.sub(r"<[^>]+>", "", e.get("summary") or e.get("description") or "")
                if title:
                    items.append({"source": source, "title": title, "summary": summary[:300]})
        except Exception as ex:
            logger.warning(f"Feed failed {source}: {ex}")
    logger.info(f"Fetched {len(items)} India headlines")
    return items


def _sentiment(text: str) -> int:
    words = set(re.findall(r"[a-z]+", text.lower()))
    return len(words & _POS) - len(words & _NEG)


def next_trading_date():
    d = datetime.now(IST).date() + timedelta(days=1)
    while d.weekday() >= 5:  # skip Sat/Sun (holidays not handled)
        d += timedelta(days=1)
    return d


def build_watchlist():
    """Compute the ranked watchlist. Returns list of dicts (not yet stored)."""
    uni = _build_universe()
    if not uni:
        logger.warning("Empty universe — cannot build watchlist")
        return []
    headlines = _fetch_headlines()

    agg = {}  # symbol -> {mentions, sentiment, hits:[(headline, source)]}
    for item in headlines:
        text = f"{item['title']} {item['summary']}"
        upper = text.upper()
        senti = _sentiment(text)
        for sym, aliases in uni.items():
            matched = False
            for a in aliases:
                # word-boundary match to avoid substring false positives
                if re.search(rf"\b{re.escape(a)}\b", upper):
                    matched = True
                    break
            if matched:
                rec = agg.setdefault(sym, {"mentions": 0, "sentiment": 0, "hits": []})
                rec["mentions"] += 1
                rec["sentiment"] += senti
                if len(rec["hits"]) < 3:
                    rec["hits"].append((item["title"][:90], item["source"]))

    ranked = []
    for sym, rec in agg.items():
        senti = rec["sentiment"]
        bias = "long" if senti > 0 else "short" if senti < 0 else "neutral"
        score = min(rec["mentions"] * 15 + abs(senti) * 10, 100)
        headline = rec["hits"][0][0] if rec["hits"] else ""
        src = rec["hits"][0][1] if rec["hits"] else ""
        thesis = (f"{rec['mentions']} mention(s), net sentiment {senti:+d} → {bias.upper()}. "
                  f"Top: \"{headline}\" ({src})")
        ranked.append({
            "symbol": sym, "bias": bias, "score": score, "thesis": thesis,
            "sources": "; ".join(f"{h[0]} [{h[1]}]" for h in rec["hits"]),
            "mentions": rec["mentions"],
        })

    # Prefer directional (non-neutral) names, then more mentions, then score.
    ranked.sort(key=lambda x: (x["bias"] != "neutral", x["mentions"], x["score"]), reverse=True)
    return ranked[:EQUITY_WATCHLIST_SIZE]


def _store_watchlist(trade_date, picks):
    conn = get_conn()
    try:
        is_pg = hasattr(conn, "cursor")
        cur = conn.cursor() if is_pg else conn
        for p in picks:
            if is_pg:
                cur.execute("""
                    INSERT INTO equity_watchlist (trade_date, symbol, bias, score, thesis, sources)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (trade_date, symbol) DO UPDATE SET
                      bias=EXCLUDED.bias, score=EXCLUDED.score,
                      thesis=EXCLUDED.thesis, sources=EXCLUDED.sources
                """, (trade_date, p["symbol"], p["bias"], p["score"], p["thesis"], p["sources"]))
            else:
                cur.execute("""
                    INSERT OR REPLACE INTO equity_watchlist
                    (trade_date, symbol, bias, score, thesis, sources)
                    VALUES (?,?,?,?,?,?)
                """, (str(trade_date), p["symbol"], p["bias"], p["score"], p["thesis"], p["sources"]))
        conn.commit()
        if is_pg:
            cur.close()
    finally:
        conn.close()


def get_watchlist(trade_date) -> list:
    """Read stored watchlist for a date → [{symbol, bias, score, thesis}]."""
    conn = get_conn()
    try:
        if hasattr(conn, "cursor"):
            from psycopg2.extras import RealDictCursor
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT * FROM equity_watchlist WHERE trade_date=%s ORDER BY score DESC", (trade_date,))
            rows = [dict(r) for r in cur.fetchall()]
            cur.close()
        else:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM equity_watchlist WHERE trade_date=? ORDER BY score DESC",
                (str(trade_date),)).fetchall()]
        return rows
    finally:
        conn.close()


def run_research(app=None):
    """Build + store + post tomorrow's watchlist. Returns the picks."""
    trade_date = next_trading_date()
    picks = build_watchlist()
    if not picks:
        logger.warning("Research produced no watchlist names")
        if app:
            from trader.equity_reporter import post_equity_watchlist
            post_equity_watchlist(app.client, trade_date, [])
        return []
    _store_watchlist(trade_date, picks)
    logger.info(f"Watchlist for {trade_date}: {[p['symbol'] for p in picks]}")
    if app:
        from trader.equity_reporter import post_equity_watchlist
        post_equity_watchlist(app.client, trade_date, picks)
    return picks
