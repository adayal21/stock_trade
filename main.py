"""
main.py — India Bot (Zerodha Kite Connect)
==========================================
Runs ONCE daily at 15:45 IST via cron, after NSE closes at 15:30.

DATA SOURCE  : Yahoo Finance (free, no API key, goes back 15+ years)
ORDER SOURCE : Kite Connect (Personal plan is fine — orders only, no historical API needed)

CANDLE TIMEFRAME: DAILY (1 candle = 1 full trading day)
The bot looks at today's closing price only. No intraday monitoring.
One run per day. That is it.

HOW SIGNALS WORK:
    Supertrend changes only on a daily close.
    Nothing to check between 15:45 today and 15:45 tomorrow.
    Bot runs, computes signal on today's close, places AMO if needed,
    then does NOTHING until next 15:45 cron.

FILES IN data/ FOLDER:
    bot.log                    - full timestamped log of every run
    run_log.csv                - one row per cron run (audit trail)
    portfolio.csv              - equity curve, one row per day
    {SYMBOL}_position.csv      - open position (deleted when trade exits)
    {SYMBOL}_trades.csv        - complete history of all closed trades

STRATEGY:
    Stocks (BAJFINANCE, TITAN, RELIANCE, MARUTI, LT):
        Entry : Supertrend(10,3) flips bullish AND Nifty > EMA(50)
        Exit  : Supertrend(10,3) flips bearish
        Hold  : Typically 30-90 days

    ETFs (NIFTYBEES, BANKBEES):
        Entry : RSI(14) crosses above 60 AND volume > 50d avg AND close > EMA(200)
        Exit  : RSI(14) drops below 50
        Hold  : Typically 20-60 days

ORDER TYPE: AMO (After Market Order)
    Placed at 15:45 IST. Executes at next morning open ~9:15 AM IST.
    Product: CNC (delivery, held overnight)
"""

import os
import sys
import time
import logging
import warnings
warnings.filterwarnings("ignore")

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
    import yfinance as yf
except ImportError:
    print("ERROR: pip install yfinance")
    sys.exit(1)

try:
    from dotenv import load_dotenv
except ImportError:
    print("ERROR: pip install python-dotenv")
    sys.exit(1)

load_dotenv()

from config import (
    TRADING_MODE, INITIAL_CAPITAL, DATA_DIR, INSTRUMENTS,
    ST_PERIOD, ST_MULTIPLIER, NIFTY_EMA_LEN,
    RSI_PERIOD, RSI_ENTRY, RSI_EXIT, EMA_200_LEN, VOL_AVG_LEN,
    ALLOCATION_PCT, MAX_OPEN_POSITIONS, MIN_QUANTITY,
    PRODUCT_TYPE, ORDER_TYPE, EXCHANGE,
    COMMISSION_RT, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, VERBOSE, IST,
)

os.makedirs(DATA_DIR, exist_ok=True)

# =============================================================================
# Logging — writes to data/bot.log AND console simultaneously
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
# Kite Connect — only used for order placement in live mode
# =============================================================================

def get_kite():
    """Returns a KiteConnect instance. Only called in live mode."""
    try:
        from kiteconnect import KiteConnect
        from kiteconnect.exceptions import KiteException
    except ImportError:
        log.error("kiteconnect not installed: pip install kiteconnect")
        sys.exit(1)

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
# Portfolio + run log
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
# Data fetch — Yahoo Finance (free, no auth needed)
# =============================================================================

def fetch_yahoo(yahoo_ticker: str, period: str = "2y") -> pd.DataFrame:
    """
    Fetch daily OHLCV from Yahoo Finance.
    NSE tickers use .NS suffix e.g. BAJFINANCE.NS, ^NSEI for Nifty 50.
    """
    try:
        raw = yf.download(yahoo_ticker, period=period,
                          progress=False, auto_adjust=True)
    except Exception as e:
        log.warning(f"  [{yahoo_ticker}] Yahoo fetch failed: {e}")
        return pd.DataFrame()

    if raw.empty:
        return pd.DataFrame()

    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)

    needed = ["Open","High","Low","Close","Volume"]
    missing = [c for c in needed if c not in raw.columns]
    if missing:
        return pd.DataFrame()

    df = raw[needed].copy()
    df.index = pd.to_datetime(df.index).normalize()
    df.index.name = "Date"
    df = df.sort_index()
    df = df[~df.index.duplicated(keep="last")]
    for c in needed:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["Open","High","Low","Close"])
    df = df[df["Close"] > 0]
    return df


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

    # Nifty regime check
    nifty_ok = True
    if nifty_ema is not None and len(nifty_ema) > 0:
        nifty_ok = True   # EMA computed = Nifty data available

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
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg,
                  "parse_mode": "HTML"},
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
log.info(f"  Data: Yahoo Finance | Orders: Kite Connect (live only)")
log.info(f"  Candle: DAILY | Cron: once at 15:45 IST")
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

# ── Kite — only connect in live mode ──────────────────────────────────────────
kite = None
if TRADING_MODE == "live":
    sep("-")
    log.info("  Connecting to Kite Connect for order placement...")
    try:
        from kiteconnect.exceptions import KiteException
        kite = get_kite()
        profile = kite.profile()
        log.info(f"  Logged in as : {profile['user_name']} ({profile['user_id']})")
    except Exception as e:
        log.error(f"  Kite login failed: {e}")
        log.error("  Cannot place live orders — exiting.")
        sys.exit(1)
else:
    log.info("  Paper mode — Yahoo Finance for data, no Kite orders.")

# ── Nifty regime via Yahoo Finance ────────────────────────────────────────────
sep("-")
log.info("  Fetching Nifty 50 (Yahoo Finance: ^NSEI)...")
nifty_df  = fetch_yahoo("^NSEI", period="2y")
nifty_ema = None
if not nifty_df.empty:
    nifty_ema = ta.ema(nifty_df["Close"], length=NIFTY_EMA_LEN)
    nifty_latest  = float(nifty_df["Close"].iloc[-1])
    nifty_ema_val = float(nifty_ema.iloc[-1]) if nifty_ema is not None else 0
    nifty_regime  = "UPTREND" if nifty_latest > nifty_ema_val else "DOWNTREND"
    log.info(f"  Nifty close  : {nifty_latest:,.2f}")
    log.info(f"  Nifty EMA({NIFTY_EMA_LEN}): {nifty_ema_val:,.2f}")
    log.info(f"  Nifty regime : {nifty_regime}")
else:
    log.warning("  Nifty data unavailable — regime filter disabled")

# ── Fetch candles via Yahoo Finance ───────────────────────────────────────────
sep("-")
log.info("  Fetching daily candles (Yahoo Finance)...")
log.info(f"  1 candle = 1 trading day | period = 2 years")

instruments_data = {}
for sym, exch, yahoo_ticker, strategy, name in INSTRUMENTS:
    df = fetch_yahoo(yahoo_ticker, period="2y")
    if df.empty or len(df) < 50:
        log.warning(f"  {sym:<14} SKIP — only {len(df)} bars")
        continue
    instruments_data[sym] = (df, strategy, name)
    log.info(f"  {sym:<14} {len(df):>4} bars | "
             f"today close = Rs {df['Close'].iloc[-1]:>10,.2f} | "
             f"strategy = {strategy}")

# ── Signals ───────────────────────────────────────────────────────────────────
sep("-")
log.info("  Computing signals on today's closing prices...")
log.info(f"  {'Symbol':<14} {'Close':>10}  {'Signal':<8}  Details")
log.info(f"  {'-'*60}")

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
        signal_str = "ENTRY   "
    elif sig["exit_signal"]:
        signal_str = "EXIT    "
    else:
        signal_str = "hold    "

    if strategy == "supertrend":
        detail = (f"ST={sig['st_direction']:<4}  "
                  f"flip_bull={sig['flip_bull']}  "
                  f"flip_bear={sig['flip_bear']}  "
                  f"nifty_ok={sig['nifty_ok']}")
    else:
        detail = (f"RSI={sig['rsi']:>5.1f}  "
                  f"vol_ok={sig['vol_ok']}  "
                  f"trend_ok={sig['trend_ok']}")

    log.info(f"  {sym:<14} Rs {sig['close']:>9,.2f}  "
             f"{signal_str}  {detail}")

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
                 f"move={move_pct:+.2%} | days={bars_held}")
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
             f"entry=Rs {entry_price:,.2f} -> "
             f"now=Rs {latest_price:,.2f} | "
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
            log.info(f"    AMO SELL placed: {order_id} "
                     f"(executes at open ~9:15 AM IST)")
        except Exception as e:
            log.error(f"    ORDER FAILED: {e}")

    clear_position(sym)

if not any_exit and count_open() == 0:
    log.info("  No open positions to check.")

# =============================================================================
# ENTRY PASS
# =============================================================================
sep("-")
log.info("  ENTRY PASS — checking for new signals...")

open_count  = count_open()
open_equity = sum(
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
    log.info(f"  {len(entry_signals)} signal(s) found:")
    for sym, exch, name, strategy, sig in entry_signals:
        log.info(f"    {sym} ({name})  close=Rs {sig['close']:,.2f}")

    for sym, exch, name, strategy, sig in entry_signals:

        if open_count >= MAX_OPEN_POSITIONS:
            log.info(f"  {sym:<14} BLOCKED — max positions "
                     f"({open_count}/{MAX_OPEN_POSITIONS})")
            continue

        allocation = total_equity * ALLOCATION_PCT
        if cash < allocation or allocation <= 0:
            log.info(f"  {sym:<14} BLOCKED — insufficient cash "
                     f"(need Rs {allocation:,.0f}, "
                     f"have Rs {cash:,.0f})")
            continue

        latest_price = sig["close"]
        quantity     = max(
            MIN_QUANTITY,
            int(allocation / (latest_price * (1 + COMMISSION_RT / 2)))
        )

        if quantity <= 0:
            log.info(f"  {sym:<14} BLOCKED — "
                     f"allocation too small for 1 share "
                     f"(share=Rs {latest_price:,.0f}, "
                     f"allocation=Rs {allocation:,.0f})")
            continue

        actual_cost = quantity * latest_price * (1 + COMMISSION_RT / 2)

        log.info(f"  {sym:<14} ENTERING LONG")
        log.info(f"    Name       : {name}")
        log.info(f"    Strategy   : {strategy}")
        log.info(f"    Price      : Rs {latest_price:,.2f}")
        log.info(f"    Quantity   : {quantity} shares")
        log.info(f"    Cost       : Rs {actual_cost:,.2f}")
        log.info(f"    Allocation : Rs {allocation:,.2f} "
                 f"({ALLOCATION_PCT:.1%} of Rs {total_equity:,.2f})")
        if strategy == "supertrend":
            log.info(f"    ST flip    : bullish | "
                     f"Nifty OK: {sig['nifty_ok']}")
        else:
            log.info(f"    RSI        : {sig['rsi']} | "
                     f"Vol OK: {sig['vol_ok']} | "
                     f"Trend OK: {sig['trend_ok']}")

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
                log.info(f"    AMO BUY placed: {order_id} "
                         f"(executes at open ~9:15 AM IST)")
            except Exception as e:
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
        run_events.append(
            f"{sym} ENTRY @ Rs{latest_price:.0f} qty={quantity}"
        )

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
unrealized = sum(
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

run_end     = datetime.now(IST)
run_seconds = (run_end - run_start).total_seconds()

log_run({
    "Date":            run_start.strftime("%Y-%m-%d"),
    "Time":            run_start.strftime("%H:%M:%S IST"),
    "Mode":            TRADING_MODE.upper(),
    "Cash":            round(cash, 2),
    "Open Equity":     round(open_equity, 2),
    "Total Equity":    round(final_equity, 2),
    "Return Pct":      round((final_equity / INITIAL_CAPITAL - 1) * 100, 2),
    "Open Positions":  open_count,
    "Realized PnL":    round(realized_pnl, 2),
    "Unrealized PnL":  round(unrealized, 2),
    "Trades This Run": trades_run,
    "Total Trades":    total_trades,
    "Events":          " | ".join(run_events) if run_events else "NO_ACTION",
    "Run Seconds":     round(run_seconds, 1),
})

_telegram(
    f"<b>India Bot — Daily Run</b>\n"
    f"Date     : {run_start.strftime('%Y-%m-%d')}\n"
    f"Equity   : Rs {final_equity:,.2f}\n"
    f"Cash     : Rs {cash:,.2f}\n"
    f"Open     : {open_count} positions\n"
    f"Realized : Rs {realized_pnl:+,.2f}\n"
    f"Return   : {(final_equity/INITIAL_CAPITAL-1)*100:+.2f}%\n"
    f"Action   : "
    f"{'  |  '.join(run_events) if run_events else 'No action today'}"
)

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
        ep    = float(pos["Entry Price"])
        qty   = int(pos["Quantity"])
        bars  = int(pos.get("Bars_Held", 0))
        edate = pos.get("Entry Date", "?")
        cp    = (float(instruments_data[sym][0]["Close"].iloc[-1])
                 if sym in instruments_data else ep)
        move  = (cp - ep) / ep
        pnl   = (cp - ep) * qty
        sign  = "+" if pnl >= 0 else ""
        log.info(f"  {sym} ({name})")
        log.info(f"    Entry    : Rs {ep:,.2f}  on {edate}")
        log.info(f"    Current  : Rs {cp:,.2f}")
        log.info(f"    Move     : {move:+.2%}")
        log.info(f"    PnL      : {sign}Rs {abs(pnl):,.2f}")
        log.info(f"    Qty      : {qty} shares")
        log.info(f"    Days held: {bars}")

sep()
log.info("  Run complete. Next run: tomorrow 15:45 IST")
sep()