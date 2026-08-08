"""
Stock Watchlist Web App — backend

Run with:
    python app.py

Then open http://localhost:8000 in your browser.
"""

import os
import re
import smtplib
import threading
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel

import backtester
import config
import db
import alpaca_data
import autotrade
import market_data
import market_sentiment
import signals
import stock_lookup

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SESSION_SECRET_KEY,
    https_only=config.SESSION_COOKIE_SECURE,
)

db.init_db()

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,32}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ============ Auth helpers ============

def require_api_login(request: Request) -> int:
    """Dependency for API routes — returns the logged-in user's id, or a
    401 instead of a page redirect (API calls expect JSON)."""
    user_id = request.session.get("user_id")
    if user_id is None:
        raise HTTPException(status_code=401, detail="Not logged in")
    return user_id


def is_logged_in(request: Request) -> bool:
    return request.session.get("user_id") is not None


def require_admin(user_id: int = Depends(require_api_login)) -> int:
    """Dependency for admin-only API routes — 403s anyone who isn't the
    account with is_admin=1."""
    user = db.get_user_by_id(user_id)
    if not user or not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user_id


def is_admin_user(request: Request) -> bool:
    user_id = request.session.get("user_id")
    if user_id is None:
        return False
    user = db.get_user_by_id(user_id)
    return bool(user and user["is_admin"])


# ============ Rate limiting ============
# Small in-memory limiter (fine for a single-process app like this one —
# no need for Redis/etc). Keyed by client IP, so it depends on uvicorn
# actually trusting Railway's X-Forwarded-For header (see proxy_headers
# below); otherwise every request looks like it's coming from the same
# upstream proxy IP and one attacker could lock out everyone.

_rate_limit_lock = threading.Lock()
_rate_limit_buckets = defaultdict(deque)
_rate_limit_last_cleanup = [0.0]
RATE_LIMIT_CLEANUP_INTERVAL_SECONDS = 600


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _rate_limited(key: str, max_attempts: int, window_seconds: int) -> bool:
    """Returns True if `key` has already hit max_attempts within the
    trailing window_seconds (and should be rejected), else records this
    attempt and returns False."""
    now = time.time()
    with _rate_limit_lock:
        bucket = _rate_limit_buckets[key]
        while bucket and bucket[0] < now - window_seconds:
            bucket.popleft()

        limited = len(bucket) >= max_attempts
        if not limited:
            bucket.append(now)

        # Periodic sweep so IPs that stop making requests don't sit in
        # memory forever -- keeps this bounded without a background job.
        if now - _rate_limit_last_cleanup[0] > RATE_LIMIT_CLEANUP_INTERVAL_SECONDS:
            for stale_key in [k for k, v in _rate_limit_buckets.items() if not v]:
                del _rate_limit_buckets[stale_key]
            _rate_limit_last_cleanup[0] = now

    return limited


# ============ Request models ============

class AddTicker(BaseModel):
    ticker: str


class ToggleStrategy(BaseModel):
    ticker: str
    strategy: str
    enabled: bool


class AutotradeToggle(BaseModel):
    enabled: bool


class AlpacaKeys(BaseModel):
    api_key: str
    secret_key: str


class AutotradeWatchlistItem(BaseModel):
    ticker: str
    entry_strategy: str
    exit_strategy: str
    allocation_mode: str | None = None  # None (default cap), "dollars", or "shares"
    allocation_value: float | None = None


class ManualTradeRequest(BaseModel):
    ticker: str
    side: str  # "buy" or "sell"
    mode: str  # "dollars" or "shares"
    value: float


class BacktestRequest(BaseModel):
    ticker: str
    entry_strategy: str
    exit_strategy: str
    period: str = "2y"
    starting_capital: float = 10000


class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str
    email: str
    password: str
    invite_code: str = ""
    first_name: str = ""
    last_name: str = ""
    phone: str = ""
    street: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""


class ProfileUpdate(BaseModel):
    first_name: str
    last_name: str
    email: str
    phone: str
    street: str
    city: str
    state: str
    zip_code: str


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


# ============ Email ============

def send_email(to_address, subject, body):
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = config.EMAIL_ADDRESS
        msg["To"] = to_address

        with smtplib.SMTP(config.SMTP_SERVER, config.SMTP_PORT) as server:
            server.starttls()
            server.login(config.EMAIL_ADDRESS, config.EMAIL_APP_PASSWORD)
            server.send_message(msg)
        print(f"Email sent to {to_address}: {subject}")
        return True
    except Exception as e:
        print(f"Failed to send email: {e}")
        return False


def send_database_backup():
    """Emails a copy of the whole SQLite database to EMAIL_TO. Railway's
    own volume backups need a paid Pro plan we're not on, and the
    database file is the only copy of every real account, watchlist, and
    AutoTrade setup that exists -- losing it means losing all of it, so
    this is cheap insurance reusing credentials that already work.
    Fine as an email attachment as long as the file stays small (it's
    currently tens of KB); revisit this approach if that ever changes."""
    if not os.path.exists(config.DB_PATH):
        print("Database backup skipped: no database file found.")
        return False

    try:
        today = date.today().isoformat()

        msg = MIMEMultipart()
        msg["Subject"] = f"TrendPulse database backup — {today}"
        msg["From"] = config.EMAIL_ADDRESS
        msg["To"] = config.EMAIL_TO
        msg.attach(MIMEText(
            "Automated daily backup of the TrendPulse database is attached. "
            "Save it somewhere safe if you want an extra copy beyond your inbox."
        ))

        with open(config.DB_PATH, "rb") as f:
            attachment = MIMEBase("application", "octet-stream")
            attachment.set_payload(f.read())
        encoders.encode_base64(attachment)
        attachment.add_header("Content-Disposition", f'attachment; filename="watchlist-backup-{today}.db"')
        msg.attach(attachment)

        with smtplib.SMTP(config.SMTP_SERVER, config.SMTP_PORT) as server:
            server.starttls()
            server.login(config.EMAIL_ADDRESS, config.EMAIL_APP_PASSWORD)
            server.send_message(msg)
        print(f"Database backup emailed to {config.EMAIL_TO}")
        return True
    except Exception as e:
        print(f"Database backup failed: {e}")
        return False


# ============ Core check logic ============

def check_ticker(user_id, ticker):
    """Checks one ticker's enabled strategies against the latest data.
    Returns a list of newly-triggered alert messages."""
    enabled_strategies = db.get_enabled_strategies(user_id, ticker)
    if not enabled_strategies:
        return []

    try:
        result = signals.evaluate_latest(ticker)
    except Exception as e:
        # yfinance/Yahoo is known to be flaky on cloud IPs -- one bad
        # ticker shouldn't stop the rest of this user's watchlist (or,
        # since check_all() shares this same call across every user,
        # every user processed after this one) from being checked.
        print(f"Daily check failed for {ticker} (user {user_id}): {e}")
        return []
    if result is None:
        return []

    triggered_messages = []
    for strategy in enabled_strategies:
        if result["signals"].get(strategy):
            if not db.already_alerted_today(user_id, ticker, strategy):
                message = (
                    f"{ticker}: {strategy} triggered on {result['date']} "
                    f"(price {result['price']:.2f})"
                )
                db.record_alert(user_id, ticker, strategy, message)
                triggered_messages.append(message)

    return triggered_messages


def check_watchlist_for_user(user):
    """Runs the check across one user's watchlist and emails them a
    summary if anything triggered."""
    watchlist = db.get_watchlist(user["id"])
    all_triggered = []

    for ticker in watchlist:
        all_triggered.extend(check_ticker(user["id"], ticker))

    if all_triggered:
        body = "\n".join(all_triggered)
        send_email(user["email"], "Stock Watchlist Alert", body)

    return all_triggered


def check_all():
    """Runs the daily check for every registered user. This is what the
    scheduler calls, and what a single logged-in user's "Check Now"
    button also uses (scoped to just that user)."""
    all_triggered = []
    for user in db.get_all_users():
        try:
            all_triggered.extend(check_watchlist_for_user(user))
        except Exception as e:
            # One user's failure (bad data, a send_email hiccup, etc.)
            # must not silently skip every other user still left in
            # this loop -- that's exactly the kind of bug where nobody
            # notices until someone says "I haven't gotten alerts lately."
            print(f"Daily check failed for user {user['id']}: {e}")
    return all_triggered


# ============ Alpaca trading guard ============
# run_rotation_bot() already checks Alpaca's own pending orders to avoid
# re-buying something already in flight, but that only closes the gap
# between separate sequential runs -- it doesn't stop two runs for the
# SAME user starting close enough together that both snapshot "nothing
# pending yet" before either's order has synced to Alpaca (a rapid
# double-click on "Run Now" or a manual Buy/Sell, two open tabs, or a
# manual trade landing at the same moment as the scheduled AutoTrade
# run). This lock makes any second concurrent trade *of any kind* for a
# user a no-op instead of a second real order -- a scheduled run and a
# manual Screener trade for the same person must not overlap either.
_trading_lock = threading.Lock()
_users_trading = set()


def _run_exclusive_for_user(user_id, fn, *args, **kwargs):
    with _trading_lock:
        if user_id in _users_trading:
            return {"error": "A trade is already in progress for this account -- try again in a moment.", "log": []}
        _users_trading.add(user_id)

    try:
        return fn(*args, **kwargs)
    finally:
        with _trading_lock:
            _users_trading.discard(user_id)


def run_rotation_bot_exclusive(user_id, api_key, secret_key):
    return _run_exclusive_for_user(user_id, autotrade.run_rotation_bot, user_id, api_key, secret_key)


def place_manual_order_exclusive(user_id, api_key, secret_key, ticker, side, mode, value):
    return _run_exclusive_for_user(
        user_id, autotrade.place_manual_order, user_id, api_key, secret_key, ticker, side, mode, value,
    )


# ============ Scheduler ============

# Explicit timezone so DAILY_CHECK_TIME/AUTOTRADE_RUN_TIME/DB_BACKUP_TIME
# mean Central Time regardless of whatever timezone the host container
# defaults to (Railway's containers run in UTC unless told otherwise) --
# without this, a scheduled run at e.g. "09:35" would actually fire at
# 9:35 UTC (early hours in the US), not 9:35 Central as the times are
# meant to be read.
SCHEDULER_TIMEZONE = ZoneInfo("America/Chicago")
scheduler = BackgroundScheduler(timezone=SCHEDULER_TIMEZONE)
hour, minute = config.DAILY_CHECK_TIME.split(":")
scheduler.add_job(check_all, "cron", hour=int(hour), minute=int(minute))


def scheduled_autotrade_run():
    """Runs the rotation bot once per user who has AutoTrade turned on and
    has their own Alpaca keys saved."""
    for user in db.get_all_users():
        if not user["autotrade_enabled"]:
            continue
        if not user["alpaca_api_key"] or not user["alpaca_secret_key"]:
            continue
        try:
            run_rotation_bot_exclusive(user["id"], user["alpaca_api_key"], user["alpaca_secret_key"])
        except Exception as e:
            # One user's bad keys/ticker/API hiccup must not stop every
            # other user still left in this loop from trading that day.
            print(f"Scheduled AutoTrade run failed for user {user['id']}: {e}")


autotrade_hour, autotrade_minute = config.AUTOTRADE_RUN_TIME.split(":")
scheduler.add_job(scheduled_autotrade_run, "cron", hour=int(autotrade_hour), minute=int(autotrade_minute))

# The momentum scan covers ~500 tickers and takes tens of seconds, so it
# runs on a timer in the background instead of on a page request — by the
# time anyone loads the Watchlist page, the cache is already warm. Delayed
# 90s past startup rather than firing immediately, so it doesn't pile its
# own memory use onto the app's own boot (a real cause of an OOM restart).
scheduler.add_job(
    market_data.refresh_momentum_cache, "interval", minutes=15,
    next_run_time=datetime.now() + timedelta(seconds=90),
)

# Same reasoning as the momentum scan above, on its own timer since it's a
# separate ~500-ticker download (technical fields for the Screener page's
# Scan tab, not momentum movers). Staggered further past startup and given
# a longer interval since technicals don't shift within minutes the way an
# intraday mover list does, and to avoid both scans hitting Yahoo at once.
scheduler.add_job(
    market_data.refresh_screener_cache, "interval", minutes=30,
    next_run_time=datetime.now() + timedelta(seconds=180),
)

backup_hour, backup_minute = config.DB_BACKUP_TIME.split(":")
scheduler.add_job(send_database_backup, "cron", hour=int(backup_hour), minute=int(backup_minute))

scheduler.start()


# ============ API routes ============

@app.get("/api/watchlist")
def api_get_watchlist(user_id: int = Depends(require_api_login)):
    return db.get_watchlist(user_id)


@app.post("/api/watchlist")
def api_add_ticker(payload: AddTicker, user_id: int = Depends(require_api_login)):
    ticker = payload.ticker.strip().upper()
    db.ensure_ticker_rows(user_id, ticker)
    return {"status": "ok", "ticker": ticker}


@app.delete("/api/watchlist/{ticker}")
def api_remove_ticker(ticker: str, user_id: int = Depends(require_api_login)):
    db.remove_ticker(user_id, ticker.upper())
    return {"status": "ok"}


@app.post("/api/toggle")
def api_toggle(payload: ToggleStrategy, user_id: int = Depends(require_api_login)):
    db.toggle_strategy(user_id, payload.ticker, payload.strategy, payload.enabled)
    return {"status": "ok"}


@app.get("/api/alerts")
def api_get_alerts(user_id: int = Depends(require_api_login)):
    return db.get_recent_alerts(user_id)


@app.post("/api/check-now")
def api_check_now(user_id: int = Depends(require_api_login)):
    user = db.get_user_by_id(user_id)
    triggered = check_watchlist_for_user(user)
    return {"triggered": triggered, "count": len(triggered)}


@app.get("/api/strategies")
def api_get_strategies(_=Depends(require_api_login)):
    return signals.ALL_STRATEGIES


@app.get("/api/news")
def api_news(_=Depends(require_api_login)):
    return market_data.get_market_news()


@app.get("/api/momentum")
def api_momentum(_=Depends(require_api_login)):
    return market_data.get_momentum_movers()


@app.get("/api/screener/scan")
def api_screener_scan(_=Depends(require_api_login)):
    return market_data.get_screener_data()


@app.post("/api/backtest")
def api_backtest(payload: BacktestRequest, _=Depends(require_api_login)):
    ticker = payload.ticker.strip().upper()
    if not ticker:
        raise HTTPException(status_code=400, detail="Ticker is required.")
    if payload.period not in backtester.BACKTEST_PERIODS:
        raise HTTPException(status_code=400, detail="Invalid period.")
    return backtester.run_backtest(
        ticker, payload.entry_strategy, payload.exit_strategy,
        payload.period, payload.starting_capital,
    )


@app.get("/api/fear-greed")
def api_fear_greed(_=Depends(require_api_login)):
    return market_sentiment.get_fear_greed_index()


@app.get("/api/options-sentiment")
def api_options_sentiment(_=Depends(require_api_login)):
    return market_sentiment.get_options_sentiment()


@app.get("/api/stock-search")
def api_stock_search(q: str = "", _=Depends(require_api_login)):
    return stock_lookup.search_symbols(q)


@app.get("/api/stock/{ticker}")
def api_stock_detail(ticker: str, _=Depends(require_api_login)):
    ticker = ticker.strip().upper()
    snapshot = stock_lookup.get_stock_snapshot(ticker)
    if snapshot.get("_error"):
        raise HTTPException(status_code=404, detail=snapshot["_error"])
    return {
        "snapshot": snapshot,
        "earnings": stock_lookup.get_stock_earnings(ticker),
        "financials": stock_lookup.get_stock_financials(ticker),
        "news": stock_lookup.get_stock_news(ticker),
    }


@app.get("/api/stock/{ticker}/chart")
def api_stock_chart(ticker: str, range: str = "1M", _=Depends(require_api_login)):
    return {"range": range.upper(), "points": stock_lookup.get_stock_chart(ticker, range)}


@app.get("/api/live-prices")
def api_live_prices(user_id: int = Depends(require_api_login)):
    watchlist = db.get_watchlist(user_id)
    tickers = list(watchlist.keys())
    return alpaca_data.get_live_snapshots(tickers)


@app.get("/api/autotrade/status")
def api_autotrade_status(user_id: int = Depends(require_api_login)):
    user = db.get_user_by_id(user_id)
    return autotrade.get_account_summary(user["alpaca_api_key"], user["alpaca_secret_key"])


@app.get("/api/portfolio/history")
def api_portfolio_history(range: str = "1M", user_id: int = Depends(require_api_login)):
    user = db.get_user_by_id(user_id)
    result = autotrade.get_portfolio_history(user["alpaca_api_key"], user["alpaca_secret_key"], range)
    if "error" not in result:
        result["benchmark_return_pct"] = autotrade.get_benchmark_return(range)
    return result


@app.get("/api/portfolio/performance")
def api_portfolio_performance(user_id: int = Depends(require_api_login)):
    user = db.get_user_by_id(user_id)
    return autotrade.get_trade_performance(user["alpaca_api_key"], user["alpaca_secret_key"])


@app.get("/api/autotrade/trades")
def api_autotrade_trades(user_id: int = Depends(require_api_login)):
    return db.get_recent_trades(user_id)


@app.post("/api/autotrade/run-now")
def api_autotrade_run_now(user_id: int = Depends(require_api_login)):
    user = db.get_user_by_id(user_id)
    try:
        return run_rotation_bot_exclusive(user_id, user["alpaca_api_key"], user["alpaca_secret_key"])
    except Exception as e:
        # run_rotation_bot isolates per-ticker failures internally, but this
        # is the last line of defense against anything unexpected still
        # surfacing as a raw 500 to a user manually clicking "Run Now".
        return {"error": f"AutoTrade run failed: {e}", "log": []}


@app.post("/api/trade/manual")
def api_manual_trade(payload: ManualTradeRequest, user_id: int = Depends(require_api_login)):
    ticker = payload.ticker.strip().upper()
    if not ticker:
        raise HTTPException(status_code=400, detail="Ticker is required.")
    if payload.side not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="Side must be 'buy' or 'sell'.")
    if payload.mode not in ("dollars", "shares"):
        raise HTTPException(status_code=400, detail="Mode must be 'dollars' or 'shares'.")
    if payload.value is None or payload.value <= 0:
        raise HTTPException(status_code=400, detail="Enter a positive amount.")

    user = db.get_user_by_id(user_id)
    try:
        return place_manual_order_exclusive(
            user_id, user["alpaca_api_key"], user["alpaca_secret_key"],
            ticker, payload.side, payload.mode, payload.value,
        )
    except Exception as e:
        # place_manual_order already returns {"error": ...} for expected
        # failure modes -- this is the last line of defense against
        # anything unexpected surfacing as a raw 500 to the browser.
        return {"error": f"Trade failed: {e}"}


@app.get("/api/autotrade/enabled")
def api_autotrade_get_enabled(user_id: int = Depends(require_api_login)):
    user = db.get_user_by_id(user_id)
    return {"enabled": bool(user["autotrade_enabled"])}


@app.post("/api/autotrade/enabled")
def api_autotrade_set_enabled(payload: AutotradeToggle, user_id: int = Depends(require_api_login)):
    db.set_autotrade_enabled(user_id, payload.enabled)
    return {"status": "ok"}


@app.get("/api/autotrade/keys")
def api_autotrade_get_keys(user_id: int = Depends(require_api_login)):
    user = db.get_user_by_id(user_id)
    api_key = user["alpaca_api_key"]
    return {
        "has_keys": bool(api_key and user["alpaca_secret_key"]),
        "api_key_preview": (api_key[:4] + "…" + api_key[-4:]) if len(api_key) > 8 else api_key,
    }


@app.post("/api/autotrade/keys")
def api_autotrade_set_keys(payload: AlpacaKeys, user_id: int = Depends(require_api_login)):
    db.set_alpaca_keys(user_id, payload.api_key.strip(), payload.secret_key.strip())
    return {"status": "ok"}


@app.get("/api/autotrade/config")
def api_autotrade_config(_=Depends(require_api_login)):
    return {
        "max_per_ticker": autotrade.MAX_PER_TICKER,
        "min_trade_dollars": autotrade.MIN_TRADE_DOLLARS,
        "paper_trading": config.PAPER_TRADING,
        "run_time": config.AUTOTRADE_RUN_TIME,
        "take_profit_pct": signals.TAKE_PROFIT_PCT,
        "stop_loss_pct": signals.STOP_LOSS_PCT,
    }


@app.get("/api/autotrade/strategies")
def api_autotrade_strategies(_=Depends(require_api_login)):
    return {"entry_strategies": signals.ALL_STRATEGIES, "exit_strategies": signals.ALL_EXIT_STRATEGIES}


@app.get("/api/autotrade/watchlist")
def api_autotrade_get_watchlist(user_id: int = Depends(require_api_login)):
    return db.get_autotrade_watchlist(user_id)


@app.post("/api/autotrade/watchlist")
def api_autotrade_add_watchlist(payload: AutotradeWatchlistItem, user_id: int = Depends(require_api_login)):
    ticker = payload.ticker.strip().upper()
    if not ticker:
        raise HTTPException(status_code=400, detail="Ticker is required.")
    if payload.entry_strategy not in signals.ALL_STRATEGIES:
        raise HTTPException(status_code=400, detail="Unknown entry strategy.")
    if payload.exit_strategy not in signals.ALL_EXIT_STRATEGIES:
        raise HTTPException(status_code=400, detail="Unknown exit strategy.")

    allocation_mode = payload.allocation_mode or None
    allocation_value = payload.allocation_value
    if allocation_mode not in (None, "dollars", "shares"):
        raise HTTPException(status_code=400, detail="Unknown allocation mode.")
    if allocation_mode is not None:
        if allocation_value is None or allocation_value <= 0:
            raise HTTPException(status_code=400, detail="Enter a positive allocation amount.")
    else:
        allocation_value = None

    db.upsert_autotrade_ticker(
        user_id, ticker, payload.entry_strategy, payload.exit_strategy,
        allocation_mode, allocation_value,
    )
    return {"status": "ok", "ticker": ticker}


@app.delete("/api/autotrade/watchlist/{ticker}")
def api_autotrade_remove_watchlist(ticker: str, user_id: int = Depends(require_api_login)):
    db.remove_autotrade_ticker(user_id, ticker.upper())
    return {"status": "ok"}


@app.get("/api/profile")
def api_get_profile(user_id: int = Depends(require_api_login)):
    user = db.get_user_by_id(user_id)
    return {
        "username": user["username"],
        "first_name": user["first_name"],
        "last_name": user["last_name"],
        "email": user["email"],
        "phone": user["phone"],
        "street": user["street"],
        "city": user["city"],
        "state": user["state"],
        "zip_code": user["zip_code"],
        "created_at": user["created_at"],
    }


@app.post("/api/profile")
def api_update_profile(payload: ProfileUpdate, user_id: int = Depends(require_api_login)):
    email = payload.email.strip()
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Please enter a valid email address.")

    db.update_profile(
        user_id,
        payload.first_name.strip(),
        payload.last_name.strip(),
        email,
        payload.phone.strip(),
        payload.street.strip(),
        payload.city.strip(),
        payload.state.strip(),
        payload.zip_code.strip(),
    )
    return {"status": "ok"}


@app.post("/api/profile/password")
def api_change_password(payload: PasswordChange, user_id: int = Depends(require_api_login)):
    user = db.get_user_by_id(user_id)
    if not db.verify_password(payload.current_password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Current password is incorrect.")
    if len(payload.new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters.")

    db.set_password(user_id, payload.new_password)
    return {"status": "ok"}


@app.get("/api/admin/users")
def api_admin_list_users(_=Depends(require_admin)):
    users = db.get_all_users()
    return [
        {
            "id": u["id"],
            "username": u["username"],
            "first_name": u["first_name"],
            "last_name": u["last_name"],
            "email": u["email"],
            "autotrade_enabled": bool(u["autotrade_enabled"]),
            "has_alpaca_keys": bool(u["alpaca_api_key"] and u["alpaca_secret_key"]),
            "is_admin": bool(u["is_admin"]),
            "created_at": u["created_at"],
        }
        for u in sorted(users, key=lambda u: u["created_at"], reverse=True)
    ]


@app.get("/api/admin/invites")
def api_admin_list_invites(_=Depends(require_admin)):
    return db.get_all_invites()


@app.post("/api/admin/invites")
def api_admin_create_invite(user_id: int = Depends(require_admin)):
    code = db.create_invite(user_id)
    return {"code": code}


# ============ Serve the frontend ============

app.mount("/static", StaticFiles(directory="static"), name="static")


NO_STORE_HEADERS = {"Cache-Control": "no-store"}


@app.get("/")
def serve_home():
    return FileResponse("static/home.html", headers=NO_STORE_HEADERS)


@app.get("/login")
def serve_login():
    return FileResponse("static/login.html", headers=NO_STORE_HEADERS)


@app.get("/register")
def serve_register():
    return FileResponse("static/register.html", headers=NO_STORE_HEADERS)


@app.post("/api/login")
def api_login(payload: LoginRequest, request: Request):
    if _rate_limited(f"login:{_client_ip(request)}", max_attempts=10, window_seconds=300):
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again in a few minutes.")

    user = db.get_user_by_username(payload.username.strip())
    if user and db.verify_password(payload.password, user["password_hash"]):
        request.session["user_id"] = user["id"]
        return {"status": "ok"}
    raise HTTPException(status_code=401, detail="Invalid username or password")


@app.post("/api/register")
def api_register(payload: RegisterRequest, request: Request):
    if _rate_limited(f"register:{_client_ip(request)}", max_attempts=5, window_seconds=3600):
        raise HTTPException(status_code=429, detail="Too many registration attempts. Try again later.")

    username = payload.username.strip()
    email = payload.email.strip()
    password = payload.password
    invite_code = payload.invite_code.strip()

    # Bootstrap exception: a brand-new install has no users yet, so no one
    # could ever generate the first invite. Only the very first account
    # ever created skips this check -- every registration after that
    # requires a real one, since db.get_all_users() is no longer empty.
    is_bootstrap = not db.get_all_users()

    if not is_bootstrap:
        if not invite_code:
            raise HTTPException(status_code=400, detail="An invite code is required to register.")
        invite = db.get_invite(invite_code)
        if not invite:
            raise HTTPException(status_code=400, detail="That invite code isn't valid.")
        if invite["used_by"] is not None:
            raise HTTPException(status_code=400, detail="That invite code has already been used.")

    if not USERNAME_RE.match(username):
        raise HTTPException(status_code=400, detail="Username must be 3-32 characters (letters, numbers, _ . -).")
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Please enter a valid email address.")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    if db.get_user_by_username(username):
        raise HTTPException(status_code=400, detail="That username is already taken.")

    user_id = db.create_user(
        username, email, password,
        first_name=payload.first_name.strip(),
        last_name=payload.last_name.strip(),
        phone=payload.phone.strip(),
        street=payload.street.strip(),
        city=payload.city.strip(),
        state=payload.state.strip(),
        zip_code=payload.zip_code.strip(),
    )
    if is_bootstrap:
        # The very first account on a fresh install becomes admin
        # immediately, so they can generate invites for everyone after them.
        db.set_admin(user_id)
    else:
        db.mark_invite_used(invite_code, user_id)
    request.session["user_id"] = user_id
    return {"status": "ok"}


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")


@app.get("/watchlist")
def serve_watchlist(request: Request):
    if not is_logged_in(request):
        response = RedirectResponse("/login")
        response.headers["Cache-Control"] = "no-store"
        return response
    return FileResponse("static/index.html", headers=NO_STORE_HEADERS)


@app.get("/screener")
def serve_screener(request: Request):
    if not is_logged_in(request):
        response = RedirectResponse("/login")
        response.headers["Cache-Control"] = "no-store"
        return response
    return FileResponse("static/screener.html", headers=NO_STORE_HEADERS)


@app.get("/sentiment")
def serve_sentiment(request: Request):
    if not is_logged_in(request):
        response = RedirectResponse("/login")
        response.headers["Cache-Control"] = "no-store"
        return response
    return FileResponse("static/sentiment.html", headers=NO_STORE_HEADERS)


@app.get("/autotrade")
def serve_autotrade(request: Request):
    if not is_logged_in(request):
        response = RedirectResponse("/login")
        response.headers["Cache-Control"] = "no-store"
        return response
    return FileResponse("static/autotrade.html", headers=NO_STORE_HEADERS)


@app.get("/profile")
def serve_profile(request: Request):
    if not is_logged_in(request):
        response = RedirectResponse("/login")
        response.headers["Cache-Control"] = "no-store"
        return response
    return FileResponse("static/profile.html", headers=NO_STORE_HEADERS)


@app.get("/admin/users")
def serve_admin_users(request: Request):
    if not is_logged_in(request):
        response = RedirectResponse("/login")
        response.headers["Cache-Control"] = "no-store"
        return response
    if not is_admin_user(request):
        response = RedirectResponse("/watchlist")
        response.headers["Cache-Control"] = "no-store"
        return response
    return FileResponse("static/admin_users.html", headers=NO_STORE_HEADERS)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    print(f"Starting server. Open http://localhost:{port} in your browser.")
    print(f"Daily automatic check scheduled for {config.DAILY_CHECK_TIME} Central Time.")
    print(f"AutoTrade scheduled to run at {config.AUTOTRADE_RUN_TIME} Central Time.")
    print(f"Daily database backup email scheduled for {config.DB_BACKUP_TIME} Central Time.")
    # proxy_headers + forwarded_allow_ips="*": trust X-Forwarded-For from
    # whatever forwards to us. Safe here because Railway's edge is the
    # only way to reach this app -- there's no direct path that could
    # spoof it. Without this, request.client.host is Railway's internal
    # proxy IP for every request, which would make per-IP rate limiting
    # either a no-op or (worse) capable of locking out every user at once.
    uvicorn.run(app, host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*")
