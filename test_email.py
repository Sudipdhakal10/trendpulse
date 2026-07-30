"""
Sends one test email using the same credentials from your .env file,
so you can confirm the email setup works without waiting for a real
strategy trigger.

Run with:
    python3 test_email.py
"""

import smtplib
from email.mime.text import MIMEText

import config

print(f"Sending test email from {config.EMAIL_ADDRESS} to {config.EMAIL_TO}...")

try:
    msg = MIMEText("This is a test email from your Stock Watchlist app. If you're reading this, your email setup works.")
    msg["Subject"] = "Stock Watchlist — Test Email"
    msg["From"] = config.EMAIL_ADDRESS
    msg["To"] = config.EMAIL_TO

    with smtplib.SMTP(config.SMTP_SERVER, config.SMTP_PORT) as server:
        server.starttls()
        server.login(config.EMAIL_ADDRESS, config.EMAIL_APP_PASSWORD)
        server.send_message(msg)

    print("Success! Check your inbox (and spam folder, just in case).")
except Exception as e:
    print(f"Failed to send: {e}")