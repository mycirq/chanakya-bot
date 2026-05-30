"""CLI entry point: python -m trader.backtest [args]

Examples:
  # Refresh universe + fetch last 90d 5min data + run all 3 setups
  python -m trader.backtest --days 90 --setup all --refresh-universe

  # Run only ORB on cached data
  python -m trader.backtest --setup orb

  # Limit to a small universe for fast iteration
  python -m trader.backtest --setup all --limit 20
"""
import argparse
import logging
import sys
from datetime import date, timedelta

import pandas as pd

from trader.backtest.config import CAPITAL_INR, DEFAULT_LOOKBACK_DAYS, DATA_DIR
from trader.backtest.universe import load_universe
from trader.backtest.data import fetch_universe_history, fetch_symbol_history
from trader.backtest.engine import BacktestEngine
from trader.backtest.report import (
    trades_to_df, summary_by_setup, daily_pnl, overall_summary
)
from trader.backtest.setups.orb import ORBSetup
from trader.backtest.setups.vwap_pullback import VWAPPullbackSetup
from trader.backtest.setups.sector_leader import SectorLeaderSetup
from trader.backtest.setups.gap_fade import GapFadeSetup

SETUP_REGISTRY = {
    "orb":      ORBSetup,
    "vwap":     VWAPPullbackSetup,
    "sector":   SectorLeaderSetup,
    "gap_fade": GapFadeSetup,
}

# NIFTY 50 instrument token (Kite NSE) — used as market context for SECTOR_LEADER
NIFTY50_TOKEN = 256265
NIFTY50_SYMBOL = "NIFTY 50"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=DEFAULT_LOOKBACK_DAYS,
                   help=f"Days of history to backtest (default {DEFAULT_LOOKBACK_DAYS})")
    p.add_argument("--setup",
                   choices=["orb", "vwap", "sector", "gap_fade", "all"],
                   default="all")
    p.add_argument("--limit", type=int, default=0,
                   help="Cap universe to first N symbols (0 = no cap, useful for dev)")
    p.add_argument("--refresh-universe", action="store_true")
    p.add_argument("--refresh-data", action="store_true",
                   help="Force re-fetch even if cached")
    p.add_argument("--out-prefix", default="backtest",
                   help="Filename prefix for output CSVs in ~/.chanakya-backtest-data/")
    p.add_argument("--direction", choices=["long", "short", "both"], default="both",
                   help="Restrict to long-only or short-only entries (default both)")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    to_date   = date.today()
    from_date = to_date - timedelta(days=args.days)

    # ── Universe ────────────────────────────────────────────────────────
    print(f"Loading universe (refresh={args.refresh_universe})...")
    universe = load_universe(refresh=args.refresh_universe)
    if args.limit > 0:
        universe = universe.head(args.limit)
    print(f"  → {len(universe)} symbols")

    # ── Historical data ────────────────────────────────────────────────
    print(f"Fetching 5-min candles {from_date} → {to_date}...")
    sym_data = fetch_universe_history(
        universe, from_date, to_date,
        interval="5minute", force_refresh=args.refresh_data,
    )
    print(f"  → {len(sym_data)} symbols with data")
    if not sym_data:
        print("ERROR: no symbol data fetched. Check KITE_ACCESS_TOKEN and PROXY_URL.")
        sys.exit(1)

    # NIFTY 50 for sector_leader context
    market_df = None
    setups_to_run = (
        list(SETUP_REGISTRY.values())
        if args.setup == "all"
        else [SETUP_REGISTRY[args.setup]]
    )
    if SectorLeaderSetup in setups_to_run:
        print(f"Fetching NIFTY 50 for market context...")
        try:
            market_df = fetch_symbol_history(
                NIFTY50_SYMBOL, NIFTY50_TOKEN, from_date, to_date,
                interval="5minute", force_refresh=args.refresh_data,
            )
            print(f"  → {len(market_df)} NIFTY 50 candles")
        except Exception as e:
            print(f"  WARN: failed to fetch NIFTY 50 ({e}); SECTOR_LEADER will skip")

    # ── Run replay ─────────────────────────────────────────────────────
    print(f"Running replay with setups: {[s.name for s in setups_to_run]}")
    allowed = ("long", "short") if args.direction == "both" else (args.direction,)
    engine = BacktestEngine(
        symbol_data=sym_data,
        setup_classes=setups_to_run,
        market_index_df=market_df,
        capital=CAPITAL_INR,
        allowed_directions=allowed,
    )
    trades = engine.run()

    # ── Reports ────────────────────────────────────────────────────────
    overall = overall_summary(trades, CAPITAL_INR)
    by_setup = summary_by_setup(trades)
    daily    = daily_pnl(trades)
    trades_df = trades_to_df(trades)

    print("\n" + "=" * 60)
    print("OVERALL")
    print("=" * 60)
    for k, v in overall.items():
        print(f"  {k:25s} {v}")

    print("\n" + "=" * 60)
    print("BY SETUP")
    print("=" * 60)
    if not by_setup.empty:
        print(by_setup.to_string(index=False))
    else:
        print("  (no trades)")

    print("\n" + "=" * 60)
    print("DAILY (last 10 days)")
    print("=" * 60)
    if not daily.empty:
        print(daily.tail(10).to_string(index=False))

    # Write CSVs
    out_trades = DATA_DIR / f"{args.out_prefix}_trades.csv"
    out_daily  = DATA_DIR / f"{args.out_prefix}_daily.csv"
    out_setup  = DATA_DIR / f"{args.out_prefix}_by_setup.csv"
    trades_df.to_csv(out_trades, index=False)
    daily.to_csv(out_daily, index=False)
    by_setup.to_csv(out_setup, index=False)
    print(f"\nWrote: {out_trades}, {out_daily}, {out_setup}")


if __name__ == "__main__":
    main()
