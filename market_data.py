"""
Market news and momentum-scanning helpers.

NEWS: pulled from yfinance's per-ticker news feed for the major index ETFs
(SPY, QQQ, DIA) as a practical stand-in for general "market" news — there's
no free, no-API-key source for true market-wide headlines.

MOMENTUM: scans the S&P 500 (~500 stocks, pulled live from a public
constituent list) and ranks the ones moving >= 5% today on >= 2x average
volume. This is the broadest free universe available — a true full-market
scan (every US-listed ticker) needs a paid data feed. Falls back to a
small hardcoded list if the live constituent list can't be fetched.

Both are cached so the page loads fast and we don't hammer Yahoo Finance
on every refresh. The momentum scan in particular (~500 tickers) takes
tens of seconds, so app.py's scheduler refreshes it in the background —
see market_data.refresh_momentum_cache — instead of computing it on the
request thread.
"""

import io
import time

import pandas as pd
import requests
import yfinance as yf

CACHE_TTL_SECONDS = 15 * 60  # 15 minutes
UNIVERSE_CACHE_TTL_SECONDS = 24 * 60 * 60  # S&P 500 membership rarely changes

_NEWS_CACHE = {"data": None, "timestamp": 0}
_MOMENTUM_CACHE = {"data": None, "timestamp": 0}
_UNIVERSE_CACHE = {"data": None, "timestamp": 0}

NEWS_SOURCE_TICKERS = ["SPY", "QQQ", "DIA"]

SP500_WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

# Used only if the live S&P 500 list can't be fetched.
FALLBACK_MOMENTUM_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "NFLX",
    "JPM", "BAC", "GS", "XOM", "CVX", "JNJ", "PFE", "UNH", "WMT", "KO", "PEP",
    "DIS", "NKE", "HD", "COST", "F", "GM", "BA", "CAT", "UBER", "PLTR",
    "SOFI", "COIN", "SHOP", "SQ", "CRM", "ORCL", "INTC", "QCOM", "AVGO", "SMCI",
]


def get_momentum_universe():
    now = time.time()
    if _UNIVERSE_CACHE["data"] is not None and (now - _UNIVERSE_CACHE["timestamp"]) < UNIVERSE_CACHE_TTL_SECONDS:
        return _UNIVERSE_CACHE["data"]

    tickers = _fetch_sp500_tickers() or FALLBACK_MOMENTUM_UNIVERSE

    _UNIVERSE_CACHE["data"] = tickers
    _UNIVERSE_CACHE["timestamp"] = now
    return tickers


def _fetch_sp500_tickers():
    try:
        resp = requests.get(SP500_WIKIPEDIA_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        resp.raise_for_status()
        table = pd.read_html(io.StringIO(resp.text))[0]
        # yfinance uses a dash where Wikipedia uses a dot (e.g. BRK.B -> BRK-B)
        return [str(t).replace(".", "-") for t in table["Symbol"].tolist()]
    except Exception as e:
        print(f"Could not fetch S&P 500 list, falling back to a smaller universe: {e}")
        return None


def get_market_news(limit=10):
    now = time.time()
    if _NEWS_CACHE["data"] is not None and (now - _NEWS_CACHE["timestamp"]) < CACHE_TTL_SECONDS:
        return _NEWS_CACHE["data"]

    all_items = []
    seen_titles = set()

    for ticker in NEWS_SOURCE_TICKERS:
        try:
            raw_items = yf.Ticker(ticker).news or []
        except Exception:
            raw_items = []

        for item in raw_items:
            try:
                # yfinance's news schema has changed across versions — handle
                # both the older flat format and the newer nested "content" format.
                content = item.get("content", item)

                title = content.get("title") or item.get("title")
                if not title or title in seen_titles:
                    continue

                link = None
                if isinstance(content.get("canonicalUrl"), dict):
                    link = content["canonicalUrl"].get("url")
                link = link or item.get("link")

                publisher = ""
                if isinstance(content.get("provider"), dict):
                    publisher = content["provider"].get("displayName", "")
                publisher = publisher or item.get("publisher", "")

                seen_titles.add(title)
                all_items.append({"title": title, "link": link, "publisher": publisher})
            except Exception:
                continue

    result = all_items[:limit]
    _NEWS_CACHE["data"] = result
    _NEWS_CACHE["timestamp"] = now
    return result


def get_momentum_movers(limit=10, min_relative_volume=2.0, min_pct_change=5.0):
    now = time.time()
    if _MOMENTUM_CACHE["data"] is not None and (now - _MOMENTUM_CACHE["timestamp"]) < CACHE_TTL_SECONDS:
        return _MOMENTUM_CACHE["data"]

    return refresh_momentum_cache(limit=limit, min_relative_volume=min_relative_volume, min_pct_change=min_pct_change)


def refresh_momentum_cache(limit=10, min_relative_volume=2.0, min_pct_change=5.0):
    """Does the actual ~500-ticker scan and updates the cache. Split out
    from get_momentum_movers so app.py's scheduler can call this directly
    on a timer, keeping the cache warm without ever making a page request
    wait on the ~40-second scan."""
    universe = get_momentum_universe()
    movers = []

    try:
        data = yf.download(universe, period="2mo", group_by="ticker", threads=True, progress=False)
    except Exception as e:
        print(f"Momentum scan download failed: {e}")
        data = None

    if data is not None and not data.empty:
        is_multi = isinstance(data.columns, pd.MultiIndex)

        for ticker in universe:
            try:
                df = data[ticker] if is_multi else data
                df = df.dropna(subset=["Close", "Volume"])
                if len(df) < 21:
                    continue

                close = df["Close"]
                volume = df["Volume"]

                pct_change = (close.iloc[-1] - close.iloc[-2]) / close.iloc[-2] * 100
                avg_volume_20 = volume.iloc[-21:-1].mean()
                relative_volume = volume.iloc[-1] / avg_volume_20 if avg_volume_20 > 0 else 0

                if relative_volume >= min_relative_volume and abs(pct_change) >= min_pct_change:
                    movers.append({
                        "ticker": ticker,
                        "price": round(float(close.iloc[-1]), 2),
                        "pct_change": round(float(pct_change), 2),
                        "relative_volume": round(float(relative_volume), 2),
                    })
            except Exception:
                continue

    movers.sort(key=lambda m: m["pct_change"], reverse=True)
    result = movers[:limit]

    _MOMENTUM_CACHE["data"] = result
    _MOMENTUM_CACHE["timestamp"] = time.time()
    return result
