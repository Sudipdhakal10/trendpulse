"""
Stock Watchlist Web App — backend

Run with:
    python app.py

Then open http://localhost:8000 in your browser.
"""

import os
import re
import smtplib
from datetime import datetime
from email.mime.text import MIMEText

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel

import config
import db
import alpaca_data
import autotrade
import market_data
import signals

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


class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str
    email: str
    password: str
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


# ============ Core check logic ============

def check_ticker(user_id, ticker):
    """Checks one ticker's enabled strategies against the latest data.
    Returns a list of newly-triggered alert messages."""
    enabled_strategies = db.get_enabled_strategies(user_id, ticker)
    if not enabled_strategies:
        return []

    result = signals.evaluate_latest(ticker)
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
        all_triggered.extend(check_watchlist_for_user(user))
    return all_triggered


# ============ Scheduler ============

scheduler = BackgroundScheduler()
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
        autotrade.run_rotation_bot(user["id"], user["alpaca_api_key"], user["alpaca_secret_key"])


autotrade_hour, autotrade_minute = config.AUTOTRADE_RUN_TIME.split(":")
scheduler.add_job(scheduled_autotrade_run, "cron", hour=int(autotrade_hour), minute=int(autotrade_minute))

# The momentum scan covers ~500 tickers and takes tens of seconds, so it
# runs on a timer in the background instead of on a page request — by the
# time anyone loads the Watchlist page, the cache is already warm.
scheduler.add_job(market_data.refresh_momentum_cache, "interval", minutes=15, next_run_time=datetime.now())

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


@app.get("/api/live-prices")
def api_live_prices(user_id: int = Depends(require_api_login)):
    watchlist = db.get_watchlist(user_id)
    tickers = list(watchlist.keys())
    return alpaca_data.get_live_snapshots(tickers)


@app.get("/api/autotrade/status")
def api_autotrade_status(user_id: int = Depends(require_api_login)):
    user = db.get_user_by_id(user_id)
    return autotrade.get_account_summary(user["alpaca_api_key"], user["alpaca_secret_key"])


@app.get("/api/autotrade/trades")
def api_autotrade_trades(user_id: int = Depends(require_api_login)):
    return db.get_recent_trades(user_id)


@app.post("/api/autotrade/run-now")
def api_autotrade_run_now(user_id: int = Depends(require_api_login)):
    user = db.get_user_by_id(user_id)
    return autotrade.run_rotation_bot(user_id, user["alpaca_api_key"], user["alpaca_secret_key"])


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
    db.upsert_autotrade_ticker(user_id, ticker, payload.entry_strategy, payload.exit_strategy)
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


# ============ One-time trade_log cleanup (delete this block after use) ============
#
# An earlier bug placed duplicate AutoTrade orders on repeat runs before an
# order had filled; each duplicate submission also logged a trade_log row,
# even for orders later cancelled as duplicates. This is a narrow,
# temporary tool to inspect and remove those specific stale rows on the
# live database. Inert unless CLEANUP_TOKEN is set (Railway dashboard
# only, never in .env) -- remove the env var and this route once used.

CLEANUP_TOKEN = os.environ.get("CLEANUP_TOKEN", "")


def _require_cleanup_token(request: Request):
    if not CLEANUP_TOKEN or request.headers.get("X-Cleanup-Token") != CLEANUP_TOKEN:
        raise HTTPException(status_code=404)


@app.get("/admin/trade-log")
def admin_list_trade_log(request: Request):
    _require_cleanup_token(request)
    conn = db.get_connection()
    rows = conn.execute("SELECT * FROM trade_log ORDER BY created_at").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/admin/trade-log/delete")
def admin_delete_trade_log(request: Request, ids: str = ""):
    _require_cleanup_token(request)
    id_list = [int(x) for x in ids.split(",") if x.strip()]
    if not id_list:
        raise HTTPException(status_code=400, detail="Pass ?ids=1,2,3")
    conn = db.get_connection()
    conn.executemany("DELETE FROM trade_log WHERE id = ?", [(i,) for i in id_list])
    conn.commit()
    conn.close()
    return {"deleted": id_list}


# ============ Serve the frontend ============

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def serve_home():
    return FileResponse("static/home.html")


@app.get("/login")
def serve_login():
    return FileResponse("static/login.html")


@app.get("/register")
def serve_register():
    return FileResponse("static/register.html")


@app.post("/api/login")
def api_login(payload: LoginRequest, request: Request):
    user = db.get_user_by_username(payload.username.strip())
    if user and db.verify_password(payload.password, user["password_hash"]):
        request.session["user_id"] = user["id"]
        return {"status": "ok"}
    raise HTTPException(status_code=401, detail="Invalid username or password")


@app.post("/api/register")
def api_register(payload: RegisterRequest, request: Request):
    username = payload.username.strip()
    email = payload.email.strip()
    password = payload.password

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
    return FileResponse("static/index.html")


@app.get("/autotrade")
def serve_autotrade(request: Request):
    if not is_logged_in(request):
        response = RedirectResponse("/login")
        response.headers["Cache-Control"] = "no-store"
        return response
    return FileResponse("static/autotrade.html")


@app.get("/profile")
def serve_profile(request: Request):
    if not is_logged_in(request):
        response = RedirectResponse("/login")
        response.headers["Cache-Control"] = "no-store"
        return response
    return FileResponse("static/profile.html")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    print(f"Starting server. Open http://localhost:{port} in your browser.")
    print(f"Daily automatic check scheduled for {config.DAILY_CHECK_TIME}.")
    uvicorn.run(app, host="0.0.0.0", port=port)
