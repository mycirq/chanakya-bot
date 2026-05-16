import logging
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from db import get_due_suggestions, update_suggestion, is_news_posted, mark_news_posted
from prices import get_price
from news import fetch_news
from trader.engine import run_scan, run_daily_summary
from trader.memory import init_month_snapshot, get_month_snapshot, get_trade_stats
from trader.binance import get_futures_balance
from trader.reporter import post_month_end_summary
from trader.config import MONTHLY_TARGET_PCT

IST = pytz.timezone("Asia/Kolkata")

MARKET_EMOJI    = {"india": "🇮🇳", "us": "🇺🇸", "crypto": "🪙", "realestate": "🏠"}
CURRENCY_SYMBOL = {"india": "₹",   "us": "$",    "crypto": "$",  "realestate": "₹"}

# Channel names → market mapping for news
NEWS_CHANNELS = {
    "india":       "dalal-street-neeti",
    "us":          "wall-street-neeti",
    "crypto":      "crypto-kautilya",
    "realestate":  "realty-rajniti",
}

BRIEFING_LABELS = {
    "morning":   ("🌅", "Morning Briefing"),
    "afternoon": ("☀️",  "Midday Update"),
    "evening":   ("🌆", "Evening Wrap"),
}


# ── News posting ──────────────────────────────────────────────────────────────

def post_news(app, slot):
    """Fetch and post top news to each investment channel."""
    emoji, label = BRIEFING_LABELS[slot]
    logging.info(f"Posting {label} news...")

    # Resolve channel name → channel ID
    try:
        result = app.client.conversations_list(limit=200, types="public_channel")
        channel_map = {c["name"]: c["id"] for c in result["channels"]}
    except Exception as e:
        logging.error(f"Could not fetch channel list: {e}")
        return

    for market, channel_name in NEWS_CHANNELS.items():
        channel_id = channel_map.get(channel_name)
        if not channel_id:
            logging.warning(f"Channel #{channel_name} not found, skipping.")
            continue

        articles = fetch_news(market, max_items=8)
        # Filter already posted
        fresh = [a for a in articles if not is_news_posted(a["url"])][:4]

        if not fresh:
            logging.info(f"No fresh news for {market}, skipping.")
            continue

        market_emoji = MARKET_EMOJI.get(market, "📈")
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{emoji} {label} — {market_emoji} {channel_name.replace('-', ' ').title()}"}
            },
            {"type": "divider"}
        ]

        for i, article in enumerate(fresh, 1):
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{i}. <{article['url']}|{article['title']}>*\n_{article['source']}_"
                            + (f"\n{article['summary']}" if article["summary"] else "")
                }
            })
            if i < len(fresh):
                blocks.append({"type": "divider"})

        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"Auto-posted by Chanakya Bot • {label}"}]
        })

        try:
            app.client.chat_postMessage(
                channel=channel_id,
                text=f"{label} — {market_emoji} Top News",
                blocks=blocks
            )
            mark_news_posted([a["url"] for a in fresh])
            logging.info(f"Posted {len(fresh)} news items to #{channel_name}")
        except Exception as e:
            logging.error(f"Failed to post news to #{channel_name}: {e}")


# ── Review checker ────────────────────────────────────────────────────────────

def check_due_suggestions(app):
    due = get_due_suggestions()
    if not due:
        logging.info("No due suggestions to review.")
        return

    logging.info(f"Reviewing {len(due)} due suggestion(s)...")

    for s in due:
        ticker     = s["ticker"]
        market     = s["market"]
        entry_price = float(s["entry_price"])
        expected_roi = float(s["expected_roi"])
        days       = s["tracking_days"]
        currency   = CURRENCY_SYMBOL.get(market, "")
        emoji      = MARKET_EMOJI.get(market, "📈")

        current_price, price_display, _ = get_price(ticker, market)
        if current_price is None:
            logging.warning(f"Could not fetch price for {ticker}, skipping.")
            continue

        actual_roi = ((current_price - entry_price) / entry_price) * 100
        update_suggestion(s["id"], current_price, actual_roi)

        if actual_roi >= expected_roi:
            status = "🟢 Target Hit!"
        elif actual_roi >= 0:
            status = "🟡 On Track"
        else:
            status = "🔴 In Loss"

        try:
            app.client.chat_postMessage(
                channel=s["channel_id"],
                text=f"{days}-Day Review: {ticker} | {actual_roi:+.2f}%",
                blocks=[
                    {"type": "header", "text": {"type": "plain_text", "text": f"📊 {days}-Day Review: {ticker} {emoji}"}},
                    {
                        "type": "section",
                        "fields": [
                            {"type": "mrkdwn", "text": f"*Entry Price:*\n{currency}{entry_price:,.2f}"},
                            {"type": "mrkdwn", "text": f"*Current Price:*\n{currency}{current_price:,.2f}"},
                            {"type": "mrkdwn", "text": f"*Expected ROI:*\n+{expected_roi:.1f}%"},
                            {"type": "mrkdwn", "text": f"*Actual Gain:*\n{actual_roi:+.2f}%"},
                        ]
                    },
                    {"type": "section", "text": {"type": "mrkdwn", "text": f"*Status:* {status}"}},
                    {"type": "context", "elements": [
                        {"type": "mrkdwn", "text": f"Suggested by <@{s['user_id']}> • Tracked for {days} days"}
                    ]}
                ]
            )
        except Exception as e:
            logging.error(f"Failed to post review for suggestion {s['id']}: {e}")


# ── Scheduler setup ───────────────────────────────────────────────────────────

def start_scheduler(app):
    scheduler = BackgroundScheduler(timezone=IST)

    # News: 8 AM IST
    scheduler.add_job(
        lambda: post_news(app, "morning"),
        CronTrigger(hour=8, minute=0, timezone=IST),
        id="news_morning"
    )
    # News: 1 PM IST
    scheduler.add_job(
        lambda: post_news(app, "afternoon"),
        CronTrigger(hour=13, minute=0, timezone=IST),
        id="news_afternoon"
    )
    # News: 6 PM IST
    scheduler.add_job(
        lambda: post_news(app, "evening"),
        CronTrigger(hour=18, minute=0, timezone=IST),
        id="news_evening"
    )
    # Portfolio review: every 6 hours
    scheduler.add_job(
        lambda: check_due_suggestions(app),
        "interval", hours=6,
        id="review_job"
    )

    # Crypto futures scan: every 15 minutes
    scheduler.add_job(
        lambda: run_scan(app),
        "interval", minutes=15,
        id="crypto_scan"
    )

    # Daily trading summary: 9 PM IST
    scheduler.add_job(
        lambda: run_daily_summary(app),
        CronTrigger(hour=21, minute=0, timezone=IST),
        id="daily_summary"
    )

    # Month start (1st of each month, 6 AM IST): snapshot starting balance
    scheduler.add_job(
        lambda: init_month_snapshot(get_futures_balance(), MONTHLY_TARGET_PCT),
        CronTrigger(day=1, hour=6, minute=0, timezone=IST),
        id="month_start_snapshot"
    )

    # Month end check: runs daily at 9 PM IST, posts summary only on the last day of the month
    scheduler.add_job(
        lambda: _run_month_end_summary(app),
        CronTrigger(hour=21, minute=0, timezone=IST),
        id="month_end_summary"
    )

    scheduler.start()
    logging.info("Scheduler started — news 8AM/1PM/6PM IST, reviews every 6h, crypto scan every 15min, month-end review")
    return scheduler


def _run_month_end_summary(app):
    import calendar
    from datetime import datetime
    now = datetime.now(IST)
    last_day = calendar.monthrange(now.year, now.month)[1]
    if now.day != last_day:
        return  # not the last day of the month
    snapshot = get_month_snapshot()
    if not snapshot:
        logging.warning("Month-end summary: no snapshot found for current month")
        return
    stats   = get_trade_stats()
    balance = get_futures_balance()
    post_month_end_summary(app.client, snapshot, stats, balance)
