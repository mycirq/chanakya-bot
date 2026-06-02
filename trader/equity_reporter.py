"""Slack reporting for Kite Equity Intraday events → #dalal-trades."""
import logging
from trader.config import EQUITY_TRADES_CHANNEL, OWNER_SLACK_ID

logger = logging.getLogger(__name__)


def _cid(client):
    try:
        result = client.conversations_list(limit=200, types="public_channel")
        for c in result["channels"]:
            if c["name"] == EQUITY_TRADES_CHANNEL:
                return c["id"]
    except Exception as e:
        logger.error(f"Channel lookup failed: {e}")
    return None


def _post(client, text):
    cid = _cid(client)
    if not cid:
        return
    try:
        client.chat_postMessage(channel=cid, text=text)
    except Exception as e:
        logger.error(f"equity post failed: {e}")


def post_equity_watchlist(client, trade_date, picks):
    if not picks:
        _post(client, f":mag: *Equity watchlist — {trade_date}*\n"
                      "No high-conviction news names tonight. Sitting out tomorrow (watchlist-only).")
        return
    lines = [f":clipboard: *Equity Intraday Watchlist — {trade_date}* (half-size observation)"]
    emoji = {"long": ":large_green_circle:", "short": ":red_circle:", "neutral": ":white_circle:"}
    for p in picks:
        lines.append(f"{emoji.get(p['bias'], ':white_circle:')} *{p['symbol']}* "
                     f"({p['bias']}, score {p['score']}) — {p['thesis']}")
    lines.append("_Engine trades ONLY these names tomorrow, on technical confirmation._")
    _post(client, "\n".join(lines))


def post_equity_thesis(client, plan):
    _post(client,
        f":brain: *Equity thesis: {plan['symbol']} {plan['direction'].upper()}*\n"
        f"Entry ~₹{plan['entry']} | SL ₹{plan['sl']} | TP ₹{plan['tp']} | "
        f"qty {plan['quantity']} | risk ₹{plan['risk_inr']:.0f} | score {plan['score']}\n"
        f"_{plan['reason']}_")


def post_equity_opened(client, plan, fill_price):
    em = ":large_green_circle:" if plan["direction"] == "long" else ":red_circle:"
    _post(client,
        f"{em} *{plan['direction'].upper()} {plan['symbol']} opened* (MIS)\n"
        f"Fill ₹{fill_price} | SL-M ₹{plan['sl']} | TP ₹{plan['tp']} | "
        f"qty {plan['quantity']} | risk ₹{plan['risk_inr']:.0f}")


def post_equity_closed(client, symbol, direction, entry, close, pnl_inr, reason, duration_min):
    em = ":white_check_mark:" if pnl_inr >= 0 else ":x:"
    _post(client,
        f"{em} *{symbol} closed* | {reason} | P&L ₹{pnl_inr:+,.0f}\n"
        f"{direction} ₹{entry} → ₹{close} | {duration_min} min")


def post_equity_squareoff(client, closed):
    if not closed:
        return
    total = sum(c["pnl"] for c in closed)
    lines = [f":checkered_flag: *3:15 PM square-off* — {len(closed)} position(s), day P&L ₹{total:+,.0f}"]
    for c in closed:
        lines.append(f"  • {c['symbol']}: ₹{c['pnl']:+,.0f}")
    _post(client, "\n".join(lines))


def post_equity_circuit(client, day_loss_inr, cap_inr):
    _post(client,
        f":octagonal_sign: *Equity daily-loss circuit hit* — down ₹{day_loss_inr:,.0f} "
        f"(cap ₹{cap_inr:,.0f}). No new equity trades today; open positions squared off.")


def post_equity_daily_summary(client, day_pnl, n_trades, wins, losses, capital):
    _post(client,
        f":bar_chart: *Equity Intraday Summary*\n"
        f"Trades {n_trades} (W{wins}/L{losses}) | Day P&L ₹{day_pnl:+,.0f} | "
        f"Capital ₹{capital:,.0f}")
