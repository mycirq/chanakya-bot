"""Slack reporting for Kite FnO trade events."""
import logging
from trader.config import KITE_TRADES_CHANNEL, OWNER_SLACK_ID

logger = logging.getLogger(__name__)


def _get_channel_id(client):
    try:
        result = client.conversations_list(limit=200, types="public_channel")
        for c in result["channels"]:
            if c["name"] == KITE_TRADES_CHANNEL:
                return c["id"]
    except Exception as e:
        logger.error(f"Channel lookup failed: {e}")
    return None


def post_totp_reminder(client):
    """DM the owner at 8:50 AM asking for TOTP."""
    try:
        dm = client.conversations_open(users=OWNER_SLACK_ID)
        channel = dm["channel"]["id"]
        client.chat_postMessage(
            channel=channel,
            text="Kite market opens in 25 min. Send your TOTP: `/kite-auth 123456`"
        )
    except Exception as e:
        logger.error(f"post_totp_reminder failed: {e}")


def post_kite_thesis(client, underlying, direction, tradingsymbol,
                     premium, tp, sl, quantity, lot_size, score, reason):
    cid = _get_channel_id(client)
    if not cid:
        return
    arrow    = "🟢 CALL" if direction == "long" else "🔴 PUT"
    lots     = quantity // lot_size
    total    = premium * quantity
    tp_pct   = (tp - premium) / premium * 100
    sl_pct   = (premium - sl) / premium * 100
    try:
        client.chat_postMessage(
            channel=cid,
            text=f"🧠 FnO Thesis: {arrow} {underlying}",
            blocks=[
                {"type": "header", "text": {"type": "plain_text",
                 "text": f"🧠 Executing: {arrow} {underlying}"}},
                {"type": "section", "text": {"type": "mrkdwn",
                 "text": f"*Thesis:*\n{reason}"}},
                {"type": "section", "fields": [
                    {"type": "mrkdwn", "text": f"*Contract:*\n`{tradingsymbol}`"},
                    {"type": "mrkdwn", "text": f"*Entry Premium:*\n₹{premium:.2f}"},
                    {"type": "mrkdwn", "text": f"*Take Profit:*\n₹{tp:.2f}  (+{tp_pct:.0f}%)"},
                    {"type": "mrkdwn", "text": f"*Stop Loss:*\n₹{sl:.2f}  (-{sl_pct:.0f}%)"},
                    {"type": "mrkdwn", "text": f"*Quantity:*\n{lots} lot(s) × {lot_size} = {quantity}"},
                    {"type": "mrkdwn", "text": f"*Total Cost:*\n₹{total:,.0f}"},
                ]},
                {"type": "section", "text": {"type": "mrkdwn",
                 "text": f"*Signal confidence:* {score}/100 — executing now ⚡"}},
                {"type": "context", "elements": [
                    {"type": "mrkdwn", "text": "Chanakya Trader • Kite FnO • NSE Weekly Options"}
                ]}
            ]
        )
    except Exception as e:
        logger.error(f"post_kite_thesis failed: {e}")


def post_kite_opened(client, underlying, direction, tradingsymbol,
                     premium, tp, sl, quantity, lot_size, score, reason):
    cid = _get_channel_id(client)
    if not cid:
        return
    arrow = "🟢 CALL" if direction == "long" else "🔴 PUT"
    lots  = quantity // lot_size
    try:
        client.chat_postMessage(
            channel=cid,
            text=f"{arrow} {underlying} opened",
            blocks=[
                {"type": "header", "text": {"type": "plain_text",
                 "text": f"{arrow} {underlying} — {tradingsymbol}"}},
                {"type": "section", "fields": [
                    {"type": "mrkdwn", "text": f"*Premium:*\n₹{premium:.2f}"},
                    {"type": "mrkdwn", "text": f"*Lots:*\n{lots} × {lot_size}"},
                    {"type": "mrkdwn", "text": f"*TP:*\n₹{tp:.2f}"},
                    {"type": "mrkdwn", "text": f"*SL:*\n₹{sl:.2f}"},
                ]},
                {"type": "section", "text": {"type": "mrkdwn",
                 "text": f"*Score:* {score}/100 | _{reason}_"}},
                {"type": "context", "elements": [
                    {"type": "mrkdwn", "text": "Chanakya Trader • Kite FnO"}
                ]}
            ]
        )
    except Exception as e:
        logger.error(f"post_kite_opened failed: {e}")


def post_kite_closed(client, underlying, tradingsymbol, direction,
                     entry_premium, close_premium, pnl_inr, close_reason, duration_min):
    cid = _get_channel_id(client)
    if not cid:
        return
    pnl_pct = (close_premium - entry_premium) / entry_premium * 100
    emoji   = "✅" if pnl_inr > 0 else "❌"
    try:
        client.chat_postMessage(
            channel=cid,
            text=f"{emoji} {underlying} closed | ₹{pnl_inr:+,.0f}",
            blocks=[
                {"type": "header", "text": {"type": "plain_text",
                 "text": f"{emoji} {underlying} Closed"}},
                {"type": "section", "fields": [
                    {"type": "mrkdwn", "text": f"*Contract:*\n`{tradingsymbol}`"},
                    {"type": "mrkdwn", "text": f"*Entry:*\n₹{entry_premium:.2f}"},
                    {"type": "mrkdwn", "text": f"*Exit:*\n₹{close_premium:.2f}"},
                    {"type": "mrkdwn", "text": f"*P&L:*\n₹{pnl_inr:+,.0f}  ({pnl_pct:+.1f}%)"},
                    {"type": "mrkdwn", "text": f"*Reason:*\n{close_reason}"},
                    {"type": "mrkdwn", "text": f"*Duration:*\n{duration_min} min"},
                ]},
                {"type": "context", "elements": [
                    {"type": "mrkdwn", "text": "Chanakya Trader • Kite FnO"}
                ]}
            ]
        )
    except Exception as e:
        logger.error(f"post_kite_closed failed: {e}")


def post_kite_drawdown_warning(client, loss_inr):
    cid = _get_channel_id(client)
    if not cid:
        return
    try:
        client.chat_postMessage(
            channel=cid,
            text=f"⚠️ FnO Drawdown Warning: ₹{loss_inr:,.0f} lost",
            blocks=[
                {"type": "header", "text": {"type": "plain_text", "text": "⚠️ FnO Drawdown Warning"}},
                {"type": "section", "text": {"type": "mrkdwn",
                 "text": f"FnO loss has reached *₹{loss_inr:,.0f}*.\n"
                         f"Approaching hard stop. Bot is still running.\n"
                         f"Use `/trade-pause kite` to intervene."}}
            ]
        )
    except Exception as e:
        logger.error(f"post_kite_drawdown_warning failed: {e}")


def post_kite_hard_stop(client, loss_inr):
    cid = _get_channel_id(client)
    if not cid:
        return
    try:
        client.chat_postMessage(
            channel=cid,
            text="🛑 FnO Hard Stop Hit — all Kite trading paused",
            blocks=[
                {"type": "header", "text": {"type": "plain_text", "text": "🛑 FnO Hard Stop Hit"}},
                {"type": "section", "text": {"type": "mrkdwn",
                 "text": f"FnO loss *₹{loss_inr:,.0f}* hit the ₹40,000 hard limit.\n"
                         f"*All positions squared off. No new trades.*\n"
                         f"Use `/trade-resume kite` after reviewing."}}
            ]
        )
    except Exception as e:
        logger.error(f"post_kite_hard_stop failed: {e}")


def post_fno_analysis(client, channel_id, analysis: dict):
    """Post full FnO analysis to a Slack channel."""
    try:
        u         = analysis["underlying"]
        score     = analysis["final_score"]
        direction = analysis["direction"]
        ts        = analysis["timestamp"]

        arrow     = "🟢 CALL (Bullish)" if direction == "long" else ("🔴 PUT (Bearish)" if direction == "short" else "⚪ No Clear Signal")
        conf      = "High" if score >= 70 else ("Moderate" if score >= 50 else "Low")
        conf_emoji = "🔥" if score >= 70 else ("⚡" if score >= 50 else "❄️")

        tech  = analysis.get("technical", {})
        vix   = analysis.get("vix", {})
        oi    = analysis.get("options_chain", {})
        fii   = analysis.get("fii", {})
        chain = oi.get("data") or {}

        price   = analysis.get("current_price", 0)
        atm     = analysis.get("atm")
        expiry  = str(analysis.get("expiry", ""))
        window  = analysis.get("entry_window", "—")
        tl      = analysis.get("total_long", 0)
        ts_     = analysis.get("total_short", 0)

        # Score bar
        filled = int(score / 10)
        bar    = "█" * filled + "░" * (10 - filled)

        # Option recommendation
        opt_type = "CE" if direction == "long" else "PE"
        rec_text = ""
        if atm and direction:
            rec_text = (
                f"*Contract:* {u}{expiry.replace('-','')} {atm:.0f} {opt_type} (ATM)\n"
                f"*Target:* +50% on premium | *SL:* -30% on premium\n"
                f"*Expected ROI:* 40–60% on premium if TP hit\n"
                f"*Best Entry:* {window}"
            )
        else:
            rec_text = f"*Best Entry Window:* {window}\n_No clear directional signal — wait for confirmation._"

        blocks = [
            {"type": "header", "text": {"type": "plain_text",
             "text": f"{conf_emoji} {u} FnO Analysis — {arrow}"}},
            {"type": "section", "fields": [
                {"type": "mrkdwn", "text": f"*Overall Score:*\n{bar} {score}/100"},
                {"type": "mrkdwn", "text": f"*Confidence:*\n{conf}"},
                {"type": "mrkdwn", "text": f"*Long pts:*\n{tl}/80"},
                {"type": "mrkdwn", "text": f"*Short pts:*\n{ts_}/80"},
            ]},
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn",
             "text": f"*📈 Technical (25 pts max) — {tech.get('long_pts', 0)}L / {tech.get('short_pts', 0)}S*\n{tech.get('reason', 'no data')}"}},
            {"type": "section", "text": {"type": "mrkdwn",
             "text": f"*🌡 India VIX (20 pts max) — {vix.get('long_pts', 0)}L / {vix.get('short_pts', 0)}S*\n{vix.get('summary', 'no data')}"}},
            {"type": "section", "text": {"type": "mrkdwn",
             "text": f"*📊 Options Chain (20 pts max) — {oi.get('long_pts', 0)}L / {oi.get('short_pts', 0)}S*\n{oi.get('summary', 'no data')}" +
             (f"\nPCR: {chain.get('pcr','—')} | Max Pain: {chain.get('max_pain','—'):.0f} | Call Wall: {chain.get('call_wall','—'):.0f} | Put Wall: {chain.get('put_wall','—'):.0f}" if chain else "")}},
            {"type": "section", "text": {"type": "mrkdwn",
             "text": f"*🏦 FII Positioning (15 pts max) — {fii.get('long_pts', 0)}L / {fii.get('short_pts', 0)}S*\n{fii.get('summary', 'no data')}"}},
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn",
             "text": f"*📌 Recommendation*\n{rec_text}"}},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": f"Chanakya Analysis • {u} • {ts}"}
            ]}
        ]

        client.chat_postMessage(
            channel=channel_id,
            text=f"{conf_emoji} {u} FnO Analysis — {score}/100 {arrow}",
            blocks=blocks
        )
    except Exception as e:
        logger.error(f"post_fno_analysis failed: {e}")


def post_kite_scan_result(client, scores, threshold, skip_reason=None):
    """DM owner with every scan result — scores for all indices."""
    try:
        from datetime import datetime
        from trader.config import IST
        now = datetime.now(IST).strftime("%H:%M")
        lines = [f"🔍 *{now} Kite Scan*"]
        for underlying, score, direction, reason in scores:
            arrow = "↑" if direction == "long" else ("↓" if direction == "short" else "—")
            near  = " 🔥 near miss!" if threshold * 0.8 <= score < threshold else ""
            fired = " ✅ TRADE!" if score >= threshold and not skip_reason else ""
            lines.append(f"• {underlying}: *{score}/100* {arrow}{near}{fired}\n  _{reason}_")
        if skip_reason:
            lines.append(f"⏭ Skipped: _{skip_reason}_")
        elif not any(s >= threshold for _, s, _, _ in scores):
            lines.append(f"❌ No trade — need {threshold}+")
        dm = client.conversations_open(users=OWNER_SLACK_ID)
        client.chat_postMessage(channel=dm["channel"]["id"], text="\n".join(lines))
    except Exception as e:
        logger.error(f"post_kite_scan_result failed: {e}")


def post_kite_daily_summary(client, stats, balance_inr, open_count):
    cid = _get_channel_id(client)
    if not cid:
        return
    wins   = next((s for s in stats if s["outcome"] == "win"),  {})
    losses = next((s for s in stats if s["outcome"] == "loss"), {})
    total  = (wins.get("cnt", 0) or 0) + (losses.get("cnt", 0) or 0)
    win_rate  = (wins.get("cnt", 0) / total * 100) if total else 0
    total_pnl = (wins.get("total_pnl") or 0) + (losses.get("total_pnl") or 0)
    try:
        client.chat_postMessage(
            channel=cid,
            text="📊 FnO Daily Summary",
            blocks=[
                {"type": "header", "text": {"type": "plain_text", "text": "📊 FnO Daily Summary"}},
                {"type": "section", "fields": [
                    {"type": "mrkdwn", "text": f"*Total Trades:*\n{total}"},
                    {"type": "mrkdwn", "text": f"*Win Rate:*\n{win_rate:.1f}%"},
                    {"type": "mrkdwn", "text": f"*Total P&L:*\n₹{total_pnl:+,.0f}"},
                    {"type": "mrkdwn", "text": f"*Capital:*\n₹{balance_inr:,.0f}"},
                    {"type": "mrkdwn", "text": f"*Open Positions:*\n{open_count}"},
                ]},
                {"type": "context", "elements": [
                    {"type": "mrkdwn", "text": "Chanakya Trader • FnO Daily Report"}
                ]}
            ]
        )
    except Exception as e:
        logger.error(f"post_kite_daily_summary failed: {e}")


def post_kite_month_end_summary(client, snapshot, stats, balance_inr):
    cid = _get_channel_id(client)
    if not cid:
        return
    start  = float(snapshot.get("start_balance") or 0)
    target = float(snapshot.get("target_pct") or 0)
    month  = snapshot.get("month", "")
    pnl    = balance_inr - start
    pnl_pct = (pnl / start * 100) if start else 0
    target_amt = start * target / 100
    hit = pnl >= target_amt

    wins   = next((s for s in stats if s["outcome"] == "win"),  {})
    losses = next((s for s in stats if s["outcome"] == "loss"), {})
    total  = (wins.get("cnt", 0) or 0) + (losses.get("cnt", 0) or 0)
    win_rate = (wins.get("cnt", 0) / total * 100) if total else 0
    result_emoji = "🏆" if hit else "📉"
    try:
        client.chat_postMessage(
            channel=cid,
            text=f"{result_emoji} FnO Month-End Review — {month}",
            blocks=[
                {"type": "header", "text": {"type": "plain_text",
                 "text": f"{result_emoji} FnO Month-End Review — {month}"}},
                {"type": "section", "fields": [
                    {"type": "mrkdwn", "text": f"*Start Capital:*\n₹{start:,.0f}"},
                    {"type": "mrkdwn", "text": f"*End Capital:*\n₹{balance_inr:,.0f}"},
                    {"type": "mrkdwn", "text": f"*Monthly P&L:*\n₹{pnl:+,.0f}  ({pnl_pct:+.1f}%)"},
                    {"type": "mrkdwn", "text": f"*Target:*\n+{target:.0f}%  (₹{target_amt:,.0f})"},
                    {"type": "mrkdwn", "text": f"*Trades:*\n{total}"},
                    {"type": "mrkdwn", "text": f"*Win Rate:*\n{win_rate:.1f}%"},
                ]},
                {"type": "section", "text": {"type": "mrkdwn",
                 "text": ("✅ *Target achieved!*" if hit
                          else f"❌ *Missed by ₹{target_amt - pnl:,.0f}*. Use `/trade-target kite` to reset.")}},
                {"type": "context", "elements": [
                    {"type": "mrkdwn", "text": "Chanakya Trader • FnO Monthly Review"}
                ]}
            ]
        )
    except Exception as e:
        logger.error(f"post_kite_month_end_summary failed: {e}")
