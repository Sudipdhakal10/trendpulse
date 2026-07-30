"""
Live price fetching via Alpaca's Market Data API.

Uses the free IEX feed (available on every Alpaca account, paper or live —
no funding or subscription required) to get near-real-time price snapshots.
Note: IEX reflects trades on the IEX exchange specifically, a subset of
total market volume — prices are genuinely live, just not the full
consolidated tape you'd get with a paid SIP subscription. Close enough for
watching movement throughout the day.
"""

import os

import requests

ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_SNAPSHOTS_URL = "https://data.alpaca.markets/v2/stocks/snapshots"


def get_live_snapshots(tickers):
    """Returns {ticker: {"price": float, "pct_change": float}} for the given tickers."""
    if not tickers:
        return {}

    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return {"_error": "Alpaca API keys not set. Add ALPACA_API_KEY and ALPACA_SECRET_KEY."}

    headers = {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }
    params = {"symbols": ",".join(tickers), "feed": "iex"}

    try:
        resp = requests.get(ALPACA_SNAPSHOTS_URL, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"Alpaca fetch failed: {e}")
        return {}

    snapshots = data if isinstance(data, dict) else {}
    results = {}

    for ticker, snap in snapshots.items():
        try:
            if snap is None:
                continue
            latest_trade = snap.get("latestTrade") or {}
            prev_daily_bar = snap.get("prevDailyBar") or {}

            price = latest_trade.get("p")
            prev_close = prev_daily_bar.get("c")

            if price is None or not prev_close:
                continue

            pct_change = (price - prev_close) / prev_close * 100
            results[ticker] = {
                "price": round(price, 2),
                "pct_change": round(pct_change, 2),
            }
        except Exception:
            continue

    return results
