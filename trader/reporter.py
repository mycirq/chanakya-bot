"""Slack reporting for every trade event."""
import logging

logger = logging.getLogger(__name__)

CRYPTO_CHANNEL = "crypto-trades"


def _get_channel_id(client):
    result = client.conversations_list(limit=200, types="public_channel")
    for c in result["channels"]:
        if c["name"] == CRYPTO_CHANNEL:
            return c["id"]
    return None


def post_pre_trade_thesis(client, symbol, direction, entry, tp, sl,
                          margin, leverage, score, reason, rr):
    """Posted BEFORE executing — thesis + proof, no approval needed."""
    cid = _get_channel_id(client)
    if not cid:
        return
    arrow   = "🟢 LONG" if direction == "long" else "🔴 SHORT"
    tp_pct  = abs(tp - entry) / entry * 100 * leverage
    sl_pct  = abs(sl - entry) / entry * 100 * leverage
    try:
        client.chat_postMessage(
            channel=cid,
            text=f"🧠 Trade thesis: {arrow} {symbol}",
            blocks=[
                {"type": "header", "text": {"type": "plain_text",
                 "text": f"🧠 Executing: {arrow} {symbol}"}},
                {"type": "section", "text": {"type": "mrkdwn",
                 "text": f"*Thesis:*\n{reason}"}},
                {"type": "section", "fields": [
                    {"type": "mrkdwn", "text": f"*Entry:*\n${entry:,.4f}"},
                    {"type": "mrkdwn", "text": f"*Direction:*\n{direction.upper()} {leverage}x isolated"},
                    {"type": "mrkdwn", "text": f"*Take Profit:*\n${tp:,.4f}  (+{tp_pct:.1f}% on margin)"},
                    {"type": "mrkdwn", "text": f"*Stop Loss:*\n${sl:,.4f}  (-{sl_pct:.1f}% on margin)"},
                    {"type": "mrkdwn", "text": f"*Margin:*\n${margin:.2f} USDT"},
                    {"type": "mrkdwn", "text": f"*Risk:Reward:*\n1:{rr:.1f}"},
                ]},
                {"type": "section", "text": {"type": "mrkdwn",
                 "text": f"*Signal confidence:* {score}/100 — executing now ⚡"}},
                {"type": "context", "elements": [
                    {"type": "mrkdwn", "text": "Chanakya Trader • No approval needed • Order firing now"}
                ]}
            ]
        )
    except Exception as e:
        logger.error(f"post_pre_trade_thesis failed: {e}")


def post_trade_opened(client, symbol, direction, entry, tp, sl, liq,
                      margin, leverage, score, reason, pnl_pct_tp):
    cid = _get_channel_id(client)
    if not cid:
        return
    arrow  = "🟢 LONG" if direction == "long" else "🔴 SHORT"
    tp_pct = abs(tp - entry) / entry * 100 * leverage
    sl_pct = abs(sl - entry) / entry * 100 * leverage
    try:
        client.chat_postMessage(
            channel=cid,
            text=f"{arrow} {symbol} opened",
            blocks=[
                {"type": "header", "text": {"type": "plain_text",
                 "text": f"{arrow} {symbol}"}},
                {"type": "section", "fields": [
                    {"type": "mrkdwn", "text": f"*Entry:*\n${entry:,.4f}"},
                    {"type": "mrkdwn", "text": f"*Direction:*\n{direction.upper()} {leverage}x"},
                    {"type": "mrkdwn", "text": f"*Take Profit:*\n${tp:,.4f}  (+{tp_pct:.1f}% on margin)"},
                    {"type": "mrkdwn", "text": f"*Stop Loss:*\n${sl:,.4f}  (-{sl_pct:.1f}% on margin)"},
                    {"type": "mrkdwn", "text": f"*Margin:*\n${margin:.2f} USDT"},
                    {"type": "mrkdwn", "text": f"*Liquidation:*\n${liq:,.4f}"},
                ]},
                {"type": "section", "text": {"type": "mrkdwn",
                 "text": f"*Signal score:* {score}/100\n_{reason}_"}},
                {"type": "context", "elements": [
                    {"type": "mrkdwn", "text": "Chanakya Trader • Binance USDT-M Futures"}
                ]}
            ]
        )
    except Exception as e:
        logger.error(f"post_trade_opened failed: {e}")


def post_trade_closed(client, symbol, direction, entry, close_price,
                      pnl_usdt, close_reason, duration_min):
    cid = _get_channel_id(client)
    if not cid:
        return
    pnl_pct = (close_price - entry) / entry * 100
    if direction == "short":
        pnl_pct = -pnl_pct
    emoji = "✅" if pnl_usdt > 0 else "❌"
    try:
        client.chat_postMessage(
            channel=cid,
            text=f"{emoji} {symbol} closed | {pnl_usdt:+.2f} USDT",
            blocks=[
                {"type": "header", "text": {"type": "plain_text",
                 "text": f"{emoji} {symbol} Closed"}},
                {"type": "section", "fields": [
                    {"type": "mrkdwn", "text": f"*Entry:*\n${entry:,.4f}"},
                    {"type": "mrkdwn", "text": f"*Exit:*\n${close_price:,.4f}"},
                    {"type": "mrkdwn", "text": f"*P&L:*\n{pnl_usdt:+.2f} USDT  ({pnl_pct:+.2f}%)"},
                    {"type": "mrkdwn", "text": f"*Reason:*\n{close_reason}"},
                    {"type": "mrkdwn", "text": f"*Duration:*\n{duration_min} min"},
                ]},
                {"type": "context", "elements": [
                    {"type": "mrkdwn", "text": "Chanakya Trader • Binance USDT-M Futures"}
                ]}
            ]
        )
    except Exception as e:
        logger.error(f"post_trade_closed failed: {e}")


def post_drawdown_warning(client, loss_usdt):
    cid = _get_channel_id(client)
    if not cid:
        return
    try:
        client.chat_postMessage(
            channel=cid,
            text=f"⚠️ Drawdown Warning: ${loss_usdt:.2f} USDT lost",
            blocks=[
                {"type": "header", "text": {"type": "plain_text",
                 "text": "⚠️ Drawdown Warning"}},
                {"type": "section", "text": {"type": "mrkdwn",
                 "text": f"Portfolio loss has reached *${loss_usdt:.2f} USDT*.\n"
                         f"Approaching hard stop limit. Bot is still running.\n"
                         f"Use `/trade-pause` if you want to intervene."}},
            ]
        )
    except Exception as e:
        logger.error(f"post_drawdown_warning failed: {e}")


def post_hard_stop(client, loss_usdt):
    cid = _get_channel_id(client)
    if not cid:
        return
    try:
        client.chat_postMessage(
            channel=cid,
            text=f"🛑 Hard Stop Hit — All trading paused",
            blocks=[
                {"type": "header", "text": {"type": "plain_text",
                 "text": "🛑 Hard Stop Hit"}},
                {"type": "section", "text": {"type": "mrkdwn",
                 "text": f"Total loss *${loss_usdt:.2f} USDT* has hit the hard limit.\n"
                         f"*All positions closed. No new trades.*\n"
                         f"Use `/trade-resume` after reviewing to restart."}},
            ]
        )
    except Exception as e:
        logger.error(f"post_hard_stop failed: {e}")


def post_daily_summary(client, stats, balance_usdt, open_count):
    cid = _get_channel_id(client)
    if not cid:
        return
    wins   = next((s for s in stats if s["outcome"] == "win"), {})
    losses = next((s for s in stats if s["outcome"] == "loss"), {})
    total  = (wins.get("cnt", 0) or 0) + (losses.get("cnt", 0) or 0)
    win_rate = (wins.get("cnt", 0) / total * 100) if total else 0
    total_pnl = (wins.get("total_pnl") or 0) + (losses.get("total_pnl") or 0)
    try:
        client.chat_postMessage(
            channel=cid,
            text="📊 Daily Trading Summary",
            blocks=[
                {"type": "header", "text": {"type": "plain_text", "text": "📊 Daily Summary"}},
                {"type": "section", "fields": [
                    {"type": "mrkdwn", "text": f"*Total Trades:*\n{total}"},
                    {"type": "mrkdwn", "text": f"*Win Rate:*\n{win_rate:.1f}%"},
                    {"type": "mrkdwn", "text": f"*Total P&L:*\n{total_pnl:+.2f} USDT"},
                    {"type": "mrkdwn", "text": f"*Wallet Balance:*\n${balance_usdt:.2f} USDT"},
                    {"type": "mrkdwn", "text": f"*Open Positions:*\n{open_count}"},
                ]},
                {"type": "context", "elements": [
                    {"type": "mrkdwn", "text": "Chanakya Trader • Daily P&L Report"}
                ]}
            ]
        )
    except Exception as e:
        logger.error(f"post_daily_summary failed: {e}")
