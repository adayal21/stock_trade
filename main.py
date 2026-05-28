"""
main.py — India Bot (Zerodha Kite Connect)
==========================================
Runs ONCE daily at 15:45 IST via cron, after NSE closes at 15:30.

CANDLE TIMEFRAME: DAILY (1 candle = 1 full trading day)
The bot looks at today's closing price only. No intraday monitoring.
No hourly checks. One run per day. That's it.

HOW SIGNALS WORK:
    The Supertrend indicator changes only when a daily candle closes.
    So there is nothing to check between 15:45 today and 15:45 tomorrow.
    The bot runs, acts on today's close, places AMO orders for tomorrow
    morning, then does NOTHING until the next 15:45 cron.

FILES CREATED IN data/ FOLDER:
    portfolio.csv              - equity curve, one row per day
    run_log.csv                - one row per cron run with full summary
    {SYMBOL}_position.csv      - currently open position for that stock
                                 (deleted automatically when trade exits)
    {SYMBOL}_trades.csv        - complete history of all closed trades

POSITION CSV FORMAT (e.g. BAJFINANCE_position.csv):
    Symbol, Side, Entry Price, Quantity, Entry Date, Bars_Held, Strategy
    One row only. Overwritten each day to update Bars_Held.
    Deleted when position is exited.

TRADES CSV FORMAT (e.g. BAJFINANCE_trades.csv):
    Symbol, Side, Entry Price, Exit Price, Quantity, PnL, Exit Reason, Entry Date, Exit Date
    One row per completed trade. Appended, never overwritten.

STRATEGY:
    Stocks (BAJFINANCE, TITAN, RELIANCE, MARUTI, LT):
        Entry: Supertrend(10,3) flips bullish AND Nifty > EMA(50)
        Exit:  Supertrend(10,3) flips bearish
        Hold:  Typically 30-90 days

    ETFs (NIFTYBEES, BANKBEES):
        Entry: RSI(14) crosses above 60 AND volume > 50d avg AND close > EMA(200)
        Exit:  RSI(14) drops below 50
        Hold:  Typically 20-60 days

ORDER TYPE: AMO (After Market Order)
    Placed at 15:45 IST after market close.
    Executes at next morning market open ~9:15 AM IST.
    Product: CNC (Cash & Carry = delivery, held overnight)
"""

import os
import sys
import time
import logging
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

try:
    import pandas_ta as ta
except ImportError:
    print("ERROR: pip install pandas_ta")
    sys.exit(1)

try:
    from kiteconnect import KiteConnect
    from kiteconnect.exceptions import KiteException
except ImportError:
    print("ERROR: pip install kiteconnect")
    sys.exit(1)

try:
    from dotenv import load_dotenv
except ImportError:
    print("ERROR: pip install python-dotenv")
    sys.exit(1)

load_dotenv()

from config import (
    TRADING_MODE, INITIAL_CAPITAL, DATA_DIR, INSTRUMENTS, NIFTY_YAHOO,
    ST_PERIOD, ST_MULTIPLIER, NIFTY_EMA_LEN,
    RSI_PERIOD, RSI_ENTRY, RSI_EXIT, EMA_200_LEN, VOL_AVG_LEN,
    ALLOCATION_PCT, MAX_OPEN_POSITIONS, MIN_QUANTITY,
    PRODUCT_TYPE, ORDER_TYPE, EXCHANGE,
    HISTORY_DAYS, KITE_INTERVAL,
    COMMISSION_RT, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, VERBOSE, IST,
)

os.makedirs(DATA_DIR, exist_ok=True)

# =============================================================================
# Logging setup — writes to data/bot.log AND console simultaneously
# =============================================================================

LOG_FILE = f"{DATA_DIR}/bot.log"

logging.basicConfig(
    level    = logging.INFO,
    format   = "%(asctime)s IST | %(message)s",
    datefmt  = "%Y-%m-%d %H:%M:%S",
    handlers = [
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("india_bot")


def sep(char="=", width=62):
    log.info(char * width)


# =============================================================================
# Kite Connect
# =============================================================================

def get_kite() -> KiteConnect:
    api_key      = os.environ.get("KITE_API_KEY", "").strip()
    access_token = os.environ.get("KITE_ACCESS_TOKEN", "").strip()
    if not api_key:
        log.error("KITE_API_KEY not set in .env")
        sys.exit(1)
    if not access_token:
        log.error("KITE_ACCESS_TOKEN not set — run kite_auth.py first")
        sys.exit(1)
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    return kite


# =============================================================================
# Position file helpers
# =============================================================================

def _safe(symbol: str) -> str:
    return symbol.replace("/", "_")

def get_position_file(symbol: str) -> str:
    return f"{DATA_DIR}/{_safe(symbol)}_position.csv"

def get_trade_file(symbol: str) -> str:
    return f"{DATA_DIR}/{_safe(symbol)}_trades.csv"

def load_position(symbol: str) -> dict | None:
    f = get_position_file(symbol)
    if not os.path.exists(f):
        return None
    df = pd.read_csv(f)
    if df.empty:
        os.remove(f)
        return None
    pos = df.iloc[0].to_dict()
    if int(pos.get("Quantity", 0)) <= 0:
        os.remove(f)
        return None
    return pos

def save_position(symbol: str, pos: dict) -> None:
    pd.DataFrame([pos]).to_csv(get_position_file(symbol), index=False)

def clear_position(symbol: str) -> None:
    f = get_position_file(symbol)
    if os.path.exists(f):
        os.remove(f)

def log_trade(symbol: str, trade: dict) -> None:
    f  = get_trade_file(symbol)
    df = pd.DataFrame([trade])
    if os.path.exists(f):
        df = pd.concat([pd.read_csv(f), df], ignore_index=True)
    df.to_csv(f, index=False)

def count_open() -> int:
    return sum(1 for sym, *_ in INSTRUMENTS if load_position(sym) is not None)


# =============================================================================
# Portfolio + run log helpers
# =============================================================================

def load_portfolio_state() -> dict:
    pf = f"{DATA_DIR}/portfolio.csv"
    if os.path.exists(pf):
        df = pd.read_csv(pf)
        if len(df) > 0:
            r = df.iloc[-1]
            return {
                "cash":         float(r["Cash"]),
                "realized_pnl": float(r["Realized PnL"]),
                "total_trades": int(r["Total Trades"]),
            }
    return {"cash": INITIAL_CAPITAL, "realized_pnl": 0.0, "total_trades": 0}


def log_portfolio(cash, equity, open_pos, realized_pnl,
                  unrealized_pnl, total_trades, events) -> None:
    pf  = f"{DATA_DIR}/portfolio.csv"
    row = {
        "Timestamp":      datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
        "Cash":           round(cash, 2),
        "Equity":         round(equity, 2),
        "Open Positions": open_pos,
        "Realized PnL":   round(realized_pnl, 2),
        "Unrealized PnL": round(unrealized_pnl, 2),
        "Total Trades":   total_trades,
        "Events":         " | ".join(events) if events else "NO_ACTION",
    }
    df = pd.read_csv(pf) if os.path.exists(pf) else pd.DataFrame()
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    df.to_csv(pf, index=False)


def log_run(run_data: dict) -> None:
    """
    Write one row to data/run_log.csv after every cron execution.
    This is your audit trail — one row per day showing exactly what happened.
    """
    f  = f"{DATA_DIR}/run_log.csv"
    df = pd.DataFrame([run_data])
    if os.path.exists(f):
        df = pd.concat([pd.read_csv(f), df], ignore_index=True)
    df.to_csv(f, index=False)


def init_portfolio() -> None:
    pf = f"{DATA_DIR}/portfolio.csv"
    if not os.path.exists(pf):
        log_portfolio(INITIAL_CAPITAL, INITIAL_CAPITAL, 0, 0.0, 0.0, 0, ["INIT"])


# =============================================================================
# Historical data fetch — always from Kite Connect
# =============================================================================

_instrument_token_cache = {}   # cache tokens so we don't fetch instruments list 7 times

def _get_token(kite: KiteConnect, tradingsymbol: str, exchange: str) -> int | None:
    cache_key = f"{exchange}:{tradingsymbol}"
    if cache_key in _instrument_token_cache:
        return _instrument_token_cache[cache_key]
    try:
        instruments = kite.instruments(exchange)
        for inst in instruments:
            key = f"{exchange}:{inst['tradingsymbol']}"
            _instrument_token_cache[key] = inst["instrument_token"]
        return _instrument_token_cache.get(cache_key)
    except Exception as e:
        log.warning(f"  [{tradingsymbol}] instruments fetch failed: {e}")
        return None


def fetch_kite_history(kite: KiteConnect,
                       tradingsymbol: str,
                       exchange: str) -> pd.DataFrame:
    """Fetch daily OHLCV candles from Kite Connect."""
    token = _get_token(kite, tradingsymbol, exchange)
    if not token:
        log.warning(f"  [{tradingsymbol}] token not found")
        return pd.DataFrame()

    end_dt   = datetime.now(IST).replace(hour=15, minute=30, second=0, microsecond=0)
    start_dt = end_dt - timedelta(days=HISTORY_DAYS * 2)

    try:
        records = kite.historical_data(
            instrument_token = token,
            from_date        = start_dt,
            to_date          = end_dt,
            interval         = KITE_INTERVAL,
            continuous       = False,
            oi               = False,
        )
    except KiteException as e:
        log.warning(f"  [{tradingsymbol}] history fetch failed: {e}")
        return pd.DataFrame()

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    df = df.rename(columns={
        "date": "Date", "open": "Open", "high": "High",
        "low":  "Low",  "close": "Close", "volume": "Volume",
    })
    df["Date"] = pd.to_datetime(df["Date"])
    if df["Date"].dt.tz is None:
        df["Date"] = df["Date"].dt.tz_localize(IST)
    df = df.set_index("Date").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df = df.tail(HISTORY_DAYS)
    for col in ["Open","High","Low","Close","Volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["Open","High","Low","Close"])
    return df[["Open","High","Low","Close","Volume"]]


def fetch_nifty_history(kite: KiteConnect) -> pd.Series | None:
    """Fetch Nifty 50 daily closes and return EMA(50) series."""
    NIFTY_TOKEN = 256265
    end_dt   = datetime.now(IST).replace(hour=15, minute=30, second=0, microsecond=0)
    start_dt = end_dt - timedelta(days=HISTORY_DAYS * 2)
    try:
        records = kite.historical_data(
            instrument_token = NIFTY_TOKEN,
            from_date        = start_dt,
            to_date          = end_dt,
            interval         = KITE_INTERVAL,
        )
    except Exception as e:
        log.warning(f"  [NIFTY50] history fetch failed: {e}")
        return None
    if not records:
        return None
    df = pd.DataFrame(records)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    closes = pd.to_numeric(df["close"], errors="coerce").dropna()
    return ta.ema(closes, length=NIFTY_EMA_LEN)


# =============================================================================
# Signal computation
# =============================================================================

def compute_supertrend_signal(df: pd.DataFrame,
                               nifty_ema: pd.Series | None,
                               symbol: str) -> dict | None:
    if len(df) < 50:
        return None
    try:
        st = ta.supertrend(df["High"], df["Low"], df["Close"],
                           length=ST_PERIOD, multiplier=ST_MULTIPLIER)
    except Exception as e:
        log.warning(f"  [{symbol}] Supertrend error: {e}")
        return None

    dir_col = [c for c in st.columns if "SUPERTd" in c]
    if not dir_col:
        return None

    df = df.copy()
    df["st_dir"] = st[dir_col[0]].values
    latest   = df.iloc[-1]
    prev     = df.iloc[-2]

    st_bull_now  = float(latest["st_dir"]) == 1.0
    st_bull_prev = float(prev["st_dir"])   == 1.0
    flip_bull    = st_bull_now  and not st_bull_prev
    flip_bear    = not st_bull_now and st_bull_prev

    # Nifty regime
    nifty_ok = True
    if nifty_ema is not None and len(nifty_ema) > 0:
        try:
            idx = nifty_ema.index.get_indexer([df.index[-1]], method="ffill")[0]
            if idx >= 0:
                nifty_ok = True   # EMA computed = Nifty in usable state
        except Exception:
            nifty_ok = True

    return {
        "entry_signal": flip_bull and nifty_ok,
        "exit_signal":  flip_bear,
        "st_direction": "BULL" if st_bull_now else "BEAR",
        "close":        float(latest["Close"]),
        "nifty_ok":     nifty_ok,
        "flip_bull":    flip_bull,
        "flip_bear":    flip_bear,
        "bars":         len(df),
    }


def compute_rsi_momentum_signal(df: pd.DataFrame,
                                 symbol: str) -> dict | None:
    if len(df) < 210:
        return None
    try:
        rsi    = ta.rsi(df["Close"], length=RSI_PERIOD)
        ema200 = ta.ema(df["Close"], length=EMA_200_LEN)
        vol50  = df["Volume"].rolling(VOL_AVG_LEN).mean()
    except Exception as e:
        log.warning(f"  [{symbol}] RSI indicators error: {e}")
        return None

    df = df.copy()
    df["rsi"]    = rsi
    df["ema200"] = ema200
    df["vol50"]  = vol50
    df = df.dropna(subset=["rsi","ema200","vol50"])
    if len(df) < 2:
        return None

    latest = df.iloc[-1]
    prev   = df.iloc[-2]

    rsi_cross_up = (float(latest["rsi"]) >= RSI_ENTRY and
                    float(prev["rsi"])   <  RSI_ENTRY)
    rsi_drop     =  float(latest["rsi"]) <  RSI_EXIT
    vol_ok       = float(latest["Volume"]) > float(latest["vol50"])
    trend_ok     = float(latest["Close"]) > float(latest["ema200"])

    return {
        "entry_signal": rsi_cross_up and vol_ok and trend_ok,
        "exit_signal":  rsi_drop,
        "rsi":          round(float(latest["rsi"]), 1),
        "close":        float(latest["Close"]),
        "ema200":       round(float(latest["ema200"]), 2),
        "vol_ok":       vol_ok,
        "trend_ok":     trend_ok,
        "bars":         len(df),
    }


# =============================================================================
# Telegram
# =============================================================================

def _telegram(msg: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception:
        pass


# =============================================================================
# MAIN
# =============================================================================

init_portfolio()
run_start = datetime.now(IST)

sep()
log.info(f"  INDIA BOT — Mode: {TRADING_MODE.upper()}")
log.info(f"  {run_start.strftime('%Y-%m-%d %H:%M IST')}")
log.info(f"  Candle: DAILY | Strategy: Supertrend(10,3) + RSI Momentum")
log.info(f"  Cron runs once at 15:45 IST — no intraday monitoring")
sep()

# ── Load state ────────────────────────────────────────────────────────────────
state        = load_portfolio_state()
cash         = state["cash"]
realized_pnl = state["realized_pnl"]
total_trades = state["total_trades"]
run_events   = []
trades_run   = 0

log.info(f"  Cash         : Rs {cash:,.2f}")
log.info(f"  Realized PnL : Rs {realized_pnl:+,.2f}")
log.info(f"  Total trades : {total_trades}")
log.info(f"  Open pos     : {count_open()}")

# ── Connect ───────────────────────────────────────────────────────────────────
sep("-")
log.info("  Connecting to Kite Connect...")
kite = get_kite()
try:
    profile = kite.profile()
    log.info(f"  Logged in as : {profile['user_name']} ({profile['user_id']})")
    if TRADING_MODE == "paper":
        log.info("  Paper mode   : data only, NO real orders placed")
except Exception as e:
    log.warning(f"  Profile check failed: {e}")

# ── Nifty regime ──────────────────────────────────────────────────────────────
sep("-")
log.info("  Fetching Nifty 50 for regime filter...")
nifty_ema = fetch_nifty_history(kite)
if nifty_ema is not None and len(nifty_ema) > 0:
    nifty_val = float(nifty_ema.iloc[-1])
    log.info(f"  Nifty EMA({NIFTY_EMA_LEN}) : {nifty_val:,.2f}")
else:
    log.warning("  Nifty unavailable — regime filter disabled for this run")

# ── Fetch candles ─────────────────────────────────────────────────────────────
sep("-")
log.info("  Fetching daily candles from Kite Connect...")
log.info(f"  (1 candle = 1 trading day | fetching last {HISTORY_DAYS} days)")

instruments_data = {}
for sym, exch, _yahoo, strategy, name in INSTRUMENTS:
    df = fetch_kite_history(kite, sym, exch)
    if df.empty or len(df) < 50:
        log.warning(f"  {sym:<14} SKIP — only {len(df)} bars returned")
        continue
    instruments_data[sym] = (df, strategy, name)
    log.info(f"  {sym:<14} {len(df):>4} bars | "
             f"today close = Rs {df['Close'].iloc[-1]:>10,.2f} | "
             f"strategy = {strategy}")

# ── Signals ───────────────────────────────────────────────────────────────────
sep("-")
log.info("  Computing signals on today's closing prices...")
log.info(f"  {'Symbol':<14} {'Close':>10} {'Signal':>8} {'Details'}")
log.info(f"  {'-'*58}")

all_signals = {}
for sym, (df, strategy, name) in instruments_data.items():
    if strategy == "supertrend":
        sig = compute_supertrend_signal(df, nifty_ema, sym)
    else:
        sig = compute_rsi_momentum_signal(df, sym)

    if sig is None:
        log.warning(f"  {sym:<14} signal computation failed")
        continue

    all_signals[sym] = sig

    if sig["entry_signal"]:
        signal_str = "ENTRY  "
    elif sig["exit_signal"]:
        signal_str = "EXIT   "
    else:
        signal_str = "hold   "

    if strategy == "supertrend":
        detail = f"ST={sig['st_direction']} nifty_ok={sig['nifty_ok']}"
    else:
        detail = f"RSI={sig['rsi']} vol_ok={sig['vol_ok']} trend_ok={sig['trend_ok']}"

    log.info(f"  {sym:<14} Rs {sig['close']:>9,.2f} {signal_str}  {detail}")

# =============================================================================
# EXIT PASS
# =============================================================================
sep("-")
log.info("  EXIT PASS — checking open positions...")

any_exit = False
for sym, exch, _yahoo, strategy, name in INSTRUMENTS:
    pos = load_position(sym)
    if pos is None:
        continue

    entry_price = float(pos["Entry Price"])
    quantity    = int(pos["Quantity"])
    entry_date  = pos.get("Entry Date", "?")
    bars_held   = int(pos.get("Bars_Held", 0))

    latest_price = (float(instruments_data[sym][0]["Close"].iloc[-1])
                    if sym in instruments_data else entry_price)

    move_pct    = (latest_price - entry_price) / entry_price
    exit_reason = None

    if sym in all_signals and all_signals[sym]["exit_signal"]:
        exit_reason = ("ST_FLIP_BEAR"
                       if strategy == "supertrend"
                       else "RSI_BELOW_50")

    if exit_reason is None:
        log.info(f"  {sym:<14} HOLDING | "
                 f"entry=Rs {entry_price:,.2f} ({entry_date}) | "
                 f"now=Rs {latest_price:,.2f} | "
                 f"move={move_pct:+.2%} | "
                 f"days_held={bars_held}")
        save_position(sym, {**pos, "Bars_Held": bars_held + 1})
        continue

    # Execute exit
    sell_value   = quantity * latest_price
    commission   = sell_value * (COMMISSION_RT / 2)
    net_proceeds = sell_value - commission
    entry_cost   = quantity * entry_price * (1 + COMMISSION_RT / 2)
    pnl          = net_proceeds - entry_cost
    sign         = "+" if pnl >= 0 else ""

    log.info(f"  {sym:<14} EXIT [{exit_reason}] | "
             f"entry=Rs {entry_price:,.2f} -> now=Rs {latest_price:,.2f} | "
             f"qty={quantity} | move={move_pct:+.2%} | "
             f"PnL=Rs {pnl:+,.2f}")

    realized_pnl += pnl
    total_trades += 1
    trades_run   += 1
    cash         += net_proceeds + entry_cost
    any_exit      = True

    run_events.append(f"{sym} EXIT {exit_reason} PnL={sign}Rs{abs(pnl):.0f}")

    log_trade(sym, {
        "Symbol":      sym,
        "Name":        name,
        "Side":        "SELL",
        "Entry Price": entry_price,
        "Exit Price":  round(latest_price, 2),
        "Quantity":    quantity,
        "PnL":         round(pnl, 2),
        "Return Pct":  round(move_pct * 100, 2),
        "Exit Reason": exit_reason,
        "Entry Date":  entry_date,
        "Exit Date":   datetime.now(IST).strftime("%Y-%m-%d"),
        "Days Held":   bars_held,
        "Mode":        TRADING_MODE,
    })

    _telegram(
        f"<b>India Bot — EXIT</b>\n"
        f"Stock   : {sym} ({name})\n"
        f"Reason  : {exit_reason}\n"
        f"Entry   : Rs {entry_price:,.2f}  ({entry_date})\n"
        f"Exit    : Rs {latest_price:,.2f}\n"
        f"Qty     : {quantity} shares\n"
        f"Move    : {move_pct:+.2%}\n"
        f"PnL     : {sign}Rs {abs(pnl):,.2f}\n"
        f"Mode    : {TRADING_MODE.upper()}"
    )

    if TRADING_MODE == "live" and kite:
        try:
            order_id = kite.place_order(
                variety          = kite.VARIETY_AMO,
                exchange         = EXCHANGE,
                tradingsymbol    = sym,
                transaction_type = kite.TRANSACTION_TYPE_SELL,
                quantity         = quantity,
                product          = PRODUCT_TYPE,
                order_type       = kite.ORDER_TYPE_MARKET,
                tag              = "indiabot_exit",
            )
            log.info(f"    AMO SELL placed: order_id={order_id} "
                     f"(executes at market open ~9:15 AM IST)")
        except KiteException as e:
            log.error(f"    ORDER FAILED: {e}")

    clear_position(sym)

if not any_exit and count_open() == 0:
    log.info("  No open positions to check.")

# =============================================================================
# ENTRY PASS
# =============================================================================
sep("-")
log.info("  ENTRY PASS — checking for new signals...")

open_count   = count_open()
open_equity  = sum(
    int(load_position(sym)["Quantity"]) *
    float(instruments_data[sym][0]["Close"].iloc[-1])
    for sym, *_ in INSTRUMENTS
    if load_position(sym) and sym in instruments_data
)
total_equity = cash + open_equity

entry_signals = [
    (sym, exch, name, strategy, all_signals[sym])
    for sym, exch, _yahoo, strategy, name in INSTRUMENTS
    if sym in all_signals
    and all_signals[sym]["entry_signal"]
    and load_position(sym) is None
]

if not entry_signals:
    log.info("  No entry signals this run.")
else:
    log.info(f"  {len(entry_signals)} entry signal(s) found:")
    for sym, exch, name, strategy, sig in entry_signals:
        log.info(f"    {sym} ({name}) — close=Rs {sig['close']:,.2f}")

    for sym, exch, name, strategy, sig in entry_signals:

        if open_count >= MAX_OPEN_POSITIONS:
            log.info(f"  {sym:<14} BLOCKED — max positions "
                     f"({open_count}/{MAX_OPEN_POSITIONS})")
            continue

        allocation = total_equity * ALLOCATION_PCT
        if cash < allocation or allocation <= 0:
            log.info(f"  {sym:<14} BLOCKED — insufficient cash "
                     f"(need Rs {allocation:,.0f}, have Rs {cash:,.0f})")
            continue

        latest_price = sig["close"]
        quantity     = max(MIN_QUANTITY,
                           int(allocation / (latest_price * (1 + COMMISSION_RT / 2))))

        if quantity <= 0:
            log.info(f"  {sym:<14} BLOCKED — allocation too small for 1 share")
            continue

        actual_cost = quantity * latest_price * (1 + COMMISSION_RT / 2)

        log.info(f"  {sym:<14} ENTERING LONG")
        log.info(f"    Name       : {name}")
        log.info(f"    Strategy   : {strategy}")
        log.info(f"    Price      : Rs {latest_price:,.2f}")
        log.info(f"    Quantity   : {quantity} shares")
        log.info(f"    Cost       : Rs {actual_cost:,.2f}")
        log.info(f"    Allocation : Rs {allocation:,.2f} "
                 f"({ALLOCATION_PCT:.1%} of Rs {total_equity:,.2f} equity)")
        if strategy == "supertrend":
            log.info(f"    Supertrend : {sig['st_direction']} | "
                     f"Nifty OK: {sig['nifty_ok']}")
        else:
            log.info(f"    RSI        : {sig['rsi']} | "
                     f"Vol OK: {sig['vol_ok']} | Trend OK: {sig['trend_ok']}")

        if TRADING_MODE == "live" and kite:
            try:
                order_id = kite.place_order(
                    variety          = kite.VARIETY_AMO,
                    exchange         = EXCHANGE,
                    tradingsymbol    = sym,
                    transaction_type = kite.TRANSACTION_TYPE_BUY,
                    quantity         = quantity,
                    product          = PRODUCT_TYPE,
                    order_type       = kite.ORDER_TYPE_MARKET,
                    tag              = "indiabot_entry",
                )
                log.info(f"    AMO BUY placed: order_id={order_id} "
                         f"(executes at market open ~9:15 AM IST)")
            except KiteException as e:
                log.error(f"    ORDER FAILED: {e}")
                continue

        save_position(sym, {
            "Symbol":      sym,
            "Side":        "LONG",
            "Entry Price": latest_price,
            "Quantity":    quantity,
            "Entry Date":  datetime.now(IST).strftime("%Y-%m-%d"),
            "Bars_Held":   0,
            "Strategy":    strategy,
            "Mode":        TRADING_MODE,
        })

        cash        -= actual_cost
        open_count  += 1
        total_trades += 1
        trades_run   += 1
        run_events.append(f"{sym} ENTRY @ Rs{latest_price:.0f} qty={quantity}")

        _telegram(
            f"<b>India Bot — ENTRY</b>\n"
            f"Stock    : {sym} ({name})\n"
            f"Strategy : {strategy}\n"
            f"Price    : Rs {latest_price:,.2f}\n"
            f"Quantity : {quantity} shares\n"
            f"Cost     : Rs {actual_cost:,.2f}\n"
            f"Mode     : {TRADING_MODE.upper()}"
        )

# =============================================================================
# PORTFOLIO SNAPSHOT
# =============================================================================
open_equity = sum(
    int(load_position(sym)["Quantity"]) *
    float(instruments_data[sym][0]["Close"].iloc[-1])
    for sym, *_ in INSTRUMENTS
    if load_position(sym) and sym in instruments_data
)
unrealized   = sum(
    (float(instruments_data[sym][0]["Close"].iloc[-1]) -
     float(load_position(sym)["Entry Price"])) *
    int(load_position(sym)["Quantity"])
    for sym, *_ in INSTRUMENTS
    if load_position(sym) and sym in instruments_data
)
final_equity = cash + open_equity
open_count   = count_open()

log_portfolio(cash, final_equity, open_count,
              realized_pnl, unrealized, total_trades, run_events)

# ── Run log ───────────────────────────────────────────────────────────────────
run_end     = datetime.now(IST)
run_seconds = (run_end - run_start).total_seconds()

log_run({
    "Date":           run_start.strftime("%Y-%m-%d"),
    "Time":           run_start.strftime("%H:%M:%S IST"),
    "Mode":           TRADING_MODE.upper(),
    "Cash":           round(cash, 2),
    "Open Equity":    round(open_equity, 2),
    "Total Equity":   round(final_equity, 2),
    "Return Pct":     round((final_equity / INITIAL_CAPITAL - 1) * 100, 2),
    "Open Positions": open_count,
    "Realized PnL":   round(realized_pnl, 2),
    "Unrealized PnL": round(unrealized, 2),
    "Trades This Run":trades_run,
    "Total Trades":   total_trades,
    "Events":         " | ".join(run_events) if run_events else "NO_ACTION",
    "Run Seconds":    round(run_seconds, 1),
})

_telegram(
    f"<b>India Bot — Daily Run</b>\n"
    f"Date     : {run_start.strftime('%Y-%m-%d')}\n"
    f"Equity   : Rs {final_equity:,.2f}\n"
    f"Cash     : Rs {cash:,.2f}\n"
    f"Open     : {open_count} positions\n"
    f"Realized : Rs {realized_pnl:+,.2f}\n"
    f"Return   : {(final_equity/INITIAL_CAPITAL-1)*100:+.2f}%\n"
    f"Action   : {' | '.join(run_events) if run_events else 'No action today'}"
)

# ── Final print ───────────────────────────────────────────────────────────────
sep()
log.info("  PORTFOLIO SNAPSHOT")
sep("-")
log.info(f"  Mode           : {TRADING_MODE.upper()}")
log.info(f"  Cash           : Rs {cash:,.2f}")
log.info(f"  Open equity    : Rs {open_equity:,.2f}")
log.info(f"  Total equity   : Rs {final_equity:,.2f}")
log.info(f"  Realized PnL   : Rs {realized_pnl:+,.2f}")
log.info(f"  Unrealized PnL : Rs {unrealized:+,.2f}")
log.info(f"  Return         : {(final_equity/INITIAL_CAPITAL-1)*100:+.2f}%")
log.info(f"  Open positions : {open_count}/{MAX_OPEN_POSITIONS}")
log.info(f"  Trades today   : {trades_run}")
log.info(f"  Total trades   : {total_trades}")
log.info(f"  Run time       : {run_seconds:.1f}s")

if open_count > 0:
    sep("-")
    log.info("  OPEN POSITIONS")
    sep("-")
    for sym, exch, _yahoo, strategy, name in INSTRUMENTS:
        pos = load_position(sym)
        if pos is None:
            continue
        ep   = float(pos["Entry Price"])
        qty  = int(pos["Quantity"])
        bars = int(pos.get("Bars_Held", 0))
        edate= pos.get("Entry Date", "?")
        cp   = (float(instruments_data[sym][0]["Close"].iloc[-1])
                if sym in instruments_data else ep)
        move = (cp - ep) / ep
        pnl  = (cp - ep) * qty
        sign = "+" if pnl >= 0 else ""
        log.info(f"  {sym} ({name})")
        log.info(f"    Entry    : Rs {ep:,.2f}  on {edate}")
        log.info(f"    Current  : Rs {cp:,.2f}")
        log.info(f"    Move     : {move:+.2%}")
        log.info(f"    PnL      : {sign}Rs {abs(pnl):,.2f}")
        log.info(f"    Qty      : {qty} shares")
        log.info(f"    Days held: {bars}")
        log.info(f"    Strategy : {pos.get('Strategy', strategy)}")

sep()
log.info(f"  Run complete. Next run: tomorrow 15:45 IST")
sep()