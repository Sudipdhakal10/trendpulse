"""
AutoTrade bot — buys/sells on a user's own ticker list, using whichever
entry and exit strategy they picked per ticker on the AutoTrade page
(separate from their email-alert Watchlist). Originally this ran a single
hardcoded RSI rotation across one fixed ticker list for everyone; that
list still exists per-user as regular rows in autotrade_watchlist, just
no longer hardcoded.

  ENTRY: not currently held, and the ticker's chosen entry strategy
         (one of signals.ALL_STRATEGIES) fires today -> buy, using only
         currently available cash. Sized per the ticker's own
         allocation_mode/allocation_value if the user set one (a fixed
         dollar amount or a fixed share quantity), else MAX_PER_TICKER
         dollars by default.
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

from collections import deque

import yfinance as yf
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest, GetPortfolioHistoryRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus

import config
import db
import signals

# Range button -> (Alpaca portfolio-history period+timeframe, matching
# yfinance period for the SPY benchmark comparison). Alpaca's period format
# is "<number><unit>" (D/W/M/A) or the literal "all"; yfinance has no "all"
# equivalent so "max" is the closest match there.
PERFORMANCE_RANGES = {
    "1D": {"alpaca_period": "1D", "alpaca_timeframe": "5Min", "yf_period": "1d"},
    "1W": {"alpaca_period": "1W", "alpaca_timeframe": "1H", "yf_period": "5d"},
    "1M": {"alpaca_period": "1M", "alpaca_timeframe": "1D", "yf_period": "1mo"},
    "3M": {"alpaca_period": "3M", "alpaca_timeframe": "1D", "yf_period": "3mo"},
    "1Y": {"alpaca_period": "1A", "alpaca_timeframe": "1D", "yf_period": "1y"},
    "All": {"alpaca_period": "all", "alpaca_timeframe": "1D", "yf_period": "max"},
}

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

    # Orders can sit "accepted" but unfilled for a while (e.g. a market
    # order placed while the exchange is closed) -- they don't show up in
    # get_all_positions() yet, so without this a repeat run would see
    # "not held" and buy/sell the same ticker again on top of the order
    # that's already working.
    pending_tickers = {o["ticker"] for o in get_open_orders(client)}

    exits, entries = [], []
    entry_strategy_by_ticker = {row["ticker"]: row["entry_strategy"] for row in watchlist}
    exit_strategy_by_ticker = {row["ticker"]: row["exit_strategy"] for row in watchlist}
    allocation_mode_by_ticker = {row["ticker"]: row["allocation_mode"] for row in watchlist}
    allocation_value_by_ticker = {row["ticker"]: row["allocation_value"] for row in watchlist}

    for row in watchlist:
        t = row["ticker"]
        held = t in current_positions

        if t in pending_tickers:
            log_lines.append(f"{t}: order already pending, skipping until it fills")
            continue

        try:
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
        except Exception as e:
            # yfinance is known to be flaky on cloud IPs (see signals.py/
            # market_data.py) -- one bad ticker must not stop every other
            # ticker in this user's list from being evaluated.
            log_lines.append(f"{t}: evaluation failed, skipping ({e})")

    if exits:
        for t in exits:
            try:
                client.close_position(t)
                db.record_trade(
                    user_id, t, "sell", current_positions[t]["qty"],
                    f"Exit: {exit_strategy_by_ticker[t]}",
                )
                log_lines.append(f"Closed {t}")
            except Exception as e:
                # One rejected/failed close must not stop other tickers'
                # exits (or the entries below) from still being attempted.
                log_lines.append(f"{t}: failed to close position ({e})")
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

            mode = allocation_mode_by_ticker.get(t)
            value = allocation_value_by_ticker.get(t)

            try:
                price = get_latest_price(t)

                if mode == "shares":
                    qty = value
                    allocation = qty * price
                    if allocation > available_cash:
                        log_lines.append(
                            f"{t}: {qty:g} shares would cost ${allocation:,.2f}, more than the "
                            f"${available_cash:,.2f} available -- skipping"
                        )
                        continue
                elif mode == "dollars":
                    allocation = min(value, available_cash)
                    qty = allocation / price
                else:
                    allocation = min(MAX_PER_TICKER, available_cash)
                    qty = allocation / price

                submit_order(user_id, client, t, qty, OrderSide.BUY, reason=f"Entry: {entry_strategy_by_ticker[t]}")
            except Exception as e:
                # One failed buy (bad price data, an Alpaca rejection, etc.)
                # must not stop the remaining entries from being attempted,
                # and must not deduct this allocation from available_cash
                # since nothing was actually bought.
                log_lines.append(f"{t}: failed to buy ({e})")
                continue

            log_lines.append(f"Bought {qty:.4f} shares of {t} (${allocation:,.2f})")
            available_cash -= allocation
    else:
        log_lines.append("No entries today.")

    return {"log": log_lines, "exits": exits, "entries": entries}


def place_manual_order(user_id, api_key, secret_key, ticker, side, mode, value):
    """Places a single manual market order for any ticker -- the Screener
    page's Buy/Sell, independent of the AutoTrade watchlist/strategy loop.
    mode is "dollars" or "shares", value is the corresponding amount.
    Returns a result dict for the UI; never raises."""
    client = get_trading_client(api_key, secret_key)
    if client is None:
        return {"error": "Alpaca API keys not set. Add them on the AutoTrade page."}

    ticker = ticker.strip().upper()

    try:
        account = client.get_account()
    except APIError as e:
        return {"error": f"Alpaca rejected these API keys: {e}"}

    try:
        price = get_latest_price(ticker)
    except Exception as e:
        return {"error": f"Could not fetch a price for {ticker}: {e}"}

    if side == "buy":
        available_cash = float(account.cash)
        if mode == "shares":
            qty = value
            cost = qty * price
            if cost > available_cash:
                return {"error": f"{qty:g} shares of {ticker} would cost ${cost:,.2f}, more than the ${available_cash:,.2f} available."}
        else:
            qty = value / price
        order_side = OrderSide.BUY
    else:
        try:
            positions = get_current_positions(client)
        except APIError as e:
            return {"error": f"Could not check current positions: {e}"}
        held = positions.get(ticker)
        if not held:
            return {"error": f"You don't currently hold any {ticker}."}
        max_qty = held["qty"]
        if mode == "shares":
            qty = value
            if qty > max_qty:
                return {"error": f"You only hold {max_qty:g} shares of {ticker}."}
        else:
            qty = value / price
            if qty > max_qty:
                return {"error": f"That's more than your {max_qty:g}-share position in {ticker} (worth ~${max_qty * price:,.2f})."}
        order_side = OrderSide.SELL

    if qty <= 0:
        return {"error": "That amount rounds to zero shares -- try a larger amount."}

    try:
        submit_order(user_id, client, ticker, qty, order_side, reason=f"Manual {side}")
    except Exception as e:
        return {"error": f"Order failed: {e}"}

    return {"status": "ok", "ticker": ticker, "side": side, "qty": round(qty, 4), "price": price}


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


def get_portfolio_history(api_key, secret_key, range_key):
    """Account equity over time, straight from Alpaca's own tracking --
    far more reliable than trying to reconstruct historical portfolio
    value ourselves from ticker prices at arbitrary past timestamps."""
    client = get_trading_client(api_key, secret_key)
    if client is None:
        return {"error": "Alpaca API keys not set."}

    params = PERFORMANCE_RANGES.get(range_key, PERFORMANCE_RANGES["1M"])

    try:
        history = client.get_portfolio_history(GetPortfolioHistoryRequest(
            period=params["alpaca_period"],
            timeframe=params["alpaca_timeframe"],
        ))
    except APIError as e:
        return {"error": f"Alpaca rejected these API keys: {e}"}
    except Exception as e:
        return {"error": f"Could not load portfolio history: {e}"}

    points = [
        {"t": ts, "equity": eq}
        for ts, eq in zip(history.timestamp, history.equity)
        if eq is not None
    ]

    # Alpaca pads the response to cover the full requested period even if
    # the account didn't exist yet for part of it (e.g. a 1-month range
    # requested for an account opened 10 days ago), returning equity=0
    # placeholders for those pre-account days rather than omitting them.
    # Left in place, that zero becomes the chart's "start" value -- a flat
    # line at the bottom followed by a misleading vertical jump, and a
    # period return of $current_value / 0%, since there's no real starting
    # balance to compare against. Trim that leading run so the series
    # starts at the account's actual first real balance.
    first_real = next((i for i, p in enumerate(points) if p["equity"] != 0), None)
    points = points[first_real:] if first_real is not None else []

    return {"points": points, "base_value": history.base_value}


def get_benchmark_return(range_key):
    """SPY's % change over the same range, for a 'vs S&P 500' comparison
    stat next to the portfolio's own return."""
    params = PERFORMANCE_RANGES.get(range_key, PERFORMANCE_RANGES["1M"])
    try:
        hist = yf.Ticker("SPY").history(period=params["yf_period"])
        closes = hist["Close"].dropna()
        if len(closes) < 2:
            return None
        return round(float((closes.iloc[-1] - closes.iloc[0]) / closes.iloc[0] * 100), 2)
    except Exception as e:
        print(f"Benchmark return fetch failed: {e}")
        return None


def _compute_realized_pnl(filled_orders):
    """FIFO lot-matching: each sell consumes the oldest still-open buy
    lots for that ticker first. filled_orders must already be sorted
    chronologically. Returns one realized P&L amount per sell that
    matched at least some open quantity -- a sell with no prior buy in
    this order history (e.g. a position that predates it) is skipped
    for that leftover portion since there's no cost basis to compare against."""
    open_lots = {}  # ticker -> deque of [qty, price]
    realized = []

    for o in filled_orders:
        lots = open_lots.setdefault(o["ticker"], deque())
        if o["side"] == "buy":
            lots.append([o["qty"], o["price"]])
            continue

        remaining = o["qty"]
        pnl = 0.0
        matched_qty = 0.0
        while remaining > 1e-9 and lots:
            lot_qty, lot_price = lots[0]
            take = min(lot_qty, remaining)
            pnl += (o["price"] - lot_price) * take
            matched_qty += take
            remaining -= take
            if take >= lot_qty - 1e-9:
                lots.popleft()
            else:
                lots[0][0] = lot_qty - take
        if matched_qty > 0:
            realized.append(pnl)

    return realized


def get_trade_performance(api_key, secret_key):
    """Realized P&L and win rate from Alpaca's own filled-order history
    (not trade_log, which never stored fill prices) -- FIFO-matches
    every filled sell against the oldest still-open filled buys for
    that ticker."""
    client = get_trading_client(api_key, secret_key)
    if client is None:
        return {"error": "Alpaca API keys not set."}

    try:
        orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=500))
    except APIError as e:
        return {"error": f"Alpaca rejected these API keys: {e}"}

    filled = []
    for o in orders:
        if not o.filled_at or not o.filled_qty or not o.filled_avg_price:
            continue
        filled.append({
            "ticker": o.symbol,
            "side": o.side.value,
            "qty": float(o.filled_qty),
            "price": float(o.filled_avg_price),
            "filled_at": o.filled_at,
        })
    filled.sort(key=lambda o: o["filled_at"])

    realized = _compute_realized_pnl(filled)
    wins = sum(1 for p in realized if p > 0)
    losses = sum(1 for p in realized if p < 0)
    decided = wins + losses

    return {
        "realized_pl": round(sum(realized), 2),
        "closed_trades": len(realized),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / decided * 100, 1) if decided > 0 else None,
    }
