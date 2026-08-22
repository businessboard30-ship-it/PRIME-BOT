"""
Reverse image search via Yandex Images (free, no API key).

Yandex is queried directly by URL — no SerpApi, no paid API of any kind.
Trade-off, and it's a real one: Yandex serves this HTML to a normal browser
and sometimes throws a CAPTCHA at obvious datacenter traffic (which is what
a Vercel serverless function looks like to them). When that happens this
returns None with a clear "temporarily unavailable" message rather than
crashing. Yandex's markup also isn't a public contract, so this can break
silently whenever they redesign the results page -- if searches stop
returning matches, that's the first thing to check.

Flow:
  1. GET https://yandex.com/images/search?rpt=imageview&url=<image_url>
  2. Pull the embedded `data-bem` JSON blobs off each `serp-item` div --
     that's where Yandex embeds title/url/thumbnail for every result,
     inside the server-rendered HTML (no JS execution needed).
"""

import asyncio
import json
import logging
import re
from typing import List, Optional

import aiohttp

logger = logging.getLogger(__name__)

YANDEX_SEARCH_URL = "https://yandex.com/images/search"

# Mimic a real browser closely -- reduces (does not eliminate) the chance of
# an immediate CAPTCHA wall on the first request.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

REQUEST_TIMEOUT_SECONDS = 15
MAX_ATTEMPTS = 2

# Matches serp-item wrapper divs and captures their data-bem JSON attribute.
_SERP_ITEM_RE = re.compile(
    r'class="serp-item[^"]*"\s+data-bem="({.*?})"\s*(?:data-|>)',
    re.DOTALL,
)


async def reverse_image_search(image_url: str, max_results: int = 5) -> Optional[List[dict]]:
    """
    Run a Yandex reverse image search against a publicly reachable
    image_url (e.g. a Telegram file URL). Returns a list of
    {"title": str, "url": str, "thumbnail": str, "domain": str} dicts,
    an empty list if the search ran but found nothing, or None if the
    search itself failed (CAPTCHA wall, network error, unparseable page,
    etc.) -- callers should show a distinct "temporarily unavailable"
    message for None vs. a plain "no matches" message for [].
    """
    reverse_image_search.last_error = None

    params = {"rpt": "imageview", "url": image_url}

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            async with aiohttp.ClientSession(headers=_HEADERS) as session:
                async with session.get(
                    YANDEX_SEARCH_URL, params=params,
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
                ) as resp:
                    html = await resp.text()

                    if resp.status == 429 or "showcaptcha" in str(resp.url):
                        reverse_image_search.last_error = (
                            "Yandex showed a CAPTCHA instead of results (common from server IPs). "
                            "Try again in a bit."
                        )
                        logger.warning("[v0] Yandex reverse image search hit a CAPTCHA wall")
                        return None

                    if resp.status != 200:
                        reverse_image_search.last_error = f"Yandex HTTP {resp.status}"
                        logger.warning(f"[v0] Yandex reverse image search failed: HTTP {resp.status}")
                        return None

            return _parse_results(html, max_results)

        except (asyncio.TimeoutError, aiohttp.ServerTimeoutError):
            logger.warning(f"[v0] Yandex request timed out (attempt {attempt}/{MAX_ATTEMPTS})")
            if attempt < MAX_ATTEMPTS:
                continue
            reverse_image_search.last_error = (
                f"Yandex didn't respond within {REQUEST_TIMEOUT_SECONDS}s after {MAX_ATTEMPTS} attempts. "
                "Try again in a moment."
            )
            return None

        except Exception as e:
            reverse_image_search.last_error = f"{type(e).__name__}: {e}"
            logger.error(f"[v0] Yandex reverse image search error: {e}")
            return None

    return None


reverse_image_search.last_error = None


def _parse_results(html: str, max_results: int) -> List[dict]:
    """
    Pull matched-site entries out of Yandex's server-rendered HTML.

    Each result on the page is a div.serp-item carrying a data-bem="{...}"
    JSON attribute with a "serp-item" key holding img_href (the source page
    URL), snippet.title, snippet.domain (or thumb/preview URLs for the
    thumbnail). We regex out each data-bem blob and json.loads it rather
    than pulling in a full HTML parser dependency.
    """
    results = []

    for match in _SERP_ITEM_RE.finditer(html):
        raw = match.group(1)
        try:
            # data-bem is HTML-attribute-escaped JSON (&quot; etc.)
            unescaped = (
                raw.replace("&quot;", '"')
                   .replace("&amp;", "&")
                   .replace("&#x27;", "'")
            )
            blob = json.loads(unescaped)
        except (json.JSONDecodeError, ValueError):
            continue

        item = blob.get("serp-item") or blob.get("serp-item-th")
        if not item:
            continue

        link = item.get("img_href") or item.get("url")
        if not link:
            continue

        snippet = item.get("snippet") or {}
        thumb = None
        preview = item.get("preview") or item.get("thumb")
        if isinstance(preview, list) and preview:
            thumb = preview[0].get("url")
        elif isinstance(preview, dict):
            thumb = preview.get("url")
        if thumb and thumb.startswith("//"):
            thumb = "https:" + thumb

        results.append({
            "title": snippet.get("title") or item.get("domain") or "Untitled",
            "url": link,
            "thumbnail": thumb,
            "domain": item.get("domain", ""),
        })
        if len(results) >= max_results:
            break

    return results
