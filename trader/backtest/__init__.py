"""Equity intraday backtest harness.

Replays setups against historical 5-min candles from Kite. Setup functions
are pure — same module can be reused if/when we go live.

## Usage

Requires extra local deps not in requirements.txt:
    python -m venv .venv && source .venv/bin/activate
    pip install kiteconnect pandas ta pyarrow pyotp psycopg2-binary pysocks pytz

Env vars (auto-loaded via `railway run --service sunny-luck` if you don't
want to copy them manually):
    KITE_API_KEY        — from Railway env
    KITE_ACCESS_TOKEN   — fresh daily; get one with:
        railway run --service sunny-luck python -c \\
          "from trader.kite import auto_login; print(auto_login())"
    PROXY_URL           — from Railway env (Kite is IP-whitelisted to Oracle VM)

Then:
    python -m trader.backtest --days 90 --setup all --limit 50

See `__main__.py --help` for all flags.

## Status (as of 2026-05-30)

All five setups tested (ORB ×2, VWAP, Sector Leader, Gap Fade) showed
negative expectancy at 211-symbol scale. Harness is correct; the market
is efficient enough that naive 5-min technical setups don't survive
Zerodha intraday costs. Kept for future use — try event-driven, swing,
or ML-scored strategies next.
"""
