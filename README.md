# TrendPulse

A multi-user web app: anyone can register their own account, build a
watchlist of tickers with rule-based strategies, get emailed when one
triggers, and run an AutoTrade bot against their own Alpaca account using
their own entry/exit strategy per ticker.

## Local setup

1. Install dependencies:
   ```
   python3 -m pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and fill in your real values:
   - **Email (for alerts)** — turn on 2-factor auth on the Gmail account
     you want to send from, create an App Password at
     https://myaccount.google.com/apppasswords, and put that (not your
     normal Gmail password) into `EMAIL_APP_PASSWORD`.
   - **Alpaca market-data keys** (`ALPACA_API_KEY`/`ALPACA_SECRET_KEY`) —
     shared by every user for the Watchlist page's live price ticker only.
     Free from any Alpaca account, paper or live. This is separate from
     AutoTrade, which uses each user's *own* keys (see below).
   - **`SESSION_SECRET_KEY`** — any long random string. Generate one with
     `python3 -c "import secrets; print(secrets.token_hex(32))"`.
   - Everything else has a reasonable default — see `.env.example` for
     the full list.

3. Run the app:
   ```
   python3 app.py
   ```

4. Open **http://localhost:8000**, click **Log In → Create an account**,
   and register. Your watchlist, AutoTrade setup, and alerts all belong
   to that account from then on.

## How to use it

- **Watchlist** (`/watchlist`) — add tickers, toggle which strategies you
  want watched per ticker. Checks run automatically once a day (time set
  by `DAILY_CHECK_TIME`) and email you at your registered address when
  one triggers. **Check Now** runs an immediate check for testing.
- **AutoTrade** (`/autotrade`) — a separate ticker list from the one
  above, so an alert-only strategy never silently becomes a real trade.
  Add your own Alpaca API key/secret (from your own Alpaca account, paper
  or live), then per ticker pick one entry strategy and one exit
  strategy. Runs automatically once a day (`AUTOTRADE_RUN_TIME`), or
  **Run Now** to test. Defaults to paper trading — see the safety note
  below before ever changing that.
- **Screener** (`/screener`) — search any publicly traded stock (not just your
  watchlist) by ticker or company name. Shows a price chart (1D through
  5Y), key stats (market cap, volume, 52-week range, PE ratio, EPS),
  recent earnings, quarterly revenue/net income, and company news, with
  a one-click **+ Add to Watchlist**. Data comes from yfinance, cached
  per ticker for a few minutes so browsing doesn't hammer Yahoo Finance.
- **Profile** (`/profile`) — name, email, phone, address, and password.

## Strategies available

**Entry** (also what the Watchlist page's alerts use): `MA50_Cross`,
`RSI_Oversold`, `MACD_Bullish_Cross`, `Breakout_20D`, `Pullback_to_MA50`,
`Combo_Trend`, `Combo_MeanReversion`.

**Exit** (AutoTrade only — none of the entry strategies above describe a
sell condition, so these are separate): `RSI_Overbought`,
`MA50_Breakdown`, `MACD_Bearish_Cross`, `Take_Profit_Stop_Loss` (+15%/-8%
from entry price).

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `EMAIL_ADDRESS` / `EMAIL_APP_PASSWORD` | Gmail account alerts are sent *from* | — |
| `EMAIL_TO` | Fallback recipient (legacy; real users get alerts at their own registered email) | `EMAIL_ADDRESS` |
| `SMTP_SERVER` / `SMTP_PORT` | SMTP settings | `smtp.gmail.com` / `587` |
| `DAILY_CHECK_TIME` | When the daily Watchlist check runs (`HH:MM`) | `16:30` |
| `AUTOTRADE_RUN_TIME` | When the daily AutoTrade run happens (`HH:MM`) | `09:35` |
| `DB_BACKUP_TIME` | When the daily database backup email goes out (`HH:MM`) | `03:15` |
| `DB_PATH` | Where the SQLite file lives | `watchlist.db` |
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | Shared market-data keys (Watchlist live prices only) | — |
| `PAPER_TRADING` | Must be explicitly `false` to trade real money | `true` |
| `APP_USERNAME` / `APP_PASSWORD` | One-time seed for an admin account when upgrading a pre-multi-user database — irrelevant on a fresh install | `admin` / `changeme` |
| `SESSION_SECRET_KEY` | Signs session cookies — any long random string | *(change this)* |
| `SESSION_COOKIE_SECURE` | Set `true` once deployed behind HTTPS | `false` |

## Notes and limitations (running locally)

- This only checks and alerts while `python3 app.py` is actively running
  in a terminal — closing the terminal or shutting your laptop stops it
  for everyone using it, not just you.
- All accounts, watchlists, AutoTrade config, and alert history live in
  `watchlist.db`, created automatically on first run.

## Deploying to the cloud (so it runs — and is reachable — even when your laptop is off)

This uses Railway (railway.com) — a hosting platform that runs your app
24/7 for a few dollars a month, without you needing to manage a server.

1. **Put the code on GitHub** (Railway deploys from a GitHub repo). This
   folder is already a git repo with an initial commit. Create an empty
   repo on GitHub (no README/license — you already have one), then:
   ```
   git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
   git branch -M main
   git push -u origin main
   ```
   `.gitignore` already keeps your database, backups, and `.env` out of
   version control.

2. **Create a Railway account** at railway.com and start a new project,
   choosing "Deploy from GitHub repo" — select the repo you just pushed.

3. **Add a persistent volume** (so accounts and watchlists survive
   redeploys):
   - Settings → Volumes → Add Volume, mount at `/data`
   - Add an environment variable `DB_PATH` = `/data/watchlist.db`

4. **Add every environment variable from the table above** under
   Settings → Variables (at minimum: email settings, a fresh
   `SESSION_SECRET_KEY`, and `SESSION_COOKIE_SECURE=true` since Railway
   serves over HTTPS).

5. **Deploy.** Railway detects the `Procfile` and starts the app
   automatically, giving you a public URL like
   `https://your-app.up.railway.app`.

6. **Test it**: open the public URL, register an account, add a ticker,
   and click **Check Now** to confirm email still sends from the cloud.

### Carrying over your existing local accounts and data

A brand-new deploy starts with an empty database — your local accounts,
watchlists, and AutoTrade setup won't just appear on their own.

This app used to have a one-time `/admin/import-db` route for exactly
this, protected by a `DB_IMPORT_TOKEN` env var. It's already served its
purpose (the original deploy's data was migrated with it) and has since
been removed from the code and from Railway's variables — it's not
there anymore, on purpose. If you ever need to do this again from
scratch on a new environment:

1. Temporarily re-add a route like it (accept a raw file upload behind
   a secret token header, write the bytes to `config.DB_PATH`), guarded
   so it does nothing unless that token env var is set.
2. Set the token only in Railway's dashboard (never in `.env`), deploy,
   `curl --data-binary @watchlist.db` the route with the token header,
   confirm you can log in with an existing account on the live URL.
3. Remove the token from Railway's variables and delete the route again
   — treat it as strictly one-time, not a permanent feature.

### Before this is genuinely public

- **Registration is open to anyone with the URL** — anyone can create an
  account today; there's no invite/approval gate. `/api/login` and
  `/api/register` are rate-limited per IP (10 login attempts / 5 min,
  5 registrations / hour) against brute-force and spam signups, but
  decide if fully open registration is what you want before sharing
  the link widely.
- **`PAPER_TRADING` stays `true`** until you deliberately flip it — do
  not change this until you're fully ready, and even then, start small.
- **Backups**: Railway's own volume backups need a paid Pro plan. In
  the meantime, a full copy of the database is emailed to `EMAIL_TO`
  daily at `DB_BACKUP_TIME` — check that it's actually arriving, since
  it's silent-fail by design (a missed backup shouldn't crash the app).
  Worth revisiting if the database ever grows large enough that an
  email attachment stops being practical.
- Cost is typically $5/month or less for an app this lightweight
  (Railway's Hobby plan bills by actual usage).
