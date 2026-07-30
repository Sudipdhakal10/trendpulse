# Stock Watchlist App

A local web app: add tickers, toggle which strategies you want watched for
each one, and get emailed automatically once a day when a strategy triggers.

## Setup

1. Install dependencies:
   ```
   python3 -m pip install -r requirements.txt
   ```

2. Open `config.py` and fill in your email settings:
   - Turn on 2-factor authentication on the Gmail account you want to send from
   - Create an App Password at https://myaccount.google.com/apppasswords
   - Put that (not your normal Gmail password) into `EMAIL_APP_PASSWORD`

3. Run the app:
   ```
   python3 app.py
   ```

4. Open your browser to **http://localhost:8000**

## How to use it

- Type a ticker (e.g. `AAPL`) and click **Add**
- Check the boxes for whichever strategies you want watched for that stock
- Click **Check Now** any time to run an immediate check (useful for testing
  your email setup without waiting for the daily schedule)
- The app also checks automatically once a day at the time set in
  `config.py` (`DAILY_CHECK_TIME`, default 4:30pm) — leave the app running
  in a terminal window for this to work
- The **Recent Alerts** panel shows a history of everything that's triggered

## Strategies available

Same seven from your backtesting: `MA50_Cross`, `RSI_Oversold`,
`MACD_Bullish_Cross`, `Breakout_20D`, `Pullback_to_MA50`, `Combo_Trend`,
`Combo_MeanReversion`. Based on your backtest results, `MA50_Cross` and
`Combo_Trend`/`MACD_Bullish_Cross` looked strongest across market conditions
— those are good starting points to enable.

## Notes and limitations (running locally)

- This runs on your machine, not the internet — it only checks and alerts
  while `python3 app.py` is actively running in a terminal.
- Closing the terminal or shutting your laptop stops the checks.
- All your watchlist and alert history live in `watchlist.db`, a file that
  gets created automatically the first time you run the app.

## Deploying to the cloud (so it runs even when your laptop is off)

This uses Railway (railway.com) — a hosting platform that runs your app
24/7 for a few dollars a month, without you needing to manage a server.

1. **Put the code on GitHub** (Railway deploys from a GitHub repo):
   - Create a free GitHub account if you don't have one
   - Create a new repository (e.g. `stock-watchlist-app`)
   - In your project folder, run:
     ```
     git init
     git add .
     git commit -m "Initial commit"
     git branch -M main
     git remote add origin https://github.com/YOUR_USERNAME/stock-watchlist-app.git
     git push -u origin main
     ```
   - The `.gitignore` file makes sure your database and any local secrets
     don't get uploaded.

2. **Create a Railway account** at railway.com and start a new project,
   choosing "Deploy from GitHub repo" — select the repo you just pushed.

3. **Add a persistent volume** (so your watchlist survives redeploys):
   - In your Railway project, go to Settings → Volumes → Add Volume
   - Mount it at `/data`
   - Add an environment variable `DB_PATH` = `/data/watchlist.db`

4. **Add your email credentials as environment variables** (Settings → Variables):
   - `EMAIL_ADDRESS` = your Gmail address
   - `EMAIL_APP_PASSWORD` = your Gmail App Password
   - `EMAIL_TO` = where you want alerts sent
   - `DAILY_CHECK_TIME` = `16:30` (adjust for your timezone relative to 4pm ET market close)

5. **Deploy.** Railway will detect the `Procfile` and start the app
   automatically. Once it's live, Railway gives you a public URL like
   `https://your-app.up.railway.app` — bookmark that on your phone and
   laptop instead of `localhost:8000`.

6. **Test it**: open the public URL, add a ticker, and click **Check Now**
   to confirm the email still sends correctly from the cloud.

Cost is typically $5/month or less for an app this lightweight (Railway
bills by actual usage on the Hobby plan). Once deployed, you can shut your
laptop completely — the daily check and alerts keep running independently.
