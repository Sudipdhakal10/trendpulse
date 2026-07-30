"""
Signal definitions — the same strategies from the backtester, adapted here
to evaluate whether each one is triggering RIGHT NOW (on the most recent
trading day) rather than scanning historical data for backtesting.
"""

import pandas as pd
import yfinance as yf

ALL_STRATEGIES = [
    "MA50_Cross",
    "RSI_Oversold",
    "MACD_Bullish_Cross",
    "Breakout_20D",
    "Pullback_to_MA50",
    "Combo_Trend",
    "Combo_MeanReversion",
]


def fetch_history(ticker, period="1y"):
    df = yf.Ticker(ticker).history(period=period)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def compute_indicators(df):
    close = df["Close"]
    df["MA50"] = close.rolling(50).mean()
    df["MA200"] = close.rolling(200).mean()

    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss
    df["RSI"] = 100 - (100 / (1 + rs))

    ema_fast = close.ewm(span=12, adjust=False).mean()
    ema_slow = close.ewm(span=26, adjust=False).mean()
    df["MACD"] = ema_fast - ema_slow
    df["MACD_SIGNAL"] = df["MACD"].ewm(span=9, adjust=False).mean()

    df["20D_HIGH"] = close.shift(1).rolling(20).max()

    return df


def get_signal_series(df):
    """Returns a dict of strategy_name -> full boolean Series (same logic as backtester)."""
    close = df["Close"]
    signals = {}

    signals["MA50_Cross"] = (close.shift(1) < df["MA50"].shift(1)) & (close > df["MA50"])
    signals["RSI_Oversold"] = df["RSI"] < 30
    signals["MACD_Bullish_Cross"] = (
        (df["MACD"].shift(1) < df["MACD_SIGNAL"].shift(1)) & (df["MACD"] > df["MACD_SIGNAL"])
    )
    signals["Breakout_20D"] = close > df["20D_HIGH"]
    signals["Pullback_to_MA50"] = (
        (close > df["MA200"]) & ((close - df["MA50"]).abs() / df["MA50"] * 100 <= 2)
    )
    signals["Combo_Trend"] = signals["MACD_Bullish_Cross"] & signals["Breakout_20D"]
    signals["Combo_MeanReversion"] = signals["RSI_Oversold"] & (close > df["MA200"])

    return signals


def evaluate_latest(ticker):
    """Fetches data for `ticker` and returns which strategies are triggering
    on the most recent trading day, plus the latest price for context."""
    df = fetch_history(ticker)
    if df.empty or len(df) < 210:
        return None

    df = compute_indicators(df)
    signal_series = get_signal_series(df)

    latest = {name: bool(series.iloc[-1]) for name, series in signal_series.items()}
    latest_price = float(df["Close"].iloc[-1])
    latest_date = df.index[-1].strftime("%Y-%m-%d")

    return {"price": latest_price, "date": latest_date, "signals": latest}


# ============ Exit strategies (for AutoTrade) ============
#
# The strategies above are entry-only — none of them describe a sell
# condition. These are a small curated set of exit rules AutoTrade users
# pick from independently of whichever entry strategy got them into a
# position; three are pure technical reversals and one is a plain
# percentage target that needs the position's entry price.

EXIT_RSI_OVERBOUGHT = 75
TAKE_PROFIT_PCT = 15
STOP_LOSS_PCT = 8

ALL_EXIT_STRATEGIES = [
    "RSI_Overbought",
    "MA50_Breakdown",
    "MACD_Bearish_Cross",
    "Take_Profit_Stop_Loss",
]


def get_exit_signal_series(df):
    """Returns a dict of exit_strategy_name -> full boolean Series, mirroring
    get_signal_series but for the exit side. Take_Profit_Stop_Loss isn't
    included here since it depends on a position's entry price, not just
    the price history."""
    close = df["Close"]
    signals = {}

    signals["RSI_Overbought"] = df["RSI"] > EXIT_RSI_OVERBOUGHT
    signals["MA50_Breakdown"] = (close.shift(1) > df["MA50"].shift(1)) & (close < df["MA50"])
    signals["MACD_Bearish_Cross"] = (
        (df["MACD"].shift(1) > df["MACD_SIGNAL"].shift(1)) & (df["MACD"] < df["MACD_SIGNAL"])
    )

    return signals


def evaluate_exit(ticker, exit_strategy, entry_price=None):
    """Returns True if `exit_strategy` fires today for a position already
    held in `ticker`. entry_price is required for Take_Profit_Stop_Loss and
    ignored by the others."""
    if exit_strategy == "Take_Profit_Stop_Loss":
        if not entry_price:
            return False
        df = fetch_history(ticker, period="5d")
        if df.empty:
            return False
        latest_price = float(df["Close"].iloc[-1])
        change_pct = (latest_price - entry_price) / entry_price * 100
        return change_pct >= TAKE_PROFIT_PCT or change_pct <= -STOP_LOSS_PCT

    df = fetch_history(ticker)
    if df.empty or len(df) < 210:
        return False
    df = compute_indicators(df)

    series = get_exit_signal_series(df).get(exit_strategy)
    return bool(series.iloc[-1]) if series is not None else False
