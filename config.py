"""
Settings — reads from environment variables so credentials never live in
code (required for deploying to a cloud host safely). For local testing,
it falls back to the placeholder values below if no environment variable
is set — but for actual use, set these as environment variables instead
of editing this file directly, especially before pushing to GitHub.
"""

import os

from dotenv import load_dotenv

load_dotenv()  # reads a .env file in this folder, if one exists

EMAIL_ADDRESS = os.environ.get("EMAIL_ADDRESS", "youraddress@gmail.com")
EMAIL_APP_PASSWORD = os.environ.get("EMAIL_APP_PASSWORD", "your_16_char_app_password")
EMAIL_TO = os.environ.get("EMAIL_TO", EMAIL_ADDRESS)

SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))

# Time of day the daily check runs (24hr "HH:MM"), after market close (4pm ET)
DAILY_CHECK_TIME = os.environ.get("DAILY_CHECK_TIME", "16:30")

# Where the SQLite database file lives. On Railway, set this to a path
# inside your mounted volume (e.g. /data/watchlist.db) so data survives
# redeploys. Defaults to a local file for local testing.
DB_PATH = os.environ.get("DB_PATH", "watchlist.db")

# Alpaca Market Data API — used for near-real-time watchlist prices.
# Get these from your Alpaca account dashboard (either paper or live keys
# work fine, since market data access isn't tied to funding your account).
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")

# AutoTrade bot settings.
# PAPER_TRADING defaults to true — this must be explicitly set to "false"
# to place real trades with real money. Do not flip this until you're
# genuinely ready, and even then, start small.
PAPER_TRADING = os.environ.get("PAPER_TRADING", "true").lower() != "false"

# Time of day the automated daily rotation check runs (24hr "HH:MM"),
# ideally shortly after market open.
AUTOTRADE_RUN_TIME = os.environ.get("AUTOTRADE_RUN_TIME", "09:35")

# Time of day a full database backup gets emailed to EMAIL_TO (24hr
# "HH:MM"). Railway's own volume backups need a paid Pro plan; this is a
# free substitute using the same Gmail credentials already set up above.
DB_BACKUP_TIME = os.environ.get("DB_BACKUP_TIME", "03:15")

# Used only once, the very first time this app runs against a database
# from before multi-user login existed: it seeds one admin account with
# these credentials so nothing gets orphaned by the upgrade. Every other
# login goes through the users table (see /register). Safe to leave as-is
# once that one-time migration has already happened.
APP_USERNAME = os.environ.get("APP_USERNAME", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "changeme")

# Random string used to sign session cookies. Change this to any long
# random string in .env — it doesn't need to be memorable, just unique
# and secret.
SESSION_SECRET_KEY = os.environ.get("SESSION_SECRET_KEY", "please-change-this-secret-key")

# Marks the session cookie Secure (browser will only ever send it over
# HTTPS). Set to "true" once deployed behind HTTPS (Railway gives you this
# automatically) — leave it "false" for local http://localhost testing,
# since browsers drop Secure cookies on a plain-HTTP connection.
SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"