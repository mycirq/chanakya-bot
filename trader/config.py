import pytz

IST = pytz.timezone("Asia/Kolkata")

# ── Owner ─────────────────────────────────────────────────────────────────────
OWNER_SLACK_ID     = "U0B2PS4SSQ6"

# ── Portfolio limits ───────────────────────────────────────────────────────────
CAPITAL_USDT       = 964.0    # total futures wallet capital
HARD_STOP_USDT     = 480.0    # freeze all trading (~₹40k)
WARNING_USDT       = 420.0    # Slack alert, still trading (~₹35k)

# ── Monthly target ─────────────────────────────────────────────────────────────
# Reviewed at month end — owner updates via /trade-target each month
MONTHLY_TARGET_PCT = 40.0     # 40% return target this month

# ── Position limits ────────────────────────────────────────────────────────────
MAX_LEVERAGE       = 5
MAX_POSITIONS      = 5
MAX_POSITION_PCT   = 0.20     # max 20% of wallet per trade
MIN_RR_RATIO       = 1.5      # min reward:risk

# ── Strategy ───────────────────────────────────────────────────────────────────
SCAN_INTERVAL_MIN  = 15       # scan every 15 minutes
TOP_PAIRS_COUNT    = 30       # scan top N pairs by volume
MIN_SIGNAL_SCORE   = 65       # 0-100, only trade above this

# ── Trading zones IST (hour, minute) ──────────────────────────────────────────
# Each tuple: (start_hour, start_min, end_hour, end_min)
ZONES = {
    "high": [
        (5,  30,  9, 30),
        (13,  0, 17,  0),
        (18, 30, 23, 30),
    ],
    "limited": [
        (9,  30, 13,  0),
        (17,  0, 18, 30),
    ],
    # dead: 23:30 – 05:30 IST → no new trades
}

# ── Kite FnO ───────────────────────────────────────────────────────────────────
KITE_CAPITAL_INR        = 100000.0   # ₹1,00,000 total capital
KITE_HARD_STOP_INR      = 40000.0    # freeze all trading
KITE_WARNING_INR        = 30000.0    # Slack alert, still trading
KITE_MONTHLY_TARGET_PCT = 40.0       # 40% monthly target
KITE_MAX_POSITIONS      = 3          # max open FnO positions
KITE_MAX_POSITION_PCT   = 0.20       # max 20% capital per trade
KITE_PREMIUM_TP_PCT     = 0.50       # 50% gain on premium → TP
KITE_PREMIUM_SL_PCT     = 0.30       # 30% loss on premium → SL
KITE_MIN_SIGNAL_SCORE   = 65         # same threshold as crypto
KITE_SCAN_INTERVAL_MIN  = 5          # scan every 5 min during market hours

# Market hours IST
KITE_MARKET_OPEN  = (9, 15)          # 9:15 AM
KITE_MARKET_CLOSE = (15, 25)         # 15:25 (stop new trades 5 min before close)

# Index config — lot sizes from instrument file at runtime, these are fallbacks
KITE_INDICES = {
    "NIFTY": {
        "token":      256265,          # NSE:NIFTY 50 instrument token
        "quote":      "NSE:NIFTY 50",
        "strike_gap": 50,
        "lot_size":   75,              # fallback — always read from instruments
    },
    "BANKNIFTY": {
        "token":      260105,          # NSE:NIFTY BANK instrument token
        "quote":      "NSE:NIFTY BANK",
        "strike_gap": 100,
        "lot_size":   35,              # fallback
    },
}

# ── Slack channels ─────────────────────────────────────────────────────────────
CRYPTO_TRADES_CHANNEL = "crypto-trades"
KITE_TRADES_CHANNEL   = "dalal-trades"

# ── Indicator params ───────────────────────────────────────────────────────────
RSI_PERIOD         = 14
MACD_FAST          = 12
MACD_SLOW          = 26
MACD_SIGNAL        = 9
EMA_FAST           = 50
EMA_SLOW           = 200
BB_PERIOD          = 20
BB_STD             = 2
