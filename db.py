"""
Database layer — a small SQLite file (watchlist.db) that stores:
- registered users (login credentials, email, and their own Alpaca keys)
- which tickers each user is watching, and which strategies are toggled
  on/off per ticker
- a log of alerts that have already fired, so a user doesn't get emailed
  twice for the same trigger on the same day
- a log of autotrade trades placed on each user's behalf
"""

import hashlib
import secrets
import sqlite3
from datetime import date

from signals import ALL_STRATEGIES
import config

DB_FILE = config.DB_PATH


def get_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _column_names(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def init_db():
    conn = get_connection()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            first_name TEXT NOT NULL DEFAULT '',
            last_name TEXT NOT NULL DEFAULT '',
            phone TEXT NOT NULL DEFAULT '',
            street TEXT NOT NULL DEFAULT '',
            city TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT '',
            zip_code TEXT NOT NULL DEFAULT '',
            alpaca_api_key TEXT NOT NULL DEFAULT '',
            alpaca_secret_key TEXT NOT NULL DEFAULT '',
            autotrade_enabled INTEGER NOT NULL DEFAULT 0,
            is_admin INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """)
    user_cols = _column_names(conn, "users")
    for column in ("first_name", "last_name", "phone", "street", "city", "state", "zip_code"):
        if column not in user_cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
    if "is_admin" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
    # full_name/address were a short-lived earlier shape (this app's only
    # release so far) that never held real data — replaced by the columns
    # above, so just drop them rather than carrying dead columns forward.
    for column in ("full_name", "address"):
        if column in user_cols:
            conn.execute(f"ALTER TABLE users DROP COLUMN {column}")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS watchlist_strategies (
            user_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            strategy TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, ticker, strategy)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alert_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            strategy TEXT NOT NULL,
            alert_date TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trade_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            qty REAL NOT NULL,
            reason TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS autotrade_watchlist (
            user_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            entry_strategy TEXT NOT NULL,
            exit_strategy TEXT NOT NULL,
            PRIMARY KEY (user_id, ticker)
        )
    """)
    conn.commit()

    _migrate_to_multi_user(conn)
    _seed_legacy_autotrade_watchlist(conn)
    _promote_legacy_admin(conn)

    conn.close()


def _migrate_to_multi_user(conn):
    """One-time migration: older versions of this app had a single global
    watchlist/alert/trade log with no user_id column. If we find that old
    shape, create an account from the legacy admin credentials in config.py
    and re-home the existing rows under it, so nothing gets lost when
    multi-user support is added."""
    watchlist_cols = _column_names(conn, "watchlist_strategies")
    if "user_id" in watchlist_cols:
        return  # already migrated (or a fresh db that was created with the new schema)

    legacy_user_id = _get_or_create_legacy_user(conn)

    conn.execute("ALTER TABLE watchlist_strategies RENAME TO watchlist_strategies_old")
    conn.execute("""
        CREATE TABLE watchlist_strategies (
            user_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            strategy TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, ticker, strategy)
        )
    """)
    conn.execute(
        "INSERT INTO watchlist_strategies (user_id, ticker, strategy, enabled) "
        "SELECT ?, ticker, strategy, enabled FROM watchlist_strategies_old",
        (legacy_user_id,),
    )
    conn.execute("DROP TABLE watchlist_strategies_old")

    for table in ("alert_log", "trade_log"):
        if "user_id" not in _column_names(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER")
            conn.execute(f"UPDATE {table} SET user_id = ? WHERE user_id IS NULL", (legacy_user_id,))

    conn.commit()


def _seed_legacy_autotrade_watchlist(conn):
    """One-time migration, independent of _migrate_to_multi_user (which may
    have already run in an earlier release before AutoTrade watchlists
    existed, and would otherwise never fire this again). AutoTrade used to
    run against one hardcoded ticker list with a fixed RSI-rotation
    strategy for every user. Seed that same list into the legacy admin
    account's new per-user AutoTrade watchlist so their bot keeps doing
    what it was already doing — they can edit or remove any of it
    afterward from the AutoTrade page."""
    marker = conn.execute(
        "SELECT 1 FROM settings WHERE key = 'autotrade_watchlist_seeded'"
    ).fetchone()
    if marker:
        return

    row = conn.execute("SELECT id FROM users WHERE username = ?", (config.APP_USERNAME,)).fetchone()
    if row:
        legacy_user_id = row["id"]
        legacy_autotrade_tickers = ["PLTR", "IREN", "NVDA", "NBIS", "MSTR", "SOFI", "GOOGL", "AMZN", "MSFT", "AMD"]
        for ticker in legacy_autotrade_tickers:
            conn.execute(
                "INSERT OR IGNORE INTO autotrade_watchlist (user_id, ticker, entry_strategy, exit_strategy) "
                "VALUES (?, ?, 'RSI_Oversold', 'RSI_Overbought')",
                (legacy_user_id, ticker),
            )

    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('autotrade_watchlist_seeded', 'true') "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
    )
    conn.commit()


def _promote_legacy_admin(conn):
    """One-time migration: grant admin (is_admin=1) to the very first
    account ever created (MIN(id)) -- the actual owner/operator of this
    deployment. Deliberately NOT keyed on config.APP_USERNAME: that env
    var only matters during the original single-user-to-multi-user
    migration and is correctly left unset afterward (see README) --
    which meant on a deploy where it was never set, this matched
    nobody (falling back to the literal string "admin") and silently
    promoted no one. MIN(id) is invariant instead: the first row is
    always the legacy/original account, regardless of any env var.

    Uses a new marker key (not the original 'legacy_admin_promoted')
    since that one already got marked "done" by the broken version of
    this migration despite updating zero rows -- reusing it would mean
    this fix never actually runs on a database that already saw that
    bug."""
    marker = conn.execute("SELECT 1 FROM settings WHERE key = 'legacy_admin_promoted_v2'").fetchone()
    if marker:
        return

    conn.execute("UPDATE users SET is_admin = 1 WHERE id = (SELECT MIN(id) FROM users)")
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('legacy_admin_promoted_v2', 'true') "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
    )
    conn.commit()


def _get_or_create_legacy_user(conn):
    username = config.APP_USERNAME
    row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if row:
        return row["id"]

    autotrade_enabled_row = conn.execute(
        "SELECT value FROM settings WHERE key = 'autotrade_enabled'"
    ).fetchone()
    autotrade_enabled = 1 if (autotrade_enabled_row and autotrade_enabled_row["value"] == "true") else 0

    cur = conn.execute(
        "INSERT INTO users (username, email, password_hash, alpaca_api_key, alpaca_secret_key, autotrade_enabled) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            username,
            config.EMAIL_TO,
            hash_password(config.APP_PASSWORD),
            config.ALPACA_API_KEY,
            config.ALPACA_SECRET_KEY,
            autotrade_enabled,
        ),
    )
    return cur.lastrowid


# ============ Password hashing (stdlib PBKDF2, no extra dependency) ============

def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 200_000)
    return f"{salt}${digest.hex()}"


def verify_password(password, password_hash):
    try:
        salt, hex_digest = password_hash.split("$", 1)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 200_000)
    return secrets.compare_digest(digest.hex(), hex_digest)


# ============ Users ============

def create_user(username, email, password, first_name="", last_name="", phone="",
                 street="", city="", state="", zip_code=""):
    """Returns the new user's id, or raises sqlite3.IntegrityError if the
    username is already taken."""
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO users (username, email, password_hash, first_name, last_name, phone, "
        "street, city, state, zip_code) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (username, email, hash_password(password), first_name, last_name, phone,
         street, city, state, zip_code),
    )
    conn.commit()
    user_id = cur.lastrowid
    conn.close()
    return user_id


def get_user_by_username(username):
    conn = get_connection()
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_by_id(user_id):
    conn = get_connection()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_users():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM users").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def set_alpaca_keys(user_id, api_key, secret_key):
    conn = get_connection()
    conn.execute(
        "UPDATE users SET alpaca_api_key = ?, alpaca_secret_key = ? WHERE id = ?",
        (api_key, secret_key, user_id),
    )
    conn.commit()
    conn.close()


def set_autotrade_enabled(user_id, enabled):
    conn = get_connection()
    conn.execute(
        "UPDATE users SET autotrade_enabled = ? WHERE id = ?",
        (1 if enabled else 0, user_id),
    )
    conn.commit()
    conn.close()


def update_profile(user_id, first_name, last_name, email, phone, street, city, state, zip_code):
    conn = get_connection()
    conn.execute(
        "UPDATE users SET first_name = ?, last_name = ?, email = ?, phone = ?, "
        "street = ?, city = ?, state = ?, zip_code = ? WHERE id = ?",
        (first_name, last_name, email, phone, street, city, state, zip_code, user_id),
    )
    conn.commit()
    conn.close()


def set_password(user_id, new_password):
    conn = get_connection()
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (hash_password(new_password), user_id),
    )
    conn.commit()
    conn.close()


# ============ Watchlist ============

def ensure_ticker_rows(user_id, ticker):
    """Adds a row for every known strategy for this ticker (default: off) if not already present."""
    conn = get_connection()
    for strategy in ALL_STRATEGIES:
        conn.execute(
            "INSERT OR IGNORE INTO watchlist_strategies (user_id, ticker, strategy, enabled) VALUES (?, ?, ?, 0)",
            (user_id, ticker.upper(), strategy),
        )
    conn.commit()
    conn.close()


def remove_ticker(user_id, ticker):
    conn = get_connection()
    conn.execute(
        "DELETE FROM watchlist_strategies WHERE user_id = ? AND ticker = ?",
        (user_id, ticker.upper()),
    )
    conn.commit()
    conn.close()


def toggle_strategy(user_id, ticker, strategy, enabled):
    conn = get_connection()
    conn.execute(
        "UPDATE watchlist_strategies SET enabled = ? WHERE user_id = ? AND ticker = ? AND strategy = ?",
        (1 if enabled else 0, user_id, ticker.upper(), strategy),
    )
    conn.commit()
    conn.close()


def get_watchlist(user_id):
    """Returns {ticker: {strategy: bool, ...}, ...} for this user."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM watchlist_strategies WHERE user_id = ? ORDER BY ticker", (user_id,)
    ).fetchall()
    conn.close()

    watchlist = {}
    for row in rows:
        watchlist.setdefault(row["ticker"], {})[row["strategy"]] = bool(row["enabled"])
    return watchlist


def get_enabled_strategies(user_id, ticker):
    conn = get_connection()
    rows = conn.execute(
        "SELECT strategy FROM watchlist_strategies WHERE user_id = ? AND ticker = ? AND enabled = 1",
        (user_id, ticker.upper()),
    ).fetchall()
    conn.close()
    return [row["strategy"] for row in rows]


def get_all_watched_tickers():
    """Returns the distinct set of tickers watched by anyone, across all users."""
    conn = get_connection()
    rows = conn.execute("SELECT DISTINCT ticker FROM watchlist_strategies").fetchall()
    conn.close()
    return [row["ticker"] for row in rows]


# ============ AutoTrade watchlist ============
# A separate ticker list from the email-alert watchlist above, so enabling
# a strategy for alerts never silently turns into a real (paper or live)
# trade. Each ticker has exactly one entry strategy and one exit strategy.

def get_autotrade_watchlist(user_id):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM autotrade_watchlist WHERE user_id = ? ORDER BY ticker", (user_id,)
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def upsert_autotrade_ticker(user_id, ticker, entry_strategy, exit_strategy):
    conn = get_connection()
    conn.execute(
        "INSERT INTO autotrade_watchlist (user_id, ticker, entry_strategy, exit_strategy) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id, ticker) DO UPDATE SET "
        "entry_strategy = excluded.entry_strategy, exit_strategy = excluded.exit_strategy",
        (user_id, ticker.upper(), entry_strategy, exit_strategy),
    )
    conn.commit()
    conn.close()


def remove_autotrade_ticker(user_id, ticker):
    conn = get_connection()
    conn.execute(
        "DELETE FROM autotrade_watchlist WHERE user_id = ? AND ticker = ?",
        (user_id, ticker.upper()),
    )
    conn.commit()
    conn.close()


# ============ Alerts ============

def already_alerted_today(user_id, ticker, strategy):
    today = date.today().isoformat()
    conn = get_connection()
    row = conn.execute(
        "SELECT 1 FROM alert_log WHERE user_id = ? AND ticker = ? AND strategy = ? AND alert_date = ?",
        (user_id, ticker, strategy, today),
    ).fetchone()
    conn.close()
    return row is not None


def record_alert(user_id, ticker, strategy, message):
    today = date.today().isoformat()
    conn = get_connection()
    conn.execute(
        "INSERT INTO alert_log (user_id, ticker, strategy, alert_date, message, created_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime'))",
        (user_id, ticker, strategy, today, message),
    )
    conn.commit()
    conn.close()


def get_recent_alerts(user_id, limit=50):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM alert_log WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


# ============ Trades ============

def record_trade(user_id, ticker, side, qty, reason=""):
    conn = get_connection()
    conn.execute(
        "INSERT INTO trade_log (user_id, ticker, side, qty, reason, created_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'))",
        (user_id, ticker, side, qty, reason),
    )
    conn.commit()
    conn.close()


def get_recent_trades(user_id, limit=50):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM trade_log WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


# ============ Settings (small global key/value store) ============

def get_setting(key, default=None):
    conn = get_connection()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = get_connection()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()
