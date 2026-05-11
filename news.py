import feedparser
import requests
import logging
from datetime import datetime, timezone

# ── RSS feed sources per market ───────────────────────────────────────────────

FEEDS = {
    "india": [
        ("Economic Times Markets", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
        ("LiveMint Markets",       "https://www.livemint.com/rss/markets"),
        ("Business Standard",      "https://www.business-standard.com/rss/markets-106.rss"),
    ],
    "us": [
        ("Reuters Business",  "https://feeds.reuters.com/reuters/businessNews"),
        ("MarketWatch",       "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines"),
        ("Seeking Alpha",     "https://seekingalpha.com/market_currents.xml"),
    ],
    "crypto": [
        ("CoinDesk",  "https://www.coindesk.com/arc/outboundfeeds/rss/"),
        ("Decrypt",   "https://decrypt.co/feed"),
    ],
    "realestate": [
        ("ET Realty",    "https://realty.economictimes.indiatimes.com/rss/topstories"),
        ("Housing News", "https://housing.com/news/feed/"),
    ],
}


def fetch_news(market, max_items=5):
    """
    Fetch latest news articles for a market.
    Returns list of dicts: {title, url, source, summary, published}
    """
    feeds = FEEDS.get(market, [])
    articles = []

    for source_name, url in feeds:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:max_items]:
                title = (entry.get("title") or "").strip()
                link  = (entry.get("link") or entry.get("id") or "").strip()
                summary = _clean(entry.get("summary") or entry.get("description") or "")
                published = entry.get("published") or entry.get("updated") or ""

                if title and link:
                    articles.append({
                        "title":     title,
                        "url":       link,
                        "source":    source_name,
                        "summary":   summary[:200] + "..." if len(summary) > 200 else summary,
                        "published": published,
                    })
        except Exception as e:
            logging.warning(f"Failed to fetch {source_name} feed: {e}")

    # Also fetch CryptoPanic for crypto (free, no key needed)
    if market == "crypto":
        articles += _fetch_cryptopanic()

    # Deduplicate by URL
    seen = set()
    unique = []
    for a in articles:
        if a["url"] not in seen:
            seen.add(a["url"])
            unique.append(a)

    return unique[:max_items]


def _fetch_cryptopanic():
    try:
        resp = requests.get(
            "https://cryptopanic.com/api/free/v1/posts/?kind=news&public=true",
            timeout=8
        )
        data = resp.json()
        results = []
        for item in data.get("results", [])[:5]:
            results.append({
                "title":     item.get("title", "").strip(),
                "url":       item.get("url", "").strip(),
                "source":    item.get("source", {}).get("title", "CryptoPanic"),
                "summary":   "",
                "published": item.get("published_at", ""),
            })
        return results
    except Exception as e:
        logging.warning(f"CryptoPanic fetch failed: {e}")
        return []


def _clean(html):
    """Strip basic HTML tags from summary."""
    import re
    return re.sub(r"<[^>]+>", "", html).strip()
