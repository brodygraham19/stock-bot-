Discord Stock Bot - Railway Deployment Guide

What it does:
   Price updates every minute
   Volume spike alerts (color‑coded /), plus EMA9/EMA21 and VWAP
   News headlines (de‑duplicated)
   Polygon Options flow snapshot (auto‑enables when POLYGON_API_KEY is set)

Deploy on Railway (free):
1) Go to https://railway.app/ and log in.
2) Create a New Project → either "Deploy from GitHub" or create empty and use the "Code" tab.
3) Upload these files: bot.py, requirements.txt, Procfile, README.txt  (all at repo root, not inside a subfolder).
4) In Variables, paste your real keys:
   
DISCORD_BOT_TOKEN
ALPHAVANTAGE_API_KEY,
DISCORD_CHANNEL_ID  (e.g., 1429934034095706203),
POLYGON_API_KEY     (optional),
TICKERS             (optional, e.g. AAPL,MSFT,SPY,TSLA,QQQ),
PRICE_LOOP_SECONDS  (optional, default 60),
NEWS_LOOP_SECONDS   (optional, default 300),
,
5) In Settings → Start Command is already handled by Procfile (no changes needed).
6) Click Deploy, then open Logs. When you see “Logged in as …” the bot is live 24/7.

Notes:
Alpha Vantage has rate‑limits. If data is missing briefly, the loop continues automatically.,
To change tickers, set env var:  TICKERS=QQQ,AAPL,TSLA,
To reduce message frequency, increase PRICE_LOOP_SECONDS and NEWS_LOOP_SECONDS.,

Troubleshooting:
Build failed / "Error creating build plan with Railpack": Files are likely inside a subfolder. Ensure bot.py, requirements.txt, Procfile are at the top level.,
Bot not posting: Confirm DISCORD_CHANNEL_ID is the target text channel and the bot has permission to post there.,
Options flow shows nothing: It only posts if Polygon returns usable data and the POLYGON_API_KEY is set.
