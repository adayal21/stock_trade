# =============================================================================
# config.py — India Bot (Zerodha Kite Connect)
# =============================================================================
# Strategy  : Supertrend(10,3) + Nifty EMA(50) regime filter
# Candles   : Daily
# Stocks    : 7 instruments — 5 Nifty 50 stocks + 2 ETFs
# Capital   : Equal allocation per stock, max 5 active positions
# Exit      : Supertrend flips bearish
# Cron      : Run at 15:45 IST (15 min after market close)
# =============================================================================

import os
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

IST = ZoneInfo("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Trading mode
# ---------------------------------------------------------------------------
TRADING_MODE = os.environ.get("TRADING_MODE", "paper")

# ---------------------------------------------------------------------------
# Capital
# ---------------------------------------------------------------------------
INITIAL_CAPITAL = float(os.environ.get("INITIAL_CAPITAL", "10000"))

# ---------------------------------------------------------------------------
# Data directory
# ---------------------------------------------------------------------------
DATA_DIR = "data"

# ---------------------------------------------------------------------------
# Instruments
# ---------------------------------------------------------------------------
# Trading symbol format for Kite Connect: NSE tradingsymbol
# Yahoo Finance ticker used for historical data fetch (for backtesting)
#
# Selected based on Strategy 4 (Supertrend + Nifty regime) backtest results:
#   BAJFINANCE : Sharpe 0.94 | +754% | MaxDD -29% — best performer
#   TITAN      : Sharpe 0.78 | +395% | MaxDD -30%
#   RELIANCE   : Sharpe 0.61 | +195% | MaxDD -22%
#   MARUTI     : Sharpe 0.56 | +168% | MaxDD -39%
#   LT         : Sharpe 0.55 | +161% | MaxDD -33%
#   NIFTYBEES  : Used with Strategy 3 (RSI Momentum) — Sharpe 0.88
#   BANKBEES   : Used with Strategy 3 (RSI Momentum) — Sharpe 0.68
#
# Format: (kite_tradingsymbol, exchange, yahoo_ticker, strategy, name)

INSTRUMENTS = [
    # Rank 1-5: Individual stocks — Supertrend strategy
    ("BAJFINANCE",  "NSE", "BAJFINANCE.NS",  "supertrend", "Bajaj Finance"),
    ("TITAN",       "NSE", "TITAN.NS",        "supertrend", "Titan Company"),
    ("RELIANCE",    "NSE", "RELIANCE.NS",     "supertrend", "Reliance Industries"),
    ("MARUTI",      "NSE", "MARUTI.NS",       "supertrend", "Maruti Suzuki"),
    ("LT",          "NSE", "LT.NS",           "supertrend", "Larsen & Toubro"),

    # Rank 6-7: ETFs — RSI Momentum strategy (cleaner data, no gap risk)
    ("NIFTYBEES",   "NSE", "NIFTYBEES.NS",   "rsi_momentum", "Nifty BeES ETF"),
    ("BANKBEES",    "NSE", "BANKBEES.NS",     "rsi_momentum", "Bank BeES ETF"),
]

# Nifty 50 index — used as regime filter for Supertrend strategy
NIFTY_YAHOO = "^NSEI"

# ---------------------------------------------------------------------------
# Strategy parameters — Supertrend (Strategy 4)
# ---------------------------------------------------------------------------
ST_PERIOD       = 10    # Supertrend ATR period
ST_MULTIPLIER   = 3.0   # Supertrend multiplier
NIFTY_EMA_LEN   = 50    # Nifty EMA period for regime filter

# ---------------------------------------------------------------------------
# Strategy parameters — RSI Momentum (Strategy 3, ETFs only)
# ---------------------------------------------------------------------------
RSI_PERIOD      = 14
RSI_ENTRY       = 60    # RSI crosses above this to enter
RSI_EXIT        = 50    # RSI drops below this to exit
EMA_200_LEN     = 200   # Trend filter
VOL_AVG_LEN     = 50    # Volume baseline

# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------
# Equal allocation per instrument
# Max 5 positions open simultaneously (2 slots always reserved for ETFs)
ALLOCATION_PCT     = 1.0 / len(INSTRUMENTS)   # ~14.3% per instrument
MAX_OPEN_POSITIONS = 5

# Minimum shares to buy (Kite requires integer quantities for equities)
MIN_QUANTITY = 1

# ---------------------------------------------------------------------------
# Order settings (live mode)
# ---------------------------------------------------------------------------
# CNC = Cash & Carry (delivery, for positional trades held overnight)
# This is the correct product type for our strategy
PRODUCT_TYPE   = "CNC"
ORDER_TYPE     = "MARKET"
EXCHANGE       = "NSE"

# ---------------------------------------------------------------------------
# Historical data
# ---------------------------------------------------------------------------
# Kite historical API limits per request:
#   daily candles: 2000 days per request (~5.5 years)
# We fetch enough bars for indicators to warm up properly
HISTORY_DAYS   = 400    # ~400 trading days (~1.6 years) enough for indicators
KITE_INTERVAL  = "day"  # daily candles

# ---------------------------------------------------------------------------
# Commission (used in paper mode simulation)
# ---------------------------------------------------------------------------
# 0.4% round trip = 0.2% buy + 0.2% sell
# Covers: Zerodha brokerage (₹20/order or 0.03%), STT (0.1%), exchange fees, GST
COMMISSION_RT  = 0.004   # round trip

# ---------------------------------------------------------------------------
# Telegram notifications (optional)
# ---------------------------------------------------------------------------
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------
VERBOSE = False

# ---------------------------------------------------------------------------
# Cron schedule
# ---------------------------------------------------------------------------
# NSE market closes at 15:30 IST
# Run at 15:45 IST daily (Monday to Friday, excluding holidays)
# GCP cron (in /etc/cron.d/india_bot):
#   15 10 * * 1-5 ubuntu cd /home/ubuntu/Projects/india_bot && python main.py >> data/bot.log 2>&1
# Note: GCP uses UTC. 15:45 IST = 10:15 UTC