"""
Scam Shield, picture text: read the words out of an uploaded image and run them through the same text rules
as a normal message (modules/scam_shield.match_text: scam words, scam domains, giveaway/promo-code bait).

Why: a scammer can post ONE picture with no caption (a fake celebrity post, a fake casino page). The text rules
never see those words because they're pixels, the hash rules only know pictures we already saw, and the AI check
has a small daily budget. Reading the text is free, so it runs before the AI check and saves its budget.

How: the `tesseract` program (apt package tesseract-ocr, see railpack.json / nixpacks.toml), called as a
subprocess with the image on stdin. If it isn't installed this whole stage is skipped (available() is False).

Safety: fails open (any error, timeout or odd output = "no text", never a delete), at most two reads at once,
10-second cap per read, results cached by file hash so a repeated spam picture costs one read.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import shutil
from collections import OrderedDict
from typing import Optional, Tuple

from modules import scam_shield as ss

logger = logging.getLogger(__name__)

MAX_SIDE = 1600                 # px, longest side handed to tesseract
MIN_UPSCALE_SIDE = 900          # smaller images are enlarged 2x (tesseract reads tiny text badly)
MAX_PIXELS = 40_000_000         # refuse decompression-bomb sized pictures
READ_TIMEOUT = 10.0
MIN_TEXT_CHARS = 30             # shorter output is noise (a meme with 3 words), never matched
MAX_TEXT_CHARS = 4000
CACHE_MAX = 500

_slots = asyncio.Semaphore(2)
_cache: "OrderedDict[str, str]" = OrderedDict()
_which: Optional[str] = None
_checked = False


def tesseract_path() -> Optional[str]:
    global _which, _checked
    if not _checked:
        _which, _checked = shutil.which("tesseract"), True
    return _which


def available() -> bool:
    """True when tesseract is installed and SCAM_OCR is not set to 0/off/false."""
    if os.environ.get("SCAM_OCR", "1").strip().lower() in ("0", "off", "false", "no"):
        return False
    return tesseract_path() is not None


def prepare(data: bytes) -> Optional[bytes]:
    """Grayscale + auto-contrast PNG, at most MAX_SIDE on the long edge. None if it isn't a readable picture."""
    try:
        from PIL import Image, ImageOps
        im = Image.open(io.BytesIO(data))
        if im.width * im.height > MAX_PIXELS:
            return None
        im = ImageOps.autocontrast(ImageOps.exif_transpose(im).convert("L"))
        longest = max(im.size)
        if longest < MIN_UPSCALE_SIDE:
            im = im.resize((im.width * 2, im.height * 2), Image.LANCZOS)
        elif longest > MAX_SIDE:
            scale = MAX_SIDE / longest
            im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, "PNG")
        return out.getvalue()
    except Exception:
        logger.debug("[scam-ocr] couldn't prepare the picture", exc_info=True)
        return None


async def read_text(data: bytes) -> str:
    """The words in the picture ('' when none, or on any failure)."""
    exe = tesseract_path()
    if exe is None:
        return ""
    key = hashlib.sha256(data).hexdigest()
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    png = await asyncio.get_running_loop().run_in_executor(None, prepare, data)
    if png is None:
        return ""
    text = ""
    async with _slots:
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                exe, "stdin", "stdout", "--psm", "6", "-l", "eng",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(png), timeout=READ_TIMEOUT)
            text = out.decode("utf-8", "ignore")[:MAX_TEXT_CHARS]
        except Exception:
            logger.debug("[scam-ocr] read failed", exc_info=True)
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
    _cache[key] = text
    if len(_cache) > CACHE_MAX:
        _cache.popitem(last=False)
    return text


def match(text: str, allowed=()) -> Optional[Tuple[str, str, Optional[int]]]:
    """Run picture text through the normal text rules. -> ('image_text', 'word: fatowin', rule_id) or None."""
    if not text or len(text.strip()) < MIN_TEXT_CHARS:
        return None
    hit = ss.match_text(text, allowed)
    if hit is None:
        return None
    return "image_text", f"{hit[0]}: {hit[1]}", hit[2]
