"""Meme feed rules (pure) plus the fetch from meme-api.com. No database, no Discord objects.

Only the preset subreddits below can be chosen, so a server can never point the bot at an arbitrary or NSFW source, and NSFW /
spoiler posts that still slip through are dropped.
"""
from __future__ import annotations

import logging
import random
from typing import Iterable, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

API = "https://meme-api.com/gimme"
PRESETS = {
    "memes": "r/memes", "dankmemes": "r/dankmemes", "wholesomememes": "r/wholesomememes",
    "ProgrammerHumor": "r/ProgrammerHumor", "me_irl": "r/me_irl", "AnimeMemes": "r/AnimeMemes", "funny": "r/funny",
}
DEFAULT_SUBS = ("memes", "dankmemes", "wholesomememes")
INTERVALS = (1, 3, 6, 12, 24)
DEFAULT_INTERVAL = 6
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp")


def clean_subs(values: Iterable[str]) -> List[str]:
    """Keeps only preset subreddits, in preset order, no duplicates. Never empty (falls back to the defaults)."""
    wanted = {str(v) for v in (values or [])}
    out = [s for s in PRESETS if s in wanted]
    return out or list(DEFAULT_SUBS)


def subs_from_text(text: Optional[str]) -> List[str]:
    return clean_subs((text or "").split(","))


def valid_interval(hours) -> int:
    try:
        h = int(hours)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL
    return h if h in INTERVALS else DEFAULT_INTERVAL


def parse_meme(data: dict) -> Optional[dict]:
    """One meme-api entry -> {url, title, post, sub, ups}, or None if it can't be posted safely."""
    if not isinstance(data, dict) or data.get("nsfw") or data.get("spoiler"):
        return None
    url = str(data.get("url") or "")
    if not url.startswith("https://") or not url.lower().split("?")[0].endswith(IMAGE_EXT):
        return None
    return {
        "url": url,
        "title": str(data.get("title") or "Meme")[:200],
        "post": str(data.get("postLink") or ""),
        "sub": str(data.get("subreddit") or ""),
        "ups": int(data.get("ups") or 0),
    }


async def fetch_meme(subs: Iterable[str], seen: Optional[set] = None) -> Optional[dict]:
    """A fresh meme from one of `subs` (not in `seen`), or None. Never raises."""
    seen = seen if seen is not None else set()
    pool = clean_subs(subs)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            for sub in random.sample(pool, k=min(len(pool), 3)):
                async with s.get(f"{API}/{sub}/5") as r:
                    if r.status != 200:
                        continue
                    data = await r.json(content_type=None)
                for raw in (data or {}).get("memes", []) or ([data] if isinstance(data, dict) and data.get("url") else []):
                    m = parse_meme(raw)
                    if m and m["url"] not in seen:
                        return m
    except Exception as e:
        logger.warning("[memes] fetch failed (%s)", type(e).__name__)
    return None
