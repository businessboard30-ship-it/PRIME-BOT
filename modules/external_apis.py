# path: modules/external_apis.py

"""
External API Integrations:
- News (using NewsAPI free tier)
- Currency Conversion (using Open Exchange Rates or Free Forex API)
- Stock Charts (using yfinance)
- Media Download (using yt-dlp)
"""

import aiohttp
import asyncio
from typing import Optional, Dict, List
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# NEWS API
# ═══════════════════════════════════════════════════════════════════════════

async def fetch_news(query: str, max_results: int = 5) -> List[Dict]:
    """
    Fetch top news headlines for a topic via Google News' public RSS feed.

    Chosen over NewsAPI/GNews/Bing News because it needs no API key/signup
    (those either require a paid key or a registered free-tier key we don't
    have configured), has no rate limit to manage, and covers any topic —
    general news, not anime-specific, per how /news is used in this bot.
    Tradeoff: it's an unofficial-but-stable public feed rather than a
    contracted API, so there's no formal uptime/rate-limit guarantee — if
    Google ever changes the RSS format this will need a parser update, same
    caveat as the Yandex reverse-image-search scrape in modules/image_search.py.

    Returns list of {"title": str, "url": str, "source": str, "published": str}.
    Empty list on no results OR on any fetch/parse failure — callers already
    treat an empty list as "no results found".
    """
    import xml.etree.ElementTree as ET
    from urllib.parse import quote as _quote

    url = f"https://news.google.com/rss/search?q={_quote(query)}&hl=en-US&gl=US&ceid=US:en"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    logger.warning(f"[v0] Google News RSS request failed: status={resp.status}")
                    return []
                body = await resp.text()

        root = ET.fromstring(body)
        items = root.findall("./channel/item")

        results = []
        for item in items[:max_results]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub_date = (item.findtext("pubDate") or "").strip()
            source_el = item.find("source")
            source = source_el.text.strip() if source_el is not None and source_el.text else ""

            if not title or not link:
                continue

            results.append({
                "title": title,
                "url": link,
                "source": source,
                "published": pub_date,
            })

        return results

    except ET.ParseError as e:
        logger.warning(f"[v0] Google News RSS returned unparseable XML: {e}")
        return []
    except Exception as e:
        logger.error(f"[v0] fetch_news('{query}') error: {e}")
        return []


# ═══════════════════════════════════════════════════════════════════════════
# CURRENCY CONVERSION
# ═══════════════════════════════════════════════════════════════════════════

async def convert_currency(amount: float, from_currency: str, to_currency: str) -> Optional[Dict]:
    """
    Convert currency amount using free exchange rate API (exchangerate-api.com or fixer.io).
    Returns dict with: original_amount, from_currency, to_currency, converted_amount, rate, timestamp
    """
    try:
        async with aiohttp.ClientSession() as session:
            # Using exchangerate-api.com free tier (1500 req/month)
            url = f"https://api.exchangerate-api.com/v4/latest/{from_currency.upper()}"
            
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    to_curr = to_currency.upper()
                    
                    if to_curr in data.get("rates", {}):
                        rate = data["rates"][to_curr]
                        converted = amount * rate
                        
                        return {
                            "original_amount": amount,
                            "from_currency": from_currency.upper(),
                            "to_currency": to_curr,
                            "converted_amount": round(converted, 2),
                            "rate": rate,
                            "timestamp": datetime.now().isoformat()
                        }
        
        return None
    
    except Exception as e:
        logger.error(f"[v0] Error converting currency: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════
# STOCK CHARTS
# ═══════════════════════════════════════════════════════════════════════════

async def get_stock_chart(ticker: str, period: str = "1mo") -> Optional[Dict]:
    """
    Fetch stock price chart data using yfinance.
    Returns dict with: ticker, current_price, 24h_change, data_points (list of {date, price, volume})
    Periods: 1d, 5d, 1mo, 3mo, 6mo, 1y, 2y, 5y, 10y, ytd, max
    """
    try:
        import yfinance as yf
        
        # Validate ticker format
        ticker = ticker.upper().strip()
        if not ticker or len(ticker) > 10:
            return None
        
        # Fetch stock data
        stock = yf.Ticker(ticker)
        hist = stock.history(period=period)
        
        if hist.empty:
            return None
        
        # Extract current price and 24h change
        current_price = hist['Close'].iloc[-1]
        prev_close = hist['Close'].iloc[-2] if len(hist) > 1 else current_price
        change_24h = ((current_price - prev_close) / prev_close * 100) if prev_close > 0 else 0
        
        # Build data points for chart
        data_points = [
            {
                "date": str(idx.date()),
                "price": round(float(row['Close']), 2),
                "volume": int(row['Volume']) if row['Volume'] > 0 else 0
            }
            for idx, row in hist.iterrows()
        ]
        
        return {
            "ticker": ticker,
            "current_price": round(current_price, 2),
            "change_24h_percent": round(change_24h, 2),
            "period": period,
            "data_points": data_points[-30:],  # Last 30 points
            "timestamp": datetime.now().isoformat()
        }
    
    except Exception as e:
        logger.error(f"[v0] Error fetching stock chart for {ticker}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════
# MEDIA DOWNLOAD (YouTube, etc.)
# ═══════════════════════════════════════════════════════════════════════════

MAX_DOWNLOAD_FILE_SIZE = 8 * 1024 * 1024  # 8 MB — Discord's safe unboosted-server upload cap.
# (Previously named TELEGRAM_MAX_FILE_SIZE / 50MB, a leftover from this
# module's Telegram-bot origin. Discord's real cap is much lower — see
# _DISCORD_SAFE_UPLOAD_BYTES in discord_bot/cogs/external_tools.py, which
# this now matches so oversized files are rejected here instead of being
# fully downloaded and only then found to be too big to upload.)


def _run_download(url: str, media_type: str) -> Dict:
    """
    Blocking body of download_media(), run off the event loop via
    asyncio.to_thread. yt_dlp.YoutubeDL.extract_info() is a synchronous,
    potentially slow (many-second) network call — calling it directly inside
    an `async def` without offloading it freezes the whole bot's event loop
    for that duration (missed gateway heartbeats, other commands stalling,
    interactions timing out). That was the previous bug here.
    """
    import yt_dlp
    import os

    download_dir = "/tmp/media_downloads"
    os.makedirs(download_dir, exist_ok=True)

    # Audio: no ffmpeg postprocessing — ask yt-dlp for an already-muxed
    # audio-only stream and send it as-is (m4a preferred, falls back to
    # whatever's available). Video: pre-muxed mp4.
    if media_type == "audio":
        format_spec = "bestaudio[ext=m4a]/bestaudio/best"
    else:
        # YouTube has been phasing out pre-muxed (single-file video+audio)
        # formats for more and more videos — some videos now only offer
        # separate video-only and audio-only streams, on every player
        # client. "best" alone fails on those with "Requested format is
        # not available". bestvideo+bestaudio asks yt-dlp to download both
        # and merge them with ffmpeg (now installed via nixpacks.toml);
        # the plain "best" at the end is a fallback for the still-common
        # case where a muxed format IS available (skips the merge step).
        format_spec = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"

    ydl_opts = {
        'format': format_spec,
        'outtmpl': os.path.join(download_dir, '%(title)s.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': 30,
        # When format_spec picks a "+"-joined pair (separate video/audio),
        # yt-dlp needs ffmpeg to merge them and this tells it what container
        # to merge into. prepare_filename() below reflects this extension
        # automatically post-merge, so the existing filepath/size logic
        # doesn't need to change. No effect when a single muxed format is
        # picked (nothing to merge).
        'merge_output_format': 'mp4',
        # YouTube's default "web" player client frequently returns HTTP 403
        # on the actual media URL even when metadata/preflight succeeds —
        # a known, ongoing signature/player-client mismatch issue, not a
        # cookies problem. Querying multiple player clients pools together
        # each client's available formats, which also helps find a muxed
        # (combined video+audio) format on videos where one client alone
        # doesn't offer one.
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'ios', 'web'],
            }
        },
    }

    # Optional cookies file (see config.YTDLP_COOKIES_FILE) so extractors
    # like YouTube that require a signed-in session don't hit bot-checks.
    # Imported lazily / defensively so a missing config attr or an
    # unconfigured deployment never breaks cookie-free downloads.
    try:
        from config import YTDLP_COOKIES_FILE
        if YTDLP_COOKIES_FILE and os.path.isfile(YTDLP_COOKIES_FILE):
            ydl_opts['cookiefile'] = YTDLP_COOKIES_FILE
            logger.info(f"[v0] Using cookies file for this download: {YTDLP_COOKIES_FILE}")
        elif YTDLP_COOKIES_FILE:
            logger.warning(
                f"[v0] YTDLP_COOKIES_FILE is set to '{YTDLP_COOKIES_FILE}' "
                "but that path doesn't exist — downloading cookie-free."
            )
        else:
            logger.info("[v0] No cookies file configured — downloading cookie-free.")
    except ImportError:
        logger.warning("[v0] Could not import YTDLP_COOKIES_FILE from config — downloading cookie-free.")

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        # Pre-flight: metadata only (download=False) first, so an oversized
        # file is rejected before spending time/bandwidth pulling it down.
        # Not every extractor reports size up front (filesize/filesize_approx
        # can be None) — falls through to the post-download hard check below.
        try:
            preflight_info = ydl.extract_info(url, download=False)
        except Exception as e:
            # Surfaced in full (not truncated to 100 chars) since this is
            # almost always the useful error — e.g. yt-dlp's "Sign in to
            # confirm you're not a bot" age/bot-check message, geo-block,
            # unsupported URL, private/deleted video, etc.
            logger.error(f"[v0] yt-dlp preflight failed for {url}: {e}")
            error_text = str(e)
            hint = ""
            if "cookiefile" not in ydl_opts and (
                "sign in" in error_text.lower() or "confirm you" in error_text.lower()
            ):
                hint = " (this extractor needs a signed-in session — set YTDLP_COOKIES_FILE)"
            return {"error": f"Couldn't read this link: {error_text}{hint}"}

        estimated_size = preflight_info.get("filesize") or preflight_info.get("filesize_approx")
        if estimated_size and estimated_size > MAX_DOWNLOAD_FILE_SIZE:
            return {
                "error": f"File too large: ~{estimated_size / (1024*1024):.1f}MB. "
                         f"Max allowed: {MAX_DOWNLOAD_FILE_SIZE // (1024*1024)}MB"
            }

        try:
            info = ydl.extract_info(url, download=True)
        except Exception as e:
            logger.error(f"[v0] yt-dlp download failed for {url}: {e}")
            return {"error": f"Download failed: {e}"}

        filepath = ydl.prepare_filename(info)
        filesize = os.path.getsize(filepath)

        # Hard check (covers extractors that couldn't report size up front)
        if filesize > MAX_DOWNLOAD_FILE_SIZE:
            os.remove(filepath)
            return {
                "error": f"File too large: {filesize / (1024*1024):.1f}MB. "
                         f"Max allowed: {MAX_DOWNLOAD_FILE_SIZE // (1024*1024)}MB"
            }

        return {
            "filename": os.path.basename(filepath),
            "filepath": filepath,
            "size_mb": round(filesize / (1024 * 1024), 2),
            "duration_seconds": info.get('duration', 0),
            "format": media_type,
            "title": info.get('title', 'Media'),
            "uploader": info.get('uploader', 'Unknown')
        }


async def download_media(url: str, media_type: str = "audio") -> Optional[Dict]:
    """
    Download audio or video from URL (YouTube, etc.) using yt-dlp.
    media_type: "audio" (m4a/opus, whatever the source provides — no ffmpeg
    transcoding) or "video" (pre-muxed mp4).
    Returns dict with: filename, size_mb, duration_seconds, format.
    Respects Discord's safe unboosted-server upload cap (MAX_DOWNLOAD_FILE_SIZE).
    The actual yt-dlp work runs in a worker thread via asyncio.to_thread so it
    never blocks the bot's event loop.
    """
    try:
        return await asyncio.to_thread(_run_download, url, media_type)
    except Exception as e:
        logger.error(f"[v0] Error downloading media from {url}: {e}")
        return {"error": str(e)[:200]}


# ═══════════════════════════════════════════════════════════════════════════
# CRYPTO PRICES (already exists in superbot but adding here for convenience)
# ═══════════════════════════════════════════════════════════════════════════

async def get_crypto_price(coin: str) -> Optional[Dict]:
    """
    Get current crypto price using CoinGecko API (free, no key required).
    Returns dict with: coin, price_usd, market_cap_usd, volume_24h_usd, change_24h_percent
    """
    try:
        coin = coin.lower().strip()
        
        async with aiohttp.ClientSession() as session:
            url = "https://api.coingecko.com/api/v3/simple/price"
            params = {
                "ids": coin,
                "vs_currencies": "usd",
                "include_market_cap": "true",
                "include_24hr_vol": "true",
                "include_24hr_change": "true"
            }
            
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    
                    if coin in data:
                        coin_data = data[coin]
                        return {
                            "coin": coin.upper(),
                            "price_usd": coin_data.get("usd"),
                            "market_cap_usd": coin_data.get("usd_market_cap"),
                            "volume_24h_usd": coin_data.get("usd_24h_vol"),
                            "change_24h_percent": coin_data.get("usd_24h_change"),
                            "timestamp": datetime.now().isoformat()
                        }
        
        return None
    
    except Exception as e:
        logger.error(f"[v0] Error fetching crypto price for {coin}: {e}")
        return None
