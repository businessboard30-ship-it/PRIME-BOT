"""
Scam Shield, extra picture sources: frames of animated GIF / WebP files and the images inside link-preview embeds.

Why: until now GIFs were skipped by every free check, and a scammer's link preview (an embed Discord builds from the
page) was never looked at. Both are pictures like any other, so they go through the same free checks (hash rules,
QR codes, OCR text). This module only prepares the bytes; the cog runs the checks.

Safety: fails open (any error = no frames / no bytes). Embed images are only fetched from Discord's own image proxy
hosts over https with redirects off, so a message can't make the bot request an arbitrary address. Size, pixel and
frame counts are all capped.
"""

from __future__ import annotations

import io
import logging
import os
from collections import OrderedDict
from typing import List, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

MAX_PIXELS = 16_000_000
MAX_FRAMES = 3                   # frames sampled from an animation: first, middle, last
SEEK_LIMIT = 120                 # frames beyond this are ignored (seeking a GIF decodes everything before it)
MAX_FRAME_SIDE = 1200
MAX_EMBED_IMAGES = 3
MAX_BYTES = 2_000_000
FETCH_TIMEOUT = 6.0
PROXY_HOSTS = ("cdn.discordapp.com", "media.discordapp.net", "images-ext-1.discordapp.net",
               "images-ext-2.discordapp.net")


def enabled() -> bool:
    """Embed-picture checks on/off. SCAM_EMBEDS=0 turns them off completely."""
    return os.environ.get("SCAM_EMBEDS", "1").strip().lower() not in ("0", "off", "false", "no")


_clean: "OrderedDict[str, bool]" = OrderedDict()


def seen_clean(url: str) -> bool:
    """True if this exact picture URL was already downloaded and found clean (never download it twice)."""
    return url in _clean


def mark_clean(url: str) -> None:
    _clean[url] = True
    if len(_clean) > 1000:
        _clean.popitem(last=False)


def frames(data: bytes, n: int = MAX_FRAMES) -> List[bytes]:
    """[data] for a still picture; up to `n` PNG frames (first / middle / last) for an animated GIF or WebP;
    [] when it isn't a readable picture or is too big. Synchronous (run it in an executor)."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        if im.width * im.height > MAX_PIXELS:
            return []
        count = int(getattr(im, "n_frames", 1) or 1)
        if count <= 1 or not getattr(im, "is_animated", False):
            return [data]
        last = min(count, SEEK_LIMIT) - 1
        out: List[bytes] = []
        for i in sorted({0, last // 2, last})[:n]:
            im.seek(i)
            fr = im.convert("RGB")
            longest = max(fr.size)
            if longest > MAX_FRAME_SIDE:
                f = MAX_FRAME_SIDE / longest
                fr = fr.resize((max(1, round(fr.width * f)), max(1, round(fr.height * f))), Image.LANCZOS)
            buf = io.BytesIO()
            fr.save(buf, "PNG")
            out.append(buf.getvalue())
        return out
    except Exception:
        logger.debug("[scam-media] couldn't read frames", exc_info=True)
        return []


def allowed_url(url: str) -> bool:
    try:
        u = urlparse(url or "")
        return u.scheme == "https" and (u.hostname or "").lower() in PROXY_HOSTS
    except Exception:
        return False


def embed_urls(message, limit: int = MAX_EMBED_IMAGES) -> List[Tuple[str, bool]]:
    """[(url, ocr_ok)] for the pictures inside a message's embeds (Discord's proxied copies only). OCR is only allowed
    for a real picture embed or an embed's main image, never for a link's small thumbnail. Video embeds are skipped
    (YouTube thumbnails are the common benign case)."""
    seen, out = set(), []
    for e in getattr(message, "embeds", None) or ():
        if getattr(e, "type", None) == "video":
            continue
        for attr in ("image", "thumbnail"):
            part = getattr(e, attr, None)
            url = None
            for cand in (getattr(part, "proxy_url", None), getattr(part, "url", None)):
                if isinstance(cand, str) and allowed_url(cand):
                    url = cand
                    break
            if url and url not in seen:
                seen.add(url)
                out.append((url, attr == "image" or getattr(e, "type", None) == "image"))
            if len(out) >= limit:
                return out
    return out


async def fetch(url: str) -> "bytes | None":
    """Download one proxied embed picture (None on any problem, a wrong content type or more than MAX_BYTES)."""
    if not allowed_url(url):
        return None
    try:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, allow_redirects=False) as resp:
                if resp.status != 200 or not (resp.headers.get("Content-Type", "").startswith("image/")):
                    return None
                if int(resp.headers.get("Content-Length", "0") or 0) > MAX_BYTES:
                    return None
                buf = bytearray()
                async for chunk in resp.content.iter_chunked(65536):
                    buf += chunk
                    if len(buf) > MAX_BYTES:
                        return None
                return bytes(buf)
    except Exception:
        logger.debug("[scam-media] fetch failed", exc_info=True)
        return None
