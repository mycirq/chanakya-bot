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


def post_crypto_scan_result(client, all_scores, threshold, zone, skip_reason=None):
    """DM owner with top 5 scores every crypto scan."""
    try:
        from datetime import datetime
        import pytz
        from trader.config import OWNER_SLACK_ID
        now = datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%H:%M")
        top5 = sorted(all_scores, key=lambda x: x[0], reverse=True)[:5]
        lines = [f"🔍 *{now} Crypto Scan* | zone: {zone} | {len(all_scores)} pairs"]
        for score, symbol, direction, reason in top5:
            arrow = "↑" if direction == "long" else ("↓" if direction == "short" else "—")
            near  = " 🔥 near miss!" if threshold * 0.8 <= score < threshold else ""
            fired = " ✅ TRADE!" if score >= threshold and not skip_reason else ""
            lines.append(f"• {symbol}: *{score}/100* {arrow}{near}{fired}\n  _{reason}_")
        if skip_reason:
            lines.append(f"⏭ Skipped: _{skip_reason}_")
        elif not any(s >= threshold for s, _, _, _ in all_scores):
            lines.append(f"❌ No trade — need {threshold}+")
        dm = client.conversations_open(users=OWNER_SLACK_ID)
        client.chat_postMessage(channel=dm["channel"]["id"], text="\n".join(lines))
    except Exception as e:
        logger.error(f"post_crypto_scan_result failed: {e}")


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


def post_month_end_summary(client, snapshot, stats, balance_usdt):
    cid = _get_channel_id(client)
    if not cid:
        return
    start_balance = float(snapshot.get("start_balance") or 0)
    target_pct    = float(snapshot.get("target_pct") or 0)
    month         = snapshot.get("month", "")
    pnl_usdt      = balance_usdt - start_balance
    pnl_pct       = (pnl_usdt / start_balance * 100) if start_balance else 0
    target_usdt   = start_balance * target_pct / 100

    wins   = next((s for s in stats if s["outcome"] == "win"),  {})
    losses = next((s for s in stats if s["outcome"] == "loss"), {})
    total  = (wins.get("cnt", 0) or 0) + (losses.get("cnt", 0) or 0)
    win_rate = (wins.get("cnt", 0) / total * 100) if total else 0

    hit = pnl_usdt >= target_usdt
    result_emoji = "🏆" if hit else "📉"
    try:
        client.chat_postMessage(
            channel=cid,
            text=f"{result_emoji} Month-End Review — {month}",
            blocks=[
                {"type": "header", "text": {"type": "plain_text",
                 "text": f"{result_emoji} Month-End Review — {month}"}},
                {"type": "section", "fields": [
                    {"type": "mrkdwn", "text": f"*Start Balance:*\n${start_balance:.2f} USDT"},
                    {"type": "mrkdwn", "text": f"*End Balance:*\n${balance_usdt:.2f} USDT"},
                    {"type": "mrkdwn", "text": f"*Monthly P&L:*\n{pnl_usdt:+.2f} USDT  ({pnl_pct:+.1f}%)"},
                    {"type": "mrkdwn", "text": f"*Target:*\n+{target_pct:.0f}%  (${target_usdt:.2f} USDT)"},
                    {"type": "mrkdwn", "text": f"*Total Trades:*\n{total}"},
                    {"type": "mrkdwn", "text": f"*Win Rate:*\n{win_rate:.1f}%"},
                ]},
                {"type": "section", "text": {"type": "mrkdwn",
                 "text": ("✅ *Target achieved!* Great month." if hit
                          else f"❌ *Target missed* by ${target_usdt - pnl_usdt:.2f} USDT.\nUse `/trade-target` to set next month's goal.")}},
                {"type": "context", "elements": [
                    {"type": "mrkdwn", "text": "Chanakya Trader • Monthly P&L Review"}
                ]}
            ]
        )
    except Exception as e:
        logger.error(f"post_month_end_summary failed: {e}")


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
