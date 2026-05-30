"""Backtest configuration: paths, market hours, costs, risk caps.

Times are IST. Costs match Zerodha equity intraday MIS as of 2026.
"""
from pathlib import Path
from datetime import time

DATA_DIR = Path.home() / ".chanakya-backtest-data"
DATA_DIR.mkdir(exist_ok=True)
CANDLES_DIR = DATA_DIR / "candles_5min"
CANDLES_DIR.mkdir(exist_ok=True)
UNIVERSE_FILE = DATA_DIR / "fno_universe.parquet"

# Market session (IST)
MARKET_OPEN          = time(9, 15)
OPENING_RANGE_END    = time(9, 30)   # ORB range = 9:15-9:30 (first 15 min)
EARLIEST_ENTRY       = time(9, 15)   # global floor; setups self-gate further (e.g., ORB requires 9:30+)
LATEST_ENTRY         = time(14, 45)  # no new entries after 2:45 PM
SQUARE_OFF           = time(15, 15)  # exit all by 3:15 PM (Zerodha auto-square at 3:20)
MARKET_CLOSE         = time(15, 30)
PROFIT_LOCK_TIME     = time(13, 0)

# Costs (per leg unless noted)
BROKERAGE_PER_ORDER  = 20.0           # Zerodha flat ₹20/order
STT_PCT              = 0.00025        # 0.025% on sell side only (intraday)
EXCHANGE_TXN_PCT     = 0.0000345      # NSE
GST_PCT              = 0.18           # on brokerage + exchange charges
STAMP_DUTY_PCT       = 0.00003        # buy side only, 0.003%
SEBI_CHARGES_PCT     = 0.000001       # ₹10 per crore
SLIPPAGE_PCT         = 0.0005         # 0.05% slippage assumed each leg

# Risk caps
CAPITAL_INR              = 100_000
MAX_RISK_PER_TRADE_INR   = 1_500   # 1.5% of capital
MAX_CONCURRENT_POSITIONS = 3
DAILY_LOSS_CIRCUIT_INR   = 4_000   # -4% — stop new entries
DAILY_PROFIT_LOCK_INR    = 4_000   # +4% by 1 PM → halve per-trade risk for rest of day

# Backtest run defaults
DEFAULT_LOOKBACK_DAYS = 90
DEFAULT_INTERVAL      = "5minute"   # Kite intervals: minute, 3minute, 5minute, 10minute, 15minute, 30minute, 60minute, day
