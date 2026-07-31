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

import concurrent.futures
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


MOMENTUM_CHUNK_SIZE = 50  # tickers per yf.download batch -- bounds peak memory
MOMENTUM_CHUNK_THREADS = 8  # bounded concurrency within a chunk (was unbounded)
MOMENTUM_CHUNK_TIMEOUT_SECONDS = 45  # hard ceiling per chunk -- a normal chunk takes a few seconds


def _download_chunk(chunk):
    return yf.download(chunk, period="2mo", group_by="ticker", threads=MOMENTUM_CHUNK_THREADS, progress=False, timeout=10)


def refresh_momentum_cache(limit=10, min_relative_volume=2.0, min_pct_change=5.0):
    """Does the actual ~500-ticker scan and updates the cache. Split out
    from get_momentum_movers so app.py's scheduler can call this directly
    on a timer, keeping the cache warm without ever making a page request
    wait on the ~40-second scan.

    Downloads the universe in small chunks rather than one ~500-ticker
    batch -- holding all of that OHLCV data in memory at once was enough
    to push a memory-constrained host over its limit. Chunking keeps peak
    memory bounded no matter how large the universe is.

    Each chunk is also given a hard wall-clock ceiling. yfinance's per-
    request timeout only covers a single HTTP request, not any internal
    retry/backoff loop, and Yahoo's unofficial API is known to slow-walk
    or rate-limit cloud IPs under repeated traffic. Without an outer
    ceiling, one bad chunk can run long enough that this job is still
    "running" when the next 15-minute trigger fires (APScheduler then
    skips it: "maximum number of running instances reached") -- and the
    stuck run itself was very likely what pushed memory up enough to
    take the site down. Skipping a slow chunk and moving on keeps the
    whole scan bounded no matter how badly one chunk misbehaves."""
    universe = get_momentum_universe()
    movers = []

    for start in range(0, len(universe), MOMENTUM_CHUNK_SIZE):
        chunk = universe[start:start + MOMENTUM_CHUNK_SIZE]

        # Not a "with" block on purpose: ThreadPoolExecutor's context
        # manager calls shutdown(wait=True) on exit, which blocks until
        # the submitted thread actually finishes -- even after we've
        # already given up on it via result(timeout=...). For a request
        # that's genuinely hung (not just slow), that would wait forever,
        # defeating the entire point of the timeout. shutdown(wait=False)
        # lets us move on immediately; the orphaned thread is abandoned
        # and cleans itself up whenever (or if) the hung request ever
        # actually returns.
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            data = executor.submit(_download_chunk, chunk).result(timeout=MOMENTUM_CHUNK_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError:
            print(f"Momentum scan chunk timed out after {MOMENTUM_CHUNK_TIMEOUT_SECONDS}s, skipping {len(chunk)} tickers")
            executor.shutdown(wait=False)
            continue
        except Exception as e:
            print(f"Momentum scan chunk download failed: {e}")
            executor.shutdown(wait=False)
            continue
        executor.shutdown(wait=False)

        if data is None or data.empty:
            continue

        is_multi = isinstance(data.columns, pd.MultiIndex)

        for ticker in chunk:
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

        del data  # done with this chunk's data before the next one is fetched

    movers.sort(key=lambda m: m["pct_change"], reverse=True)
    result = movers[:limit]

    _MOMENTUM_CACHE["data"] = result
    _MOMENTUM_CACHE["timestamp"] = time.time()
    return result
