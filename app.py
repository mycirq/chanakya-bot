import os
import logging
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from db import init_db, save_suggestion, get_active_suggestions, get_distinct_users, is_duplicate, remove_suggestion
from prices import get_price, search_tickers
from scheduler import start_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = App(token=os.environ["SLACK_BOT_TOKEN"])

MARKET_EMOJI    = {"india": "🇮🇳", "us": "🇺🇸", "crypto": "🪙", "realestate": "🏠"}
CURRENCY_SYMBOL = {"india": "₹",   "us": "$",    "crypto": "$",  "realestate": "₹"}
MARKET_LABEL    = {"india": "Indian Stock", "us": "US Stock", "crypto": "Crypto", "realestate": "Real Estate"}
TICKER_HINT     = {
    "india":       "e.g. RELIANCE, TCS, INFY (NSE ticker)",
    "us":          "e.g. AAPL, TSLA, NVDA",
    "crypto":      "e.g. bitcoin, ethereum, solana",
    "realestate":  "Property name or location",
}

def _post_reply(client, channel_id, user_id, text, blocks=None):
    """Use ephemeral in channels, plain DM in direct messages."""
    if channel_id.startswith("D"):
        client.chat_postMessage(channel=channel_id, text=text, blocks=blocks)
    else:
        client.chat_postEphemeral(channel=channel_id, user=user_id, text=text, blocks=blocks)


MARKET_OPTIONS = [
    {"text": {"type": "plain_text", "text": "🇮🇳  Indian Stock Market"}, "value": "india"},
    {"text": {"type": "plain_text", "text": "🇺🇸  US Stock Market"},     "value": "us"},
    {"text": {"type": "plain_text", "text": "🪙  Crypto Market"},        "value": "crypto"},
    {"text": {"type": "plain_text", "text": "🏠  Real Estate"},          "value": "realestate"},
]


# ── Modal builder ─────────────────────────────────────────────────────────────

def build_suggest_modal(channel_id, channel_name, selected_market=None):
    """Build the suggest modal. If market is selected, include stock search + optional fields."""

    market_block = {
        "type": "input",
        "block_id": "market_block",
        "dispatch_action": True,
        "label": {"type": "plain_text", "text": "Market Type"},
        "element": {
            "type": "static_select",
            "action_id": "market_type",
            "placeholder": {"type": "plain_text", "text": "Select market..."},
            "options": MARKET_OPTIONS,
        }
    }

    # Pre-select if already chosen
    if selected_market:
        market_block["element"]["initial_option"] = next(
            o for o in MARKET_OPTIONS if o["value"] == selected_market
        )

    blocks = [market_block]

    if selected_market:
        if selected_market == "realestate":
            blocks.append({
                "type": "input",
                "block_id": "ticker_block",
                "label": {"type": "plain_text", "text": "Property Name *"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "ticker",
                    "placeholder": {"type": "plain_text", "text": "e.g. Prestige Whitefield Block A"}
                }
            })
            blocks.append({
                "type": "input",
                "block_id": "price_block",
                "optional": True,
                "label": {"type": "plain_text", "text": "Current Value (₹)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "manual_price",
                    "placeholder": {"type": "plain_text", "text": "e.g. 8500000"}
                }
            })
        else:
            blocks.append({
                "type": "input",
                "block_id": "ticker_block",
                "dispatch_action": True,
                "label": {"type": "plain_text", "text": "Stock / Asset *"},
                "hint": {"type": "plain_text", "text": TICKER_HINT[selected_market]},
                "element": {
                    "type": "external_select",
                    "action_id": "ticker",
                    "placeholder": {"type": "plain_text", "text": "Type to search..."},
                    "min_query_length": 1
                }
            })

        blocks += [
            {
                "type": "input",
                "block_id": "roi_block",
                "optional": True,
                "label": {"type": "plain_text", "text": "Expected ROI (%)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "roi",
                    "placeholder": {"type": "plain_text", "text": "e.g. 15"}
                }
            },
            {
                "type": "input",
                "block_id": "days_block",
                "optional": True,
                "label": {"type": "plain_text", "text": "Tracking Period (days)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "days",
                    "initial_value": "10",
                    "placeholder": {"type": "plain_text", "text": "10"}
                }
            },
            {
                "type": "input",
                "block_id": "analysis_block",
                "optional": True,
                "label": {"type": "plain_text", "text": "Analysis / Reasoning"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "analysis",
                    "multiline": True,
                    "placeholder": {"type": "plain_text", "text": "Why is this a good investment? Catalysts, technical levels, news..."}
                }
            }
        ]

    return {
        "type": "modal",
        "callback_id": "suggest_modal",
        "private_metadata": f"{channel_id}|{channel_name}|{selected_market or ''}",
        "title": {"type": "plain_text", "text": "New Suggestion"},
        "submit": {"type": "plain_text", "text": "Post Suggestion"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks
    }


# ── /suggest command ──────────────────────────────────────────────────────────

@app.command("/suggest")
def handle_suggest(ack, command, client):
    ack()
    if command["channel_name"] != "artha-manthan":
        client.chat_postEphemeral(
            channel=command["channel_id"],
            user=command["user_id"],
            text="📍 Please use `/suggest` in <#artha-manthan> — that's the dedicated picks channel."
        )
        return
    client.views_open(
        trigger_id=command["trigger_id"],
        view=build_suggest_modal(command["channel_id"], command["channel_name"])
    )


# ── Market type selected → update modal ──────────────────────────────────────

@app.action("market_type")
def handle_market_type(ack, body, client, action):
    ack()
    selected_market = action["selected_option"]["value"]
    view_id = body["view"]["id"]
    meta = body["view"]["private_metadata"].split("|")
    channel_id, channel_name = meta[0], meta[1]

    client.views_update(
        view_id=view_id,
        view=build_suggest_modal(channel_id, channel_name, selected_market)
    )


# ── Stock selected → show live LTP in modal ──────────────────────────────────

@app.action("ticker")
def handle_ticker_selected(ack, body, client, action):
    ack()
    selected = action.get("selected_option")
    if not selected:
        return

    ticker = selected["value"]
    view = body["view"]
    meta = view["private_metadata"].split("|")
    market = meta[2] if len(meta) > 2 else "us"

    price, price_display, _ = get_price(ticker, market)
    emoji = MARKET_EMOJI.get(market, "📈")
    ltp_text = (f"{emoji} *Live Price (LTP):* {price_display}"
                if price else f"⚠️ Could not fetch live price for `{ticker}`")

    # Strip any existing ltp_context block, then insert fresh one after ticker_block
    base_blocks = [b for b in view["blocks"] if b.get("block_id") != "ltp_context"]
    new_blocks = []
    for block in base_blocks:
        new_blocks.append(block)
        if block.get("block_id") == "ticker_block":
            new_blocks.append({
                "type": "context",
                "block_id": "ltp_context",
                "elements": [{"type": "mrkdwn", "text": ltp_text}]
            })

    client.views_update(
        view_id=view["id"],
        view={
            "type": "modal",
            "callback_id": view["callback_id"],
            "private_metadata": view["private_metadata"],
            "title": view["title"],
            "submit": view["submit"],
            "close": view["close"],
            "blocks": new_blocks
        }
    )


# ── Ticker typeahead ──────────────────────────────────────────────────────────

@app.options("ticker")
def handle_ticker_search(ack, payload):
    query = payload.get("value", "").strip()
    view = payload.get("view", {})
    meta = view.get("private_metadata", "||").split("|")
    market = meta[2] if len(meta) > 2 and meta[2] else "us"

    results = search_tickers(query, market)
    ack(options=[
        {"text": {"type": "plain_text", "text": r["text"]}, "value": r["value"]}
        for r in results
    ])


# ── Modal submission ──────────────────────────────────────────────────────────

@app.view("suggest_modal")
def handle_modal_submit(ack, body, client, view):
    ack()
    values = view["state"]["values"]
    meta = view["private_metadata"].split("|")
    channel_id, channel_name = meta[0], meta[1]

    # Read market from the form itself (not metadata) so it's always accurate
    market = values["market_block"]["market_type"]["selected_option"]["value"]
    user_id = body["user"]["id"]

    # Ticker
    if market == "realestate":
        ticker = (values["ticker_block"]["ticker"].get("value") or "").strip()
    else:
        ticker_opt = values["ticker_block"]["ticker"].get("selected_option") or {}
        ticker = (ticker_opt.get("value") or "").strip().upper()

    if not ticker:
        client.chat_postEphemeral(channel=channel_id, user=user_id,
                                  text="Please select a stock / asset.")
        return

    # Duplicate check — same user + ticker + market
    if is_duplicate(user_id, ticker, market):
        client.chat_postEphemeral(
            channel=channel_id, user=user_id,
            text=f"You already have an active suggestion for *{ticker}* in {MARKET_LABEL[market]}. "
                 f"Close the existing one before adding a new entry."
        )
        return

    # Optional fields
    roi_raw = (values.get("roi_block", {}).get("roi", {}).get("value") or "").strip().replace("%", "")
    days_raw = (values.get("days_block", {}).get("days", {}).get("value") or "10").strip()
    analysis = (values.get("analysis_block", {}).get("analysis", {}).get("value") or "").strip()

    try:
        expected_roi = float(roi_raw) if roi_raw else 0.0
    except ValueError:
        expected_roi = 0.0

    try:
        days = int(days_raw) if days_raw else 10
    except ValueError:
        days = 10

    # Price
    if market == "realestate":
        price_raw = (values.get("price_block", {}).get("manual_price", {}).get("value") or "0").replace(",", "")
        try:
            price = float(price_raw)
        except ValueError:
            price = 0.0
        price_display = f"₹{price:,.2f}"
    else:
        price, price_display, _ = get_price(ticker, market)
        if price is None:
            client.chat_postEphemeral(
                channel=channel_id, user=user_id,
                text=f"Could not fetch live price for *{ticker}*. Please check the ticker and try again."
            )
            return

    suggestion_id = save_suggestion(
        ticker=ticker, market=market,
        channel_id=channel_id, channel_name=channel_name,
        user_id=user_id, entry_price=price,
        expected_roi=expected_roi, analysis=analysis, tracking_days=days
    )

    emoji    = MARKET_EMOJI[market]
    currency = CURRENCY_SYMBOL[market]
    label    = MARKET_LABEL[market]

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"{emoji}  New Investment Suggestion"}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Asset:*\n`{ticker}`"},
                {"type": "mrkdwn", "text": f"*Market:*\n{label}"},
                {"type": "mrkdwn", "text": f"*Entry Price (LTP):*\n{price_display}"},
                {"type": "mrkdwn", "text": f"*Expected ROI:*\n{'+' + str(expected_roi) + '%' if expected_roi else 'Not set'}"},
                {"type": "mrkdwn", "text": f"*Review In:*\n{days} days"},
            ]
        },
    ]
    if analysis:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Analysis:*\n{analysis}"}})
    blocks += [
        {"type": "divider"},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"Suggested by <@{user_id}>  •  ID: #{suggestion_id}"}
        ]}
    ]

    client.chat_postMessage(channel=channel_id, text=f"New {label} suggestion: {ticker}", blocks=blocks)


# ── /remove-suggestion command ────────────────────────────────────────────────

def _build_remove_modal(channel_id, requester_id, selected_uid=None):
    """Build remove modal. Step 1: pick user. Step 2: show their suggestions as checkboxes."""
    distinct_users = get_distinct_users()
    user_options = [{"text": {"type": "plain_text", "text": "👤 My suggestions"}, "value": requester_id}]
    for uid in distinct_users:
        if uid != requester_id:
            user_options.append({"text": {"type": "plain_text", "text": f"<@{uid}>"}, "value": uid})

    user_block = {
        "type": "input",
        "block_id": "user_pick_block",
        "dispatch_action": True,
        "label": {"type": "plain_text", "text": "Whose portfolio?"},
        "element": {
            "type": "static_select",
            "action_id": "remove_user_pick",
            "placeholder": {"type": "plain_text", "text": "Select a member..."},
            "options": user_options,
        }
    }

    if selected_uid:
        user_block["element"]["initial_option"] = next(
            (o for o in user_options if o["value"] == selected_uid),
            user_options[0]
        )

    blocks = [user_block]

    if selected_uid:
        suggestions = get_active_suggestions(user_id=selected_uid)
        if suggestions:
            checkbox_options = []
            for s in suggestions:
                emoji = MARKET_EMOJI.get(s["market"], "📈")
                currency = CURRENCY_SYMBOL.get(s["market"], "")
                label = f"{emoji} {s['ticker']} — Entry: {currency}{float(s['entry_price']):,.2f}"
                checkbox_options.append({
                    "text": {"type": "mrkdwn", "text": label},
                    "value": str(s["id"])
                })
            blocks.append({
                "type": "input",
                "block_id": "suggestions_block",
                "label": {"type": "plain_text", "text": "Select suggestions to remove"},
                "element": {
                    "type": "checkboxes",
                    "action_id": "suggestions_check",
                    "options": checkbox_options
                }
            })
        else:
            blocks.append({
                "type": "section",
                "block_id": "no_suggestions_block",
                "text": {"type": "mrkdwn", "text": "_No active suggestions for this user._"}
            })

    return {
        "type": "modal",
        "callback_id": "remove_suggestion_modal",
        "private_metadata": f"{channel_id}|{requester_id}|{selected_uid or ''}",
        "title": {"type": "plain_text", "text": "Remove Suggestion"},
        "submit": {"type": "plain_text", "text": "Remove Selected"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks
    }


@app.command("/remove-suggestion")
def handle_remove_suggestion(ack, command, client):
    ack()
    client.views_open(
        trigger_id=command["trigger_id"],
        view=_build_remove_modal(command["channel_id"], command["user_id"])
    )


@app.action("remove_user_pick")
def handle_remove_user_pick(ack, body, client, action):
    ack()
    selected_uid = action["selected_option"]["value"]
    meta = body["view"]["private_metadata"].split("|")
    channel_id, requester_id = meta[0], meta[1]
    client.views_update(
        view_id=body["view"]["id"],
        view=_build_remove_modal(channel_id, requester_id, selected_uid)
    )


@app.view("remove_suggestion_modal")
def handle_remove_modal_submit(ack, body, client, view):
    ack()
    meta = view["private_metadata"].split("|")
    channel_id, requester_id = meta[0], meta[1]
    values = view["state"]["values"]

    checked = values.get("suggestions_block", {}).get("suggestions_check", {}).get("selected_options", [])
    if not checked:
        _post_reply(client, channel_id, requester_id, "No suggestions selected — nothing was removed.")
        return

    selected_uid = meta[2] if len(meta) > 2 and meta[2] else requester_id
    all_suggestions = get_active_suggestions(user_id=selected_uid)
    id_to_suggestion = {str(s["id"]): s for s in all_suggestions}

    removed = []
    for opt in checked:
        sid = opt["value"]
        s = id_to_suggestion.get(sid)
        remove_suggestion(int(sid), selected_uid)
        if s:
            emoji = MARKET_EMOJI.get(s["market"], "📈")
            currency = CURRENCY_SYMBOL.get(s["market"], "")
            removed.append(f"{emoji} *{s['ticker']}* (entry: {currency}{float(s['entry_price']):,.2f})")

    lines = "\n".join(f"• {r}" for r in removed)
    _post_reply(client, channel_id, requester_id,
                f"🗑️ Removed {len(removed)} suggestion(s):\n{lines}")


# ── /portfolio command ────────────────────────────────────────────────────────

@app.command("/portfolio")
def handle_portfolio(ack, command, client):
    ack()
    channel_id = command["channel_id"]
    requester = command["user_id"]

    # Build user options from DB
    distinct_users = get_distinct_users()
    user_options = [{"text": {"type": "plain_text", "text": "👥  Everyone"}, "value": "all"}]
    for uid in distinct_users:
        user_options.append({
            "text": {"type": "plain_text", "text": f"<@{uid}>"},
            "value": uid
        })

    client.views_open(
        trigger_id=command["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "portfolio_filter_modal",
            "private_metadata": f"{channel_id}|{requester}",
            "title": {"type": "plain_text", "text": "Portfolio"},
            "submit": {"type": "plain_text", "text": "View"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "user_filter_block",
                    "label": {"type": "plain_text", "text": "Whose portfolio?"},
                    "element": {
                        "type": "static_select",
                        "action_id": "user_filter",
                        "initial_option": {"text": {"type": "plain_text", "text": "👥  Everyone"}, "value": "all"},
                        "options": user_options
                    }
                },
                {
                    "type": "input",
                    "block_id": "market_filter_block",
                    "label": {"type": "plain_text", "text": "Which market?"},
                    "element": {
                        "type": "static_select",
                        "action_id": "market_filter",
                        "initial_option": {"text": {"type": "plain_text", "text": "📊  All Markets"}, "value": "all"},
                        "options": [
                            {"text": {"type": "plain_text", "text": "📊  All Markets"}, "value": "all"},
                        ] + MARKET_OPTIONS
                    }
                }
            ]
        }
    )


@app.view("portfolio_filter_modal")
def handle_portfolio_view(ack, body, client, view):
    ack()
    meta = view["private_metadata"].split("|")
    channel_id, requester = meta[0], meta[1]
    values = view["state"]["values"]

    user_filter   = values["user_filter_block"]["user_filter"]["selected_option"]["value"]
    market_filter = values["market_filter_block"]["market_filter"]["selected_option"]["value"]

    uid    = None if user_filter == "all" else user_filter
    market = None if market_filter == "all" else market_filter

    suggestions = get_active_suggestions(user_id=uid, market=market)

    if not suggestions:
        _post_reply(client, channel_id, requester, "No active suggestions found for that filter.")
        return

    user_label   = "Everyone" if not uid else f"<@{uid}>"
    market_label = "All Markets" if not market else MARKET_LABEL.get(market, market)
    blocks = [{"type": "header", "text": {"type": "plain_text",
               "text": f"📊  Portfolio — {user_label} · {market_label}"}}]

    for s in suggestions:
        currency = CURRENCY_SYMBOL.get(s["market"], "$")
        emoji    = MARKET_EMOJI.get(s["market"], "📈")
        entry    = float(s["entry_price"])

        current_price, _, _ = get_price(s["ticker"], s["market"])
        if current_price:
            pnl     = ((current_price - entry) / entry) * 100
            pnl_str = f"{pnl:+.2f}%"
            cur_str = f"{currency}{current_price:,.2f}"
            trend   = "🟢" if pnl >= 0 else "🔴"
        else:
            pnl_str = "N/A"
            cur_str = "N/A"
            trend   = "⚪"

        roi_target = f"+{float(s['expected_roi']):.1f}%" if float(s['expected_roi']) else "—"
        blocks.append({
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*{trend} {emoji} {s['ticker']}*\nEntry: {currency}{entry:,.2f}"},
                {"type": "mrkdwn", "text": f"*Now:* {cur_str}   *P&L:* {pnl_str}\n*Target:* {roi_target}   *By:* <@{s['user_id']}>"},
            ]
        })
        blocks.append({"type": "divider"})

    _post_reply(client, channel_id, requester,
                f"Portfolio — {user_label} · {market_label}", blocks=blocks)


def _owner_only(command, client):
    """Returns True if caller is owner, posts error and returns False otherwise."""
    from trader.config import OWNER_SLACK_ID
    if command["user_id"] != OWNER_SLACK_ID:
        client.chat_postEphemeral(
            channel=command["channel_id"],
            user=command["user_id"],
            text="🚫 This command is restricted to the workspace owner."
        )
        return False
    return True


# ── /kite-auth command ────────────────────────────────────────────────────────

@app.command("/kite-auth")
def handle_kite_auth(ack, command, client):
    """Trigger Kite auto-login using stored TOTP secret (no manual TOTP needed)."""
    ack()
    if not _owner_only(command, client): return
    channel_id = command["channel_id"]
    user_id    = command["user_id"]
    try:
        from trader.kite import auto_login
        auto_login()
        _post_reply(client, channel_id, user_id,
                    "✅ Kite authorized. FnO trading active until 6 AM tomorrow.")
    except Exception as e:
        msg = str(e)
        if msg.startswith("NEEDS_CONSENT:"):
            authorize_url = msg.split("NEEDS_CONSENT:", 1)[1]
            _post_reply(client, channel_id, user_id,
                f"🔐 *One-time Kite authorization needed:*\n"
                f"1. Open this URL in your browser:\n{authorize_url}\n"
                f"2. Click *Allow* on the page\n"
                f"3. Browser will show a failed page — copy the `request_token` from the URL bar\n"
                f"   _(URL looks like: `127.0.0.1/?request_token=XXXXXX&...`)_\n"
                f"4. Run `/kite-token XXXXXX` with just the token value\n\n"
                f"_After this one-time step, `/kite-auth` will be fully automatic forever._"
            )
        else:
            _post_reply(client, channel_id, user_id, f"❌ Kite auth failed: {e}")


@app.command("/kite-token")
def handle_kite_token(ack, command, client):
    """Complete Kite auth using request_token from browser after manual consent."""
    ack()
    if not _owner_only(command, client): return
    channel_id = command["channel_id"]
    user_id    = command["user_id"]
    token = (command.get("text") or "").strip()
    if not token:
        _post_reply(client, channel_id, user_id,
                    "Usage: `/kite-token <request_token>` — paste token from browser URL bar")
        return
    try:
        import os
        from kiteconnect import KiteConnect
        from trader.kite import _store_token, _kite
        api_key    = os.environ["KITE_API_KEY"]
        api_secret = os.environ["KITE_API_SECRET"]
        kite = KiteConnect(api_key=api_key)
        session_data = kite.generate_session(token, api_secret=api_secret)
        access_token = session_data["access_token"]
        _store_token(access_token)
        kite.set_access_token(access_token)
        _post_reply(client, channel_id, user_id,
                    "✅ Kite authorized via token. FnO trading active — future `/kite-auth` calls are now fully automatic.")
    except Exception as e:
        _post_reply(client, channel_id, user_id, f"❌ Token exchange failed: {e}")


# ── /trade-test command ────────────────────────────────────────────────────────

@app.command("/trade-test")
def handle_trade_test(ack, command, client):
    """Place a $10 BTC long limit order at 1x leverage to verify connectivity."""
    ack()
    if not _owner_only(command, client): return
    channel_id = command["channel_id"]
    user_id    = command["user_id"]
    try:
        from trader.binance import get_exchange, get_futures_balance
        ex = get_exchange()

        symbol   = "ETH/USDT:USDT"
        leverage = 1
        margin   = 25.0  # $25 — safely above Binance $20 minimum notional

        # Fetch current ETH price
        ticker      = ex.fetch_ticker(symbol)
        price       = float(ticker["last"])
        # Limit slightly below market so it sits as open order (real limit test)
        limit_price = ex.price_to_precision(symbol, price * 0.995)
        # Respect Binance minimum: 0.001 ETH
        raw_amount  = margin / price
        market      = ex.market(symbol)
        min_amount  = float(market.get("limits", {}).get("amount", {}).get("min") or 0.001)
        amount      = ex.amount_to_precision(symbol, max(raw_amount, min_amount))

        # Set 1x isolated
        try: ex.set_margin_mode("isolated", symbol)
        except Exception: pass
        try: ex.set_leverage(leverage, symbol)
        except Exception: pass

        order = ex.create_order(symbol, "limit", "buy", amount, limit_price, {
            "timeInForce":  "GTC",
            "positionSide": "BOTH",
        })

        balance = get_futures_balance()
        _post_reply(client, channel_id, user_id,
            f"✅ *Test order placed!*\n"
            f"• Symbol: `{symbol}` | Side: LONG | 1x\n"
            f"• Amount: `{amount} ETH` | Limit: `${float(limit_price):,.2f}`\n"
            f"• Order ID: `{order['id']}`\n"
            f"• Wallet balance: `${balance:.2f} USDT`\n"
            f"_Binance connection is working. Cancel this order manually on Binance._"
        )
    except Exception as e:
        _post_reply(client, channel_id, user_id, f"❌ Test order failed: {e}")


# ── /trade-active command ──────────────────────────────────────────────────────

@app.command("/trade-active")
def handle_trade_active(ack, command, client):
    ack()
    if not _owner_only(command, client): return
    market = (command.get("text") or "crypto").strip().lower()
    channel_id, user_id = command["channel_id"], command["user_id"]

    if market in ("kite", "fno", "dalal"):
        _show_kite_active(client, channel_id, user_id)
    else:
        _show_crypto_active(client, channel_id, user_id)


def _show_crypto_active(client, channel_id, user_id):
    from trader.binance import get_open_positions, get_futures_balance
    from trader.engine import is_paused, get_total_loss_usdt
    positions  = get_open_positions()
    balance    = get_futures_balance()
    total_loss = get_total_loss_usdt()
    status     = "🔴 PAUSED" if is_paused() else "🟢 ACTIVE"
    if not positions:
        _post_reply(client, channel_id, user_id,
                    f"Crypto {status} | Balance: ${balance:.2f} USDT | Loss: ${total_loss:.2f} USDT\nNo open positions.")
        return
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"⚡ Crypto Positions {status}"}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Wallet:*\n${balance:.2f} USDT"},
            {"type": "mrkdwn", "text": f"*Total Loss:*\n${total_loss:.2f} USDT"},
        ]}, {"type": "divider"},
    ]
    for p in positions:
        pnl   = p["unrealized_pnl"]
        trend = "🟢" if pnl >= 0 else "🔴"
        blocks.append({"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*{trend} {p['symbol']}*\n{p['side'].upper()} {int(p['leverage'])}x"},
            {"type": "mrkdwn", "text": f"*Entry:* ${p['entry_price']:,.4f}\n*Mark:* ${p['mark_price']:,.4f}"},
            {"type": "mrkdwn", "text": f"*Unrealized P&L:*\n{pnl:+.2f} USDT"},
            {"type": "mrkdwn", "text": f"*Liq. Price:*\n${p['liq_price']:,.4f}"},
        ]})
        blocks.append({"type": "divider"})
    _post_reply(client, channel_id, user_id, "Crypto positions", blocks=blocks)


def _show_kite_active(client, channel_id, user_id):
    from trader.kite import get_open_positions as kite_positions
    from trader.kite_engine import is_kite_paused, get_kite_total_loss_inr, get_kite_capital
    positions  = kite_positions()
    capital    = get_kite_capital()
    total_loss = get_kite_total_loss_inr()
    status     = "🔴 PAUSED" if is_kite_paused() else "🟢 ACTIVE"
    if not positions:
        _post_reply(client, channel_id, user_id,
                    f"Kite FnO {status} | Capital: ₹{capital:,.0f} | Loss: ₹{total_loss:,.0f}\nNo open positions.")
        return
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"⚡ Kite FnO Positions {status}"}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Capital:*\n₹{capital:,.0f}"},
            {"type": "mrkdwn", "text": f"*Total Loss:*\n₹{total_loss:,.0f}"},
        ]}, {"type": "divider"},
    ]
    for p in positions:
        trend = "🟢" if p["pnl"] >= 0 else "🔴"
        blocks.append({"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*{trend} {p['tradingsymbol']}*"},
            {"type": "mrkdwn", "text": f"*Avg Price:*\n₹{p['average_price']:.2f}"},
            {"type": "mrkdwn", "text": f"*LTP:*\n₹{p['last_price']:.2f}"},
            {"type": "mrkdwn", "text": f"*P&L:*\n₹{p['pnl']:+,.0f}"},
        ]})
        blocks.append({"type": "divider"})
    _post_reply(client, channel_id, user_id, "Kite positions", blocks=blocks)


# ── /trade-pause & /trade-resume ──────────────────────────────────────────────

@app.command("/trade-pause")
def handle_trade_pause(ack, command, client):
    ack()
    if not _owner_only(command, client): return
    market = (command.get("text") or "crypto").strip().lower()
    msgs = []
    if market in ("crypto", "both"):
        from trader.engine import pause_trading; pause_trading()
        msgs.append("🔴 Crypto paused")
    if market in ("kite", "fno", "dalal", "both"):
        from trader.kite_engine import pause_kite; pause_kite()
        msgs.append("🔴 Kite FnO paused")
    if not msgs:
        msgs.append("Usage: `/trade-pause crypto` | `kite` | `both`")
    _post_reply(client, command["channel_id"], command["user_id"], " | ".join(msgs))


@app.command("/trade-resume")
def handle_trade_resume(ack, command, client):
    ack()
    if not _owner_only(command, client): return
    market = (command.get("text") or "crypto").strip().lower()
    msgs = []
    if market in ("crypto", "both"):
        from trader.engine import resume_trading; resume_trading()
        msgs.append("🟢 Crypto resumed")
    if market in ("kite", "fno", "dalal", "both"):
        from trader.kite_engine import resume_kite; resume_kite()
        msgs.append("🟢 Kite FnO resumed")
    if not msgs:
        msgs.append("Usage: `/trade-resume crypto` | `kite` | `both`")
    _post_reply(client, command["channel_id"], command["user_id"], " | ".join(msgs))


# ── /trade-target command ─────────────────────────────────────────────────────

@app.command("/trade-target")
def handle_trade_target(ack, command, client):
    ack()
    if not _owner_only(command, client): return
    channel_id, user_id = command["channel_id"], command["user_id"]
    text = (command.get("text") or "").strip()

    # Parse: "/trade-target crypto 40" or "/trade-target kite 35" or "/trade-target crypto"
    parts  = text.split()
    market = parts[0].lower() if parts else "crypto"
    raw    = parts[1].replace("%", "").replace(",", "") if len(parts) > 1 else ""

    is_kite = market in ("kite", "fno", "dalal")

    if not raw:
        # Show current target
        if is_kite:
            from trader.memory import get_kite_month_snapshot
            snap = get_kite_month_snapshot()
        else:
            from trader.memory import get_month_snapshot
            snap = get_month_snapshot()
        label = "Kite FnO" if is_kite else "Crypto"
        if snap:
            _post_reply(client, channel_id, user_id,
                        f"📅 {label} target: *{snap['target_pct']:.0f}%*\n"
                        f"Usage: `/trade-target {market} 35` to update.")
        else:
            _post_reply(client, channel_id, user_id,
                        f"No {label} snapshot yet. Usage: `/trade-target {market} 35`")
        return

    try:
        target_pct = float(raw)
    except ValueError:
        _post_reply(client, channel_id, user_id,
                    f"Invalid. Usage: `/trade-target crypto 40` or `/trade-target kite 35`")
        return

    if is_kite:
        from trader.memory import update_kite_monthly_target
        update_kite_monthly_target(target_pct)
        label = "Kite FnO"
    else:
        from trader.memory import update_monthly_target
        update_monthly_target(target_pct)
        label = "Crypto"

    _post_reply(client, channel_id, user_id,
                f"✅ {label} monthly target set to *{target_pct:.0f}%*.")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    from trader.memory import (
        init_trader_db, init_month_snapshot, get_month_snapshot,
        init_kite_month_snapshot, get_kite_month_snapshot
    )
    from trader.binance import get_futures_balance
    from trader.kite_engine import get_kite_capital
    from trader.config import MONTHLY_TARGET_PCT, KITE_MONTHLY_TARGET_PCT
    init_trader_db()
    # Seed crypto month snapshot
    try:
        if not get_month_snapshot():
            init_month_snapshot(get_futures_balance(), MONTHLY_TARGET_PCT)
    except Exception as _e:
        logging.warning(f"Could not init crypto month snapshot: {_e}")
    # Seed Kite month snapshot
    try:
        if not get_kite_month_snapshot():
            init_kite_month_snapshot(get_kite_capital(), KITE_MONTHLY_TARGET_PCT)
    except Exception as _e:
        logging.warning(f"Could not init Kite month snapshot: {_e}")
    start_scheduler(app)
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    try:
        from trader.config import OWNER_SLACK_ID
        from datetime import datetime
        import pytz
        now = datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%d %b %Y, %I:%M %p IST")
        app.client.chat_postMessage(
            channel=OWNER_SLACK_ID,
            text=f"🚀 *Chanakya Bot deployed* — {now}\nCrypto scanner active. Use `/kite-auth` to activate FnO."
        )
    except Exception:
        pass
    logging.info("Chanakya Bot is running...")
    handler.start()
