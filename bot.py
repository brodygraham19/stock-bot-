# discord-stock-bot (Railway-ready)
# Features:
# - Price updates every minute
# - Volume spike alerts (🟢 up / 🔴 down), with EMA9/EMA21 and VWAP
# - News headlines (de-duplicated)
# - Polygon options flow snapshot (auto-enables when POLYGON_API_KEY is set)
#
# Env Vars (set in Railway → Variables):
#   DISCORD_BOT_TOKEN        (required)
#   DISCORD_CHANNEL_ID       (required) e.g. 1429934034095706203
#   ALPHAVANTAGE_API_KEY     (required)
#   POLYGON_API_KEY          (optional; enables options flow snapshot)
#   TICKERS                  (optional; default "AAPL,MSFT,SPY,TSLA,QQQ")
#   PRICE_LOOP_SECONDS       (optional; default 60)
#   NEWS_LOOP_SECONDS        (optional; default 300)
#
# Start command (Procfile):
#   worker: python bot.py

import os
import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Tuple

import aiohttp
import discord
from discord.ext import tasks

# --------------------------------------------------
# Configuration
# --------------------------------------------------
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")
ALPHA_KEY = os.getenv("ALPHAVANTAGE_API_KEY")
POLYGON_KEY = os.getenv("POLYGON_API_KEY", "").strip() or None

TICKERS = [t.strip().upper() for t in os.getenv("TICKERS", "AAPL,MSFT,SPY,TSLA,QQQ").split(",") if t.strip()]
PRICE_LOOP_SECONDS = int(os.getenv("PRICE_LOOP_SECONDS", "60"))
NEWS_LOOP_SECONDS = int(os.getenv("NEWS_LOOP_SECONDS", "300"))

if not DISCORD_TOKEN:
    raise SystemExit("Missing DISCORD_BOT_TOKEN")
if not DISCORD_CHANNEL_ID:
    raise SystemExit("Missing DISCORD_CHANNEL_ID")
if not ALPHA_KEY:
    raise SystemExit("Missing ALPHAVANTAGE_API_KEY")

CHANNEL_ID_INT = int(DISCORD_CHANNEL_ID)

intents = discord.Intents.default()
client = discord.Client(intents=intents)

# Track what we've posted (dedupe)
posted_news_ids: set = set()

# --------------------------------------------------
# Helpers
# --------------------------------------------------
def ema(series: List[float], period: int) -> Optional[float]:
    if len(series) < period:
        return None
    k = 2 / (period + 1)
    e = series[0]
    for x in series[1:]:
        e = x * k + e * (1 - k)
    return e

def calc_vwap(prices: List[Tuple[float,float,float]], volumes: List[int]) -> Optional[float]:
    # prices list of tuples: (high, low, close) 1-min bars
    if not prices or not volumes or len(prices) != len(volumes):
        return None
    cum_pv = 0.0
    cum_v = 0
    for (h,l,c), v in zip(prices, volumes):
        typical = (h + l + c) / 3.0
        cum_pv += typical * v
        cum_v += v
    if cum_v == 0:
        return None
    return cum_pv / cum_v

async def fetch_json(session: aiohttp.ClientSession, url: str, params: Dict[str, Any], headers: Optional[Dict[str,str]]=None) -> Optional[Dict[str, Any]]:
    try:
        async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status != 200:
                logging.warning("HTTP %s for %s", r.status, url)
                return None
            return await r.json()
    except Exception as e:
        logging.exception("fetch_json error: %s", e)
        return None

# --------------------------------------------------
# Alpha Vantage: intraday + news
# --------------------------------------------------
async def get_intraday(session: aiohttp.ClientSession, symbol: str) -> Optional[Dict[str, Any]]:
    params = {
        "function": "TIME_SERIES_INTRADAY",
        "symbol": symbol,
        "interval": "1min",
        "outputsize": "compact",
        "apikey": ALPHA_KEY,
    }
    return await fetch_json(session, "https://www.alphavantage.co/query", params)

def parse_intraday(series_json: Dict[str, Any]) -> Optional[Tuple[List[float], List[Tuple[float,float,float]], List[int], float]]:
    # returns (closes_desc, hlc_desc, volumes_desc, last_close)
    try:
        ts = series_json.get("Time Series (1min)") or {}
        if not ts:
            return None
        # Alpha returns newest first when sorted descending by timestamp
        items = sorted(ts.items(), key=lambda kv: kv[0])  # oldest → newest
        closes = []
        hlc = []
        vols = []
        for _, v in items:
            close = float(v["4. close"])
            high = float(v["2. high"])
            low  = float(v["3. low"])
            vol  = int(float(v["5. volume"]))
            closes.append(close)
            hlc.append((high, low, close))
            vols.append(vol)
        return closes, hlc, vols, closes[-1]
    except Exception as e:
        logging.exception("parse_intraday error: %s", e)
        return None

def volume_spike(vols: List[int]) -> Optional[Tuple[float, int]]:
    if len(vols) < 25:
        return None
    baseline = sum(vols[-25:-5]) / 20.0  # last ~20 bars excluding latest 5 bars
    current = vols[-1]
    if baseline <= 0:
        return None
    ratio = current / baseline
    if ratio >= 2.0:  # 2x spike
        return (ratio, current)
    return None

async def get_news(session: aiohttp.ClientSession, symbols: List[str]) -> List[Dict[str, Any]]:
    # Alpha Vantage News & Sentiment
    params = {
        "function": "NEWS_SENTIMENT",
        "tickers": ",".join(symbols[:10]),
        "apikey": ALPHA_KEY,
        "limit": 30,
        "sort": "LATEST"
    }
    data = await fetch_json(session, "https://www.alphavantage.co/query", params)
    feed = data.get("feed", []) if data else []
    # Normalize
    news = []
    for item in feed:
        uid = item.get("uuid") or item.get("url")
        if not uid or uid in posted_news_ids:
            continue
        headline = item.get("title") or "News"
        url = item.get("url")
        tickers = [t.get("ticker") for t in item.get("ticker_sentiment", []) if t.get("ticker")]
        news.append({"id": uid, "headline": headline, "url": url, "tickers": tickers})
    return news[:10]

# --------------------------------------------------
# Polygon: simple options flow snapshot (best-effort)
# --------------------------------------------------
async def polygon_options_flow(session: aiohttp.ClientSession, symbol: str) -> Optional[Dict[str, Any]]:
    if not POLYGON_KEY:
        return None
    # Best-effort: try the aggregated options trades snapshot endpoint.
    # If Polygon returns error or endpoint differs, we fail gracefully.
    url = f"https://api.polygon.io/v3/snapshot/options/{symbol}"
    params = {"apiKey": POLYGON_KEY}
    data = await fetch_json(session, url, params)
    if not data:
        return None
    # Attempt to summarize calls vs puts volume if present
    try:
        results = data.get("results") or []
        calls_vol = sum(int(x.get("day", {}).get("volume", 0)) for x in results if x.get("details", {}).get("contract_type") == "call")
        puts_vol  = sum(int(x.get("day", {}).get("volume", 0)) for x in results if x.get("details", {}).get("contract_type") == "put")
        if calls_vol == 0 and puts_vol == 0:
            return None
        return {"calls": calls_vol, "puts": puts_vol}
    except Exception:
        return None

# --------------------------------------------------
# Discord tasks
# --------------------------------------------------
async def ensure_channel() -> discord.TextChannel:
    await client.wait_until_ready()
    ch = client.get_channel(CHANNEL_ID_INT)
    if ch is None:
        ch = await client.fetch_channel(CHANNEL_ID_INT)
    if not isinstance(ch, discord.TextChannel):
        raise RuntimeError("DISCORD_CHANNEL_ID is not a text channel.")
    return ch

@client.event
async def on_ready():
    logging.info("Logged in as %s", client.user)
    if not price_loop.is_running():
        price_loop.start()
    if not news_loop.is_running():
        news_loop.start()

@tasks.loop(seconds=PRICE_LOOP_SECONDS)
async def price_loop():
    ch = await ensure_channel()
    async with aiohttp.ClientSession() as session:
        for sym in TICKERS:
            series = await get_intraday(session, sym)
            if not series:
                continue
            parsed = parse_intraday(series)
            if not parsed:
                continue
            closes, hlc, vols, last = parsed
            change = last - closes[-2] if len(closes) > 1 else 0.0
            up = change >= 0
            dot = "🟢" if up else "🔴"

            ema9 = ema(closes[-50:], 9)
            ema21 = ema(closes[-50:], 21)
            vwap_val = calc_vwap(hlc[-120:], vols[-120:])

            spike_txt = ""
            spike = volume_spike(vols)
            if spike:
                ratio, curv = spike
                spike_txt = f" • Vol spike {ratio:.1f}×"

            ema_txt = ""
            if ema9 and ema21:
                trend = "↑" if ema9 > ema21 else "↓"
                ema_txt = f" • EMA9/21: {ema9:.2f}/{ema21:.2f} {trend}"

            vwap_txt = f" • VWAP: {vwap_val:.2f}" if vwap_val else ""

            poly_txt = ""
            if POLYGON_KEY:
                snap = await polygon_options_flow(session, sym)
                if snap:
                    poly_txt = f" • Opts flow C/P: {snap['calls']}/{snap['puts']}"

            msg = f"{dot} **{sym}** {last:.2f} ({change:+.2f}){spike_txt}{ema_txt}{vwap_txt}{poly_txt}"
            try:
                await ch.send(msg)
            except Exception as e:
                logging.warning("send price msg failed: %s", e)
            await asyncio.sleep(1.2)  # small spacing; also helps with rate limits

@tasks.loop(seconds=NEWS_LOOP_SECONDS)
async def news_loop():
    ch = await ensure_channel()
    async with aiohttp.ClientSession() as session:
        news = await get_news(session, TICKERS)
        if not news:
            return
        for n in news:
            posted_news_ids.add(n["id"])
            tickers_txt = ", ".join(n["tickers"]) if n["tickers"] else ""
            title = n["headline"]
            url = n["url"] or ""
            embed = discord.Embed(title=title, description=tickers_txt, timestamp=datetime.now(timezone.utc))
            if url:
                embed.url = url
            embed.set_footer(text="Alpha Vantage News")
            try:
                await ch.send(embed=embed)
            except Exception as e:
                logging.warning("send news failed: %s", e)
            await asyncio.sleep(1.0)

# --------------------------------------------------
# Run
# --------------------------------------------------
def main():
    client.run(DISCORD_TOKEN)

if __name__ == "__main__":
    main()
