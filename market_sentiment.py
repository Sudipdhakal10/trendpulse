"""
Market sentiment for the Sentiment page: a homegrown 0-100 "Fear & Greed"
style score, plus call/put options volume for the major index ETFs.

This is NOT CNN's actual Fear & Greed Index -- there's no public API for
that, only an undocumented endpoint CNN itself could change or block at
any time. Instead this is our own proxy, built the same way CNN's real
index conceptually works (blending volatility, momentum, and options
positioning) but entirely from data we already have reliable free access
to via yfinance:

  - VIX level (elevated VIX = fear, low VIX = complacency/greed)
  - S&P 500 price vs its 125-day moving average (a classic momentum
    read -- above trend = greed, below = fear)
  - SPY options put/call volume ratio (more puts relative to calls =
    hedging/fear, more calls = speculative greed)

Each component is scored 0-100 independently and the overall score is
the average of whichever components successfully loaded, so one flaky
data source (yfinance is documented elsewhere in this app as unreliable
on cloud IPs) degrades the result rather than breaking the whole page.
"""

import time

import yfinance as yf

CACHE_TTL_SECONDS = 15 * 60
# Yahoo's unofficial API is known to slow-walk/rate-limit cloud IPs under
# repeated traffic (see market_data.py's momentum scan for the same issue)
# -- firing 4 requests back-to-back for the options table can trip that,
# so a failed ticker is retried much sooner than a successful one's normal
# 15-minute TTL, instead of sitting broken for the full cache window.
OPTIONS_ERROR_CACHE_TTL_SECONDS = 90
OPTIONS_REQUEST_SPACING_SECONDS = 0.4
OPTIONS_FETCH_ATTEMPTS = 2
OPTIONS_RETRY_DELAY_SECONDS = 1.5

_fear_greed_cache = {"data": None, "timestamp": 0}
_options_cache = {}  # symbol -> {"data": {...}, "timestamp": float, "is_error": bool}

MAJOR_INDEX_ETFS = ["SPY", "QQQ", "DIA", "IWM"]


def _clamp(value, lo=0.0, hi=100.0):
    return max(lo, min(hi, value))


def _score_vix(vix_level):
    """VIX <=12 -> 100 (extreme calm/greed), 20 -> 50 (neutral), >=35 -> 0
    (extreme fear). These bands match how VIX levels are commonly read."""
    if vix_level <= 12:
        return 100.0
    if vix_level <= 20:
        return 100 - (vix_level - 12) / (20 - 12) * 50
    if vix_level <= 35:
        return 50 - (vix_level - 20) / (35 - 20) * 50
    return 0.0


def _score_momentum(pct_above_ma125):
    """S&P 500 vs its own 125-day moving average. 0% (right at the
    average) -> 50 (neutral); +-10% away -> +-50 points, clamped."""
    return _clamp(50 + pct_above_ma125 * 5)


def _score_put_call(ratio):
    """SPY put/call volume ratio. <=0.4 -> 100 (greed), 0.7 -> 50
    (neutral, roughly the typical baseline), >=1.2 -> 0 (fear)."""
    if ratio <= 0.4:
        return 100.0
    if ratio <= 0.7:
        return 100 - (ratio - 0.4) / (0.7 - 0.4) * 50
    if ratio <= 1.2:
        return 50 - (ratio - 0.7) / (1.2 - 0.7) * 50
    return 0.0


def _label_for_score(score):
    if score < 25:
        return "Extreme Fear"
    if score < 45:
        return "Fear"
    if score < 55:
        return "Neutral"
    if score < 75:
        return "Greed"
    return "Extreme Greed"


def _compute_fear_greed_index():
    components = {}

    try:
        vix_hist = yf.Ticker("^VIX").history(period="3mo")
        vix_level = float(vix_hist["Close"].iloc[-1])
        components["vix"] = {
            "label": "Volatility (VIX)",
            "value": round(vix_level, 2),
            "score": round(_score_vix(vix_level)),
        }
    except Exception as e:
        print(f"Fear & Greed VIX component failed: {e}")
        components["vix"] = None

    try:
        spx_hist = yf.Ticker("^GSPC").history(period="1y")
        current = float(spx_hist["Close"].iloc[-1])
        ma125 = float(spx_hist["Close"].tail(125).mean())
        pct_above = (current - ma125) / ma125 * 100
        components["momentum"] = {
            "label": "S&P 500 vs 125-Day Average",
            "value": round(pct_above, 2),
            "score": round(_score_momentum(pct_above)),
        }
    except Exception as e:
        print(f"Fear & Greed momentum component failed: {e}")
        components["momentum"] = None

    try:
        spy = yf.Ticker("SPY")
        exp = spy.options[0]
        chain = spy.option_chain(exp)
        call_vol = float(chain.calls["volume"].sum())
        put_vol = float(chain.puts["volume"].sum())
        ratio = (put_vol / call_vol) if call_vol else None
        components["put_call"] = {
            "label": "SPY Options Put/Call Ratio",
            "value": round(ratio, 3) if ratio is not None else None,
            "score": round(_score_put_call(ratio)) if ratio is not None else None,
        }
    except Exception as e:
        print(f"Fear & Greed put/call component failed: {e}")
        components["put_call"] = None

    valid_scores = [c["score"] for c in components.values() if c and c.get("score") is not None]
    overall = round(sum(valid_scores) / len(valid_scores)) if valid_scores else None
    label = _label_for_score(overall) if overall is not None else "Unavailable"

    return {"score": overall, "label": label, "components": components}


def get_fear_greed_index():
    now = time.time()
    if _fear_greed_cache["data"] is not None and (now - _fear_greed_cache["timestamp"]) < CACHE_TTL_SECONDS:
        return _fear_greed_cache["data"]

    result = _compute_fear_greed_index()
    _fear_greed_cache["data"] = result
    _fear_greed_cache["timestamp"] = now
    return result


def _fetch_options_row(symbol):
    t = yf.Ticker(symbol)
    exp = t.options[0]
    chain = t.option_chain(exp)
    call_vol = float(chain.calls["volume"].sum())
    put_vol = float(chain.puts["volume"].sum())
    call_oi = float(chain.calls["openInterest"].sum())
    put_oi = float(chain.puts["openInterest"].sum())
    ratio = (put_vol / call_vol) if call_vol else None

    return {
        "ticker": symbol,
        "expiration": exp,
        "call_volume": call_vol,
        "put_volume": put_vol,
        "call_open_interest": call_oi,
        "put_open_interest": put_oi,
        "put_call_ratio": round(ratio, 3) if ratio is not None else None,
    }


def _fetch_options_row_with_retry(symbol):
    last_error = None
    for attempt in range(OPTIONS_FETCH_ATTEMPTS):
        try:
            return _fetch_options_row(symbol)
        except Exception as e:
            last_error = e
            if attempt < OPTIONS_FETCH_ATTEMPTS - 1:
                time.sleep(OPTIONS_RETRY_DELAY_SECONDS)
    raise last_error


def get_options_sentiment():
    now = time.time()
    result = []

    for i, symbol in enumerate(MAJOR_INDEX_ETFS):
        cached = _options_cache.get(symbol)
        ttl = OPTIONS_ERROR_CACHE_TTL_SECONDS if cached and cached["is_error"] else CACHE_TTL_SECONDS
        if cached and (now - cached["timestamp"]) < ttl:
            result.append(cached["data"])
            continue

        try:
            row = _fetch_options_row_with_retry(symbol)
            _options_cache[symbol] = {"data": row, "timestamp": now, "is_error": False}
        except Exception as e:
            print(f"Options sentiment fetch failed for {symbol}: {e}")
            row = {"ticker": symbol, "error": "Could not load options data."}
            _options_cache[symbol] = {"data": row, "timestamp": now, "is_error": True}
        result.append(row)

        # Space out sequential requests to the same upstream host so this
        # doesn't itself look like the rapid-fire traffic Yahoo rate-limits.
        if i < len(MAJOR_INDEX_ETFS) - 1:
            time.sleep(OPTIONS_REQUEST_SPACING_SECONDS)

    return result
