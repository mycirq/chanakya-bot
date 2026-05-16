import pytz

IST = pytz.timezone("Asia/Kolkata")

# ── Portfolio limits ───────────────────────────────────────────────────────────
HARD_STOP_INR      = 40_000   # freeze all trading
WARNING_INR        = 35_000   # Slack alert, still trading
INR_TO_USDT        = 0.012    # ~83 INR per USD; update periodically
HARD_STOP_USDT     = HARD_STOP_INR * INR_TO_USDT
WARNING_USDT       = WARNING_INR  * INR_TO_USDT

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

# ── Slack channels ─────────────────────────────────────────────────────────────
CRYPTO_TRADES_CHANNEL = "crypto-trades"

# ── Indicator params ───────────────────────────────────────────────────────────
RSI_PERIOD         = 14
MACD_FAST          = 12
MACD_SLOW          = 26
MACD_SIGNAL        = 9
EMA_FAST           = 50
EMA_SLOW           = 200
BB_PERIOD          = 20
BB_STD             = 2
