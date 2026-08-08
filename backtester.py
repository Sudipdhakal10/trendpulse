"""
Backtester — simulates a single ticker's entry+exit strategy pairing
against its own historical price data, day by day, so a user can see how
a pairing would have performed before ever running it live via AutoTrade.

Reuses signals.py's exact indicator/signal logic (same MA50/RSI/MACD/
20D_HIGH columns, same get_signal_series/get_exit_signal_series boolean
series) so a strategy behaves identically here and in AutoTrade -- no
separate reimplementation to drift out of sync.
"""

import signals

MIN_HISTORY_ROWS = 210  # matches signals.py's own indicator warm-up requirement
WARMUP_ROWS = 200  # MA200 needs 200 rows before it's valid; simulation starts after that

BACKTEST_PERIODS = ["1y", "2y", "5y", "10y", "max"]


def run_backtest(ticker, entry_strategy, exit_strategy, period="2y", starting_capital=10000):
    if entry_strategy not in signals.ALL_STRATEGIES:
        return {"error": f"Unknown entry strategy: {entry_strategy}"}
    if exit_strategy not in signals.ALL_EXIT_STRATEGIES:
        return {"error": f"Unknown exit strategy: {exit_strategy}"}
    if starting_capital <= 0:
        return {"error": "Starting capital must be greater than zero."}

    df = signals.fetch_history(ticker, period=period)
    if df.empty or len(df) < MIN_HISTORY_ROWS:
        return {"error": f"Not enough price history for {ticker} to backtest."}

    df = signals.compute_indicators(df)
    entry_series = signals.get_signal_series(df)[entry_strategy]
    exit_series = None
    if exit_strategy != "Take_Profit_Stop_Loss":
        exit_series = signals.get_exit_signal_series(df)[exit_strategy]

    return _simulate(df, entry_series, exit_series, ticker, entry_strategy, exit_strategy, starting_capital)


def _simulate(df, entry_series, exit_series, ticker, entry_strategy, exit_strategy, starting_capital):
    close = df["Close"]
    dates = df.index
    start_idx = min(WARMUP_ROWS, len(df) - 1)

    cash = starting_capital
    shares = 0.0
    entry_price = None
    equity_curve = []
    trades = []

    for i in range(start_idx, len(df)):
        price = float(close.iloc[i])
        date_str = dates[i].strftime("%Y-%m-%d")

        if shares > 0:
            if exit_strategy == "Take_Profit_Stop_Loss":
                change_pct = (price - entry_price) / entry_price * 100
                fired = change_pct >= signals.TAKE_PROFIT_PCT or change_pct <= -signals.STOP_LOSS_PCT
            else:
                fired = bool(exit_series.iloc[i])

            if fired:
                proceeds = shares * price
                pnl = proceeds - (shares * entry_price)
                cash += proceeds
                trades.append({
                    "date": date_str, "side": "sell", "price": round(price, 2),
                    "shares": round(shares, 4), "pnl": round(pnl, 2),
                })
                shares = 0.0
                entry_price = None
        elif bool(entry_series.iloc[i]):
            shares = cash / price
            entry_price = price
            trades.append({
                "date": date_str, "side": "buy", "price": round(price, 2),
                "shares": round(shares, 4), "pnl": None,
            })
            cash = 0.0

        equity_curve.append({"date": date_str, "equity": round(cash + shares * price, 2)})

    final_equity = equity_curve[-1]["equity"] if equity_curve else starting_capital
    total_return_pct = (final_equity - starting_capital) / starting_capital * 100

    buy_hold_start = float(close.iloc[start_idx])
    buy_hold_end = float(close.iloc[-1])
    buy_hold_return_pct = (buy_hold_end - buy_hold_start) / buy_hold_start * 100

    sells = [t for t in trades if t["side"] == "sell"]
    wins = sum(1 for t in sells if t["pnl"] > 0)
    losses = sum(1 for t in sells if t["pnl"] <= 0)

    peak = starting_capital
    max_drawdown_pct = 0.0
    for p in equity_curve:
        peak = max(peak, p["equity"])
        max_drawdown_pct = min(max_drawdown_pct, (p["equity"] - peak) / peak * 100)

    return {
        "ticker": ticker,
        "entry_strategy": entry_strategy,
        "exit_strategy": exit_strategy,
        "starting_capital": starting_capital,
        "final_equity": round(final_equity, 2),
        "total_return_pct": round(total_return_pct, 2),
        "buy_hold_return_pct": round(buy_hold_return_pct, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "trade_count": len(sells),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / len(sells) * 100, 1) if sells else None,
        "still_holding": shares > 0,
        "equity_curve": equity_curve,
        "trades": list(reversed(trades)),
    }
