"""
Scam Shield, QR codes: decode QR codes in an uploaded picture and run the link inside through the scam rules.

Why: "scan to claim" bait hides the link in pixels, so neither the text rules nor OCR see it. Decoding is free and
fast (a few ms), so it runs before OCR and the AI check.

How: `pyzbar` (pip) on top of the small `libzbar0` system library (apt, see railpack.json / nixpacks.toml). It was
picked over OpenCV on purpose: OpenCV adds tens of MB of RAM. If either piece is missing, available() is False and the
stage is skipped.

Safety: fails open (any error or odd output = no QR, never a delete). Only the SERVER'S RULES decide: a QR that points
at an unknown site is not a hit by itself. Pictures over MAX_PIXELS are skipped, one decode at a time, results cached
by file hash.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
from collections import OrderedDict
from typing import List, Optional, Tuple

from modules import scam_ocr as sco
from modules import scam_shield as ss

logger = logging.getLogger(__name__)

MAX_PIXELS = 16_000_000
MAX_SIDE = 1600                  # decode at most this long edge (QR codes are big enough at this size)
MAX_CODES = 5
MAX_PAYLOAD = 500
DECODE_TIMEOUT = 6.0
CACHE_MAX = 200

_slot = asyncio.Semaphore(1)
_cache: "OrderedDict[str, Tuple[str, ...]]" = OrderedDict()
_zbar = None
_checked = False


def _lib():
    global _zbar, _checked
    if not _checked:
        _checked = True
        try:
            from pyzbar import pyzbar          # ImportError / OSError when libzbar0 isn't installed
            _zbar = pyzbar
        except Exception:
            logger.debug("swallowed exception in _lib", exc_info=True)
            _zbar = None
    return _zbar


def available() -> bool:
    """True when pyzbar + libzbar0 load and SCAM_QR is not set to 0/off/false."""
    if os.environ.get("SCAM_QR", "1").strip().lower() in ("0", "off", "false", "no"):
        return False
    return _lib() is not None


def decode(data: bytes) -> List[str]:
    """Text of every QR code in the picture (at most MAX_CODES), [] when none or on any problem. Synchronous."""
    lib = _lib()
    if lib is None:
        return []
    try:
        from PIL import Image, ImageOps
        im = Image.open(io.BytesIO(data))
        if im.width * im.height > MAX_PIXELS:
            return []
        im.draft("L", (MAX_SIDE, MAX_SIDE))
        g = ImageOps.exif_transpose(im).convert("L")
        del im
        longest = max(g.size)
        if longest > MAX_SIDE:
            f = MAX_SIDE / longest
            g = g.resize((max(1, round(g.width * f)), max(1, round(g.height * f))), Image.LANCZOS)
        tries = [g, ImageOps.invert(g)]
        if longest < 600:                        # a small screenshot: enlarge once
            tries.append(g.resize((g.width * 2, g.height * 2), Image.LANCZOS))
        found: List[str] = []
        for im2 in tries:
            for sym in lib.decode(im2, symbols=[lib.ZBarSymbol.QRCODE]):
                try:
                    txt = sym.data.decode("utf-8", "ignore").strip()[:MAX_PAYLOAD]
                except Exception:
                    logger.debug("swallowed exception in decode", exc_info=True)
                    continue
                if txt and txt not in found:
                    found.append(txt)
            if found:
                break
        return found[:MAX_CODES]
    except Exception:
        logger.debug("[scam-qr] decode failed", exc_info=True)
        return []


async def read_codes(data: bytes) -> Tuple[str, ...]:
    if not available():
        return ()
    key = hashlib.sha256(data).hexdigest()
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    codes: Tuple[str, ...] = ()
    async with _slot:
        try:
            codes = tuple(await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(None, decode, data), timeout=DECODE_TIMEOUT))
        except Exception:
            logger.debug("[scam-qr] read failed", exc_info=True)
    _cache[key] = codes
    if len(_cache) > CACHE_MAX:
        _cache.popitem(last=False)
    return codes


def match(codes, allowed=()) -> Optional[Tuple[str, str, Optional[int]]]:
    """Run each decoded payload through the normal rules (words, scam domains, garbled/look-alike domains).
    -> ('qr', 'domain: hubowin.com', rule id) or None. Real Discord hosts and allowed domains never match."""
    for payload in codes or ():
        hit = ss.match_text(payload, allowed) or sco.match_domains(payload, allowed)
        if hit:
            return "qr", f"{hit[0]}: {hit[1]}", hit[2]
    return None


async def scan(data: bytes, allowed=()) -> Optional[Tuple[str, str, Optional[int]]]:
    try:
        return match(await read_codes(data), allowed)
    except Exception:
        logger.debug("[scam-qr] scan failed", exc_info=True)
        return None
