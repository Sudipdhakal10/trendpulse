"""
Data for the Shop page: search any publicly traded symbol (not just
watchlist tickers) and pull its chart, fundamentals, earnings history,
quarterly financials, and news. All from yfinance (no API key needed),
cached per-key so browsing around the page doesn't hammer Yahoo Finance
on every click.
"""

import time

import yfinance as yf

import market_data

SEARCH_CACHE_TTL_SECONDS = 5 * 60
SNAPSHOT_CACHE_TTL_SECONDS = 5 * 60
CHART_CACHE_TTL_SECONDS = 5 * 60
INTRADAY_CHART_CACHE_TTL_SECONDS = 60  # 1D range: short TTL so the Shop page's live-updating chart actually sees new bars
NEWS_CACHE_TTL_SECONDS = 15 * 60
EARNINGS_CACHE_TTL_SECONDS = 60 * 60
FINANCIALS_CACHE_TTL_SECONDS = 24 * 60 * 60

_search_cache = {}
_snapshot_cache = {}
_chart_cache = {}
_news_cache = {}
_earnings_cache = {}
_financials_cache = {}

# yfinance period/interval pairs per chart range button.
CHART_RANGES = {
    "1D": {"period": "1d", "interval": "5m"},
    "1W": {"period": "5d", "interval": "30m"},
    "1M": {"period": "1mo", "interval": "1d"},
    "3M": {"period": "3mo", "interval": "1d"},
    "6M": {"period": "6mo", "interval": "1d"},
    "1Y": {"period": "1y", "interval": "1wk"},
    "5Y": {"period": "5y", "interval": "1wk"},
}


def _cached(cache, key, ttl, fetch_fn):
    now = time.time()
    entry = cache.get(key)
    if entry is not None and (now - entry["timestamp"]) < ttl:
        return entry["data"]
    data = fetch_fn()
    cache[key] = {"data": data, "timestamp": now}
    return data


def _num(value):
    """Coerces a possibly-missing/NaN yfinance value to a plain float,
    or None. (v != v is true only for NaN -- avoids importing pandas
    just for isna().)"""
    try:
        if value is None or value != value:
            return None
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def search_symbols(query, limit=8):
    query = (query or "").strip()
    if not query:
        return []

    def fetch():
        try:
            results = yf.Search(query, max_results=limit).quotes
        except Exception as e:
            print(f"Stock search failed for {query!r}: {e}")
            return []

        out = []
        for r in results:
            symbol = r.get("symbol")
            quote_type = r.get("quoteType")
            if not symbol or quote_type not in ("EQUITY", "ETF"):
                continue
            out.append({
                "symbol": symbol,
                "name": r.get("shortname") or r.get("longname") or symbol,
                "exchange": r.get("exchDisp") or r.get("exchange") or "",
            })
        return out

    return _cached(_search_cache, query.lower(), SEARCH_CACHE_TTL_SECONDS, fetch)


def get_stock_snapshot(ticker):
    ticker = ticker.strip().upper()

    def fetch():
        try:
            info = yf.Ticker(ticker).info or {}
        except Exception as e:
            print(f"Snapshot fetch failed for {ticker}: {e}")
            return {"_error": f"Could not load data for {ticker}."}

        if not info.get("symbol") and not info.get("shortName") and not info.get("longName"):
            return {"_error": f"No data found for {ticker}."}

        price = info.get("currentPrice") or info.get("regularMarketPrice")
        prev_close = info.get("regularMarketPreviousClose")
        change_pct = info.get("regularMarketChangePercent")
        if change_pct is None and price and prev_close:
            change_pct = (price - prev_close) / prev_close * 100

        return {
            "ticker": ticker,
            "name": info.get("longName") or info.get("shortName") or ticker,
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "price": _num(price),
            "change_pct": _num(change_pct),
            "market_cap": _num(info.get("marketCap")),
            "pe_ratio": _num(info.get("trailingPE")),
            "forward_pe": _num(info.get("forwardPE")),
            "eps": _num(info.get("trailingEps")),
            "forward_eps": _num(info.get("forwardEps")),
            "week52_high": _num(info.get("fiftyTwoWeekHigh")),
            "week52_low": _num(info.get("fiftyTwoWeekLow")),
            "volume": _num(info.get("volume") or info.get("regularMarketVolume")),
            "avg_volume": _num(info.get("averageVolume")),
            "dividend_yield": _num(info.get("dividendYield")),
        }

    return _cached(_snapshot_cache, ticker, SNAPSHOT_CACHE_TTL_SECONDS, fetch)


def get_stock_chart(ticker, range_key):
    ticker = ticker.strip().upper()
    range_key = (range_key or "1M").upper()
    params = CHART_RANGES.get(range_key, CHART_RANGES["1M"])

    def fetch():
        try:
            hist = yf.Ticker(ticker).history(period=params["period"], interval=params["interval"])
        except Exception as e:
            print(f"Chart fetch failed for {ticker} ({range_key}): {e}")
            return []

        if hist is None or hist.empty:
            return []

        points = []
        for ts, row in hist.iterrows():
            close = _num(row.get("Close"))
            if close is None:
                continue
            points.append({"t": ts.isoformat(), "c": close})
        return points

    ttl = INTRADAY_CHART_CACHE_TTL_SECONDS if range_key == "1D" else CHART_CACHE_TTL_SECONDS
    return _cached(_chart_cache, f"{ticker}:{range_key}", ttl, fetch)


def get_stock_earnings(ticker):
    ticker = ticker.strip().upper()

    def fetch():
        try:
            df = yf.Ticker(ticker).get_earnings_dates(limit=8)
        except Exception as e:
            print(f"Earnings fetch failed for {ticker}: {e}")
            return {"next_date": None, "history": []}

        if df is None or df.empty:
            return {"next_date": None, "history": []}

        next_date = None
        history = []
        for ts, row in df.iterrows():
            reported_eps = _num(row.get("Reported EPS"))
            entry = {
                "date": ts.strftime("%Y-%m-%d"),
                "estimate_eps": _num(row.get("EPS Estimate")),
                "reported_eps": reported_eps,
                "surprise_pct": _num(row.get("Surprise(%)")),
            }
            if reported_eps is None and next_date is None:
                next_date = entry["date"]
            if reported_eps is not None:
                history.append(entry)

        return {"next_date": next_date, "history": history[:4]}

    return _cached(_earnings_cache, ticker, EARNINGS_CACHE_TTL_SECONDS, fetch)


def get_stock_financials(ticker):
    ticker = ticker.strip().upper()

    def fetch():
        try:
            qis = yf.Ticker(ticker).quarterly_income_stmt
        except Exception as e:
            print(f"Financials fetch failed for {ticker}: {e}")
            return []

        if qis is None or qis.empty:
            return []

        quarters = []
        for col in qis.columns[:6]:
            revenue = qis.loc["Total Revenue", col] if "Total Revenue" in qis.index else None
            net_income = qis.loc["Net Income", col] if "Net Income" in qis.index else None
            quarters.append({
                "quarter": col.strftime("%Y-%m-%d") if hasattr(col, "strftime") else str(col),
                "revenue": _num(revenue),
                "net_income": _num(net_income),
            })

        quarters.reverse()  # oldest -> newest, left-to-right on the page
        return quarters

    return _cached(_financials_cache, ticker, FINANCIALS_CACHE_TTL_SECONDS, fetch)


def get_stock_news(ticker, limit=8):
    ticker = ticker.strip().upper()

    def fetch():
        try:
            raw_items = yf.Ticker(ticker).news or []
        except Exception:
            raw_items = []
        return market_data.parse_yf_news_items(raw_items)[:limit]

    return _cached(_news_cache, ticker, NEWS_CACHE_TTL_SECONDS, fetch)
