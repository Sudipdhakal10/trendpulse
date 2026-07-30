"""
AutoTrade bot — buys/sells on a user's own ticker list, using whichever
entry and exit strategy they picked per ticker on the AutoTrade page
(separate from their email-alert Watchlist). Originally this ran a single
hardcoded RSI rotation across one fixed ticker list for everyone; that
list still exists per-user as regular rows in autotrade_watchlist, just
no longer hardcoded.

  ENTRY: not currently held, and the ticker's chosen entry strategy
         (one of signals.ALL_STRATEGIES) fires today -> buy up to
         MAX_PER_TICKER dollars, using only currently available cash
  EXIT:  currently held, and the ticker's chosen exit strategy
         (one of signals.ALL_EXIT_STRATEGIES) fires today -> close it
  Exits are processed before entries so freed-up cash can be reused
  the same run. Positions are NOT rebalanced/trimmed to make room for
  new ones — each position just sits until its own exit signal fires.

  Positions the user is holding that aren't in their AutoTrade watchlist
  (e.g. the ticker was since removed) are left alone — we don't know
  which exit rule should apply, so we never touch them automatically.

SAFETY: config.PAPER_TRADING controls whether this trades with real money.
Defaults to true. Do not flip to false until you're fully ready, and even
then, start small.
"""

import yfinance as yf
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus

import config
import db
import signals

MAX_PER_TICKER = 10000
MIN_TRADE_DOLLARS = 100


def get_trading_client(api_key, secret_key):
    if not api_key or not secret_key:
        return None
    return TradingClient(api_key, secret_key, paper=config.PAPER_TRADING)


def get_latest_price(ticker):
    data = yf.download(ticker, period="5d", progress=False)
    if data.columns.nlevels > 1:
        data.columns = data.columns.get_level_values(0)
    return float(data["Close"].iloc[-1])


def get_current_positions(client):
    """Returns {ticker: {"qty": float, "avg_entry_price": float}}."""
    positions = client.get_all_positions()
    return {
        p.symbol: {"qty": float(p.qty), "avg_entry_price": float(p.avg_entry_price)}
        for p in positions
    }


def get_open_orders(client):
    """Orders that have been accepted by Alpaca but haven't filled yet —
    most commonly a market order placed while the exchange is closed,
    which sits queued until the next session opens. These don't show up
    in get_all_positions() since nothing has actually been bought/sold
    yet, which is exactly why a user can place a trade and see nothing
    change on the Current Positions table until it fills."""
    request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
    orders = client.get_orders(request)
    return [
        {
            "ticker": o.symbol,
            "side": o.side.value,
            "qty": float(o.qty) if o.qty is not None else None,
            "status": o.status.value,
            "submitted_at": o.submitted_at.isoformat() if o.submitted_at else None,
        }
        for o in orders
    ]


def submit_order(user_id, client, ticker, qty, side, reason=""):
    if qty <= 0:
        return
    order = MarketOrderRequest(
        symbol=ticker,
        qty=round(qty, 4),
        side=side,
        time_in_force=TimeInForce.DAY,
    )
    client.submit_order(order)
    db.record_trade(user_id, ticker, side.value, round(qty, 4), reason)


def run_rotation_bot(user_id, api_key, secret_key):
    """Runs one full check-and-trade cycle for a single user's Alpaca
    account, against their own AutoTrade ticker list. Returns a summary
    dict for the UI."""
    client = get_trading_client(api_key, secret_key)
    if client is None:
        return {"error": "Alpaca API keys not set.", "log": []}

    watchlist = db.get_autotrade_watchlist(user_id)
    if not watchlist:
        return {"error": "No tickers in your AutoTrade watchlist yet. Add some below.", "log": []}

    log_lines = []

    try:
        account = client.get_account()
    except APIError as e:
        return {"error": f"Alpaca rejected these API keys: {e}", "log": []}

    portfolio_value = float(account.portfolio_value)
    log_lines.append(f"Portfolio value: ${portfolio_value:,.2f}")

    current_positions = get_current_positions(client)
    log_lines.append(f"Currently held: {list(current_positions.keys()) or 'nothing'}")

    exits, entries = [], []
    entry_strategy_by_ticker = {row["ticker"]: row["entry_strategy"] for row in watchlist}
    exit_strategy_by_ticker = {row["ticker"]: row["exit_strategy"] for row in watchlist}

    for row in watchlist:
        t = row["ticker"]
        held = t in current_positions

        if held:
            entry_price = current_positions[t]["avg_entry_price"]
            fired = signals.evaluate_exit(t, row["exit_strategy"], entry_price=entry_price)
            log_lines.append(f"{t}: HELD — exit strategy {row['exit_strategy']} {'FIRED' if fired else 'not yet'}")
            if fired:
                exits.append(t)
        else:
            result = signals.evaluate_latest(t)
            if result is None:
                log_lines.append(f"{t}: could not fetch data, skipping")
                continue
            fired = bool(result["signals"].get(row["entry_strategy"]))
            log_lines.append(f"{t}: not held — entry strategy {row['entry_strategy']} {'FIRED' if fired else 'not yet'}")
            if fired:
                entries.append(t)

    if exits:
        for t in exits:
            client.close_position(t)
            db.record_trade(
                user_id, t, "sell", current_positions[t]["qty"],
                f"Exit: {exit_strategy_by_ticker[t]}",
            )
            log_lines.append(f"Closed {t}")
    else:
        log_lines.append("No exits today.")

    if entries:
        account = client.get_account()
        available_cash = float(account.cash)
        log_lines.append(f"Available cash: ${available_cash:,.2f}")

        for t in entries:
            if available_cash < MIN_TRADE_DOLLARS:
                log_lines.append(f"{t}: not enough cash left, skipping")
                continue

            allocation = min(MAX_PER_TICKER, available_cash)
            price = get_latest_price(t)
            qty = allocation / price

            submit_order(user_id, client, t, qty, OrderSide.BUY, reason=f"Entry: {entry_strategy_by_ticker[t]}")
            log_lines.append(f"Bought {qty:.4f} shares of {t} (${allocation:,.2f})")

            available_cash -= allocation
    else:
        log_lines.append("No entries today.")

    return {"log": log_lines, "exits": exits, "entries": entries}


def get_account_summary(api_key, secret_key):
    client = get_trading_client(api_key, secret_key)
    if client is None:
        return {"error": "Alpaca API keys not set."}

    try:
        account = client.get_account()
        positions = client.get_all_positions()
        pending_orders = get_open_orders(client)
    except APIError as e:
        return {"error": f"Alpaca rejected these API keys: {e}"}

    return {
        "portfolio_value": float(account.portfolio_value),
        "cash": float(account.cash),
        "paper_trading": config.PAPER_TRADING,
        "positions": [
            {
                "ticker": p.symbol,
                "qty": float(p.qty),
                "current_price": float(p.current_price),
                "market_value": float(p.market_value),
                "unrealized_pl": float(p.unrealized_pl),
            }
            for p in positions
        ],
        "pending_orders": pending_orders,
    }
