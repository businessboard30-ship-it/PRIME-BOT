"""
Scam Shield, picture text: read the words out of an uploaded image and run them through the same text rules
as a normal message (modules/scam_shield.match_text: scam words, scam domains, giveaway/promo-code bait).

Why: a scammer can post ONE picture with no caption (a fake celebrity post, a fake casino page). The text rules
never see those words because they're pixels, the hash rules only know pictures we already saw, and the AI check
has a small daily budget. Reading the text is free, so it runs before the AI check and saves its budget.

How: the `tesseract` program (apt package tesseract-ocr, see railpack.json / nixpacks.toml), called as a
subprocess with the image on stdin. If it isn't installed this whole stage is skipped (available() is False).

Safety: fails open (any error, timeout or odd output = "no text", never a delete), at most SCAM_OCR_SLOTS reads at once (default 1),
10-second cap per read, results cached by file hash so a repeated spam picture costs one read.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import re
import shutil
import unicodedata
from collections import OrderedDict
from typing import List, Optional, Set, Tuple

from modules import scam_shield as ss

logger = logging.getLogger(__name__)

MAX_SIDE = 1600                 # px, longest side handed to tesseract
MIN_UPSCALE_SIDE = 900          # smaller images are enlarged 2x (tesseract reads tiny text badly)
MAX_PIXELS = 16_000_000         # refuse huge pictures (a 36 MP one peaked at ~400 MB); real screenshots are far smaller
READ_TIMEOUT = 10.0
MIN_TEXT_CHARS = 30             # shorter output is noise (a meme with 3 words), never matched
MAX_TEXT_CHARS = 4000
CACHE_MAX = 200                 # cached reads (text only, a few KB each)
EXTRA_BUDGET = 14.0             # seconds for ALL extra passes on one picture (each pass still has READ_TIMEOUT)
EXTRA_MAX_SIDE = 2000           # px, longest side of an enlarged extra pass
MIN_FUZZY_LABEL = 6             # a name shorter than this must match exactly (edit distance 1 is too loose there)

def _slot_count() -> int:
    try:
        return max(1, min(4, int(os.environ.get("SCAM_OCR_SLOTS", "1"))))
    except ValueError:
        return 1


_slots = asyncio.Semaphore(_slot_count())   # each tesseract process peaks around 45 MB, so one at a time by default
_cache: "OrderedDict[str, str]" = OrderedDict()
_cache_more: "OrderedDict[str, str]" = OrderedDict()
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
        im.draft("L", (MAX_SIDE, MAX_SIDE))          # JPEG: decode at 1/2, 1/4 or 1/8 size straight away
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


async def _tess(exe: str, png: bytes, psm: str) -> str:
    """One tesseract run ('' on any failure or timeout). Holds one of the two read slots."""
    text = ""
    async with _slots:
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                exe, "stdin", "stdout", "--psm", psm, "-l", "eng",
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
    return text


def _png(im) -> bytes:
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def _enlarge(im, factor: int):
    from PIL import Image
    w, h = im.size
    f = min(factor, EXTRA_MAX_SIDE / max(w, h))
    if f <= 1:
        return im
    return im.resize((max(1, round(w * f)), max(1, round(h * f))), Image.LANCZOS)


def variants(data: bytes, only_urlbar: bool = False) -> List[Tuple[bytes, str]]:
    """Extra reading attempts for a picture the first pass couldn't use: [(png, psm)]. The top strip (browser tab /
    address bar) comes first. Light-on-dark text gets an inverted copy, small text an enlarged one, and the middle
    (the post body) is read on its own. [] if it isn't a readable picture."""
    try:
        from PIL import Image, ImageOps
        im = Image.open(io.BytesIO(data))
        if im.width * im.height > MAX_PIXELS:
            return []
        im.draft("L", (EXTRA_MAX_SIDE, EXTRA_MAX_SIDE))
        g = ImageOps.autocontrast(ImageOps.exif_transpose(im).convert("L"))
        del im
        w, h = g.size
        out: List[Tuple[bytes, str]] = []
        if h >= 60:
            out.append((_png(_enlarge(g.crop((0, 0, w, max(30, round(h * 0.18)))), 3)), "11"))
        if only_urlbar:
            return out
        out.append((_png(ImageOps.invert(_enlarge(g, 2 if max(w, h) < MAX_SIDE else 1))), "11"))
        out.append((_png(_enlarge(g, 2)), "4"))
        if h >= 200:
            out.append((_png(_enlarge(g.crop((0, round(h * 0.2), w, round(h * 0.85))), 2)), "6"))
        return out
    except Exception:
        logger.debug("[scam-ocr] couldn't build extra passes", exc_info=True)
        return []


async def read_more(data: bytes, only_urlbar: bool = False) -> str:
    """The words from the extra passes, joined ('' on failure). Cached by file hash. Bounded by EXTRA_BUDGET."""
    exe = tesseract_path()
    if exe is None:
        return ""
    key = hashlib.sha256(data).hexdigest() + (":u" if only_urlbar else ":a")
    if key in _cache_more:
        _cache_more.move_to_end(key)
        return _cache_more[key]
    loop = asyncio.get_running_loop()
    passes = await loop.run_in_executor(None, variants, data, only_urlbar)
    deadline = loop.time() + EXTRA_BUDGET
    parts: List[str] = []
    for png, psm in passes:
        if loop.time() >= deadline:
            break
        parts.append(await _tess(exe, png, psm))
    text = "\n".join(p for p in parts if p)[:MAX_TEXT_CHARS * 2]
    _cache_more[key] = text
    if len(_cache_more) > CACHE_MAX:
        _cache_more.popitem(last=False)
    return text


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
    text = await _tess(exe, png, "6")
    _cache[key] = text
    if len(_cache) > CACHE_MAX:
        _cache.popitem(last=False)
    return text


# ── domains in picture text ──────────────────────────────────────────────

_TLDS = "com|net|org|io|gg|xyz|top|club|online|site|vip|bet|casino|info|app|fun"
_DOT_WORD = re.compile(r"\s*(?:\[\s*\.\s*\]|\(\s*\.\s*\)|\{\s*\.\s*\}|[\[({]\s*dot\s*[\])}])\s*", re.I)
_SPACED_DOT = re.compile(r"(?<=[a-z0-9])\s*(?:\.|,|\s+dot\s+)\s*(?=(?:" + _TLDS + r")\b)", re.I)
_CONFUSABLE = str.maketrans({"\u0430": "a", "\u0435": "e", "\u043e": "o", "\u0440": "p", "\u0441": "c", "\u0445": "x",
                             "\u0443": "y", "\u0456": "i", "\u0455": "s", "\u03bf": "o", "\u03b1": "a", "\u03c1": "p",
                             "\u0131": "i"})


def clean_urls(text: str) -> str:
    """Undo how scam pictures and OCR mangle a link: 'hubowin [.] com', 'hubowin (dot) com', 'hubowin . com',
    'hubowin,com'. Only touches a dot-like gap in front of a common TLD, so normal sentences are left alone."""
    t = _DOT_WORD.sub(".", text or "")
    return _SPACED_DOT.sub(".", t)


def skeleton(s: str) -> str:
    """A name with the usual OCR / look-alike confusions folded together, so 'hub0win', 'hubovvin' and 'rnail'
    compare equal to 'hubowin' and 'mail'. Used for BOTH sides of a comparison."""
    s = unicodedata.normalize("NFKD", (s or "").lower()).translate(_CONFUSABLE)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("rn", "m").replace("vv", "w")
    return s.translate(str.maketrans({"0": "o", "1": "l", "i": "l", "|": "l"}))


def registrable(host: str) -> str:
    """'login.www.hubowin.com' -> 'hubowin.com'; 'x.hubowin.co.uk' -> 'hubowin.co.uk' (the usual 2-letter-TLD pattern)."""
    parts = [p for p in (host or "").lower().strip(".").split(".") if p]
    if len(parts) <= 2:
        return ".".join(parts)
    if len(parts[-1]) == 2 and parts[-2] in ("co", "com", "org", "net", "gov", "ac", "ne", "or"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _within_one(a: str, b: str) -> bool:
    """Edit distance <= 1 (rapidfuzz when installed, a tiny exact check otherwise)."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    try:
        from rapidfuzz.distance import Levenshtein
        return Levenshtein.distance(a, b, score_cutoff=1) <= 1
    except ImportError:
        pass
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1:] if len(a) < len(b) else a[i + 1:] == b[i + 1:]


def _decode_puny(host: str) -> str:
    if "xn--" not in host:
        return host
    try:
        return host.encode("ascii").decode("idna")
    except Exception:
        logger.debug("swallowed exception in _decode_puny", exc_info=True)
        return host


def match_domains(text: str, allowed=()) -> Optional[Tuple[str, str, Optional[int]]]:
    """Find a scam domain in picture text even when OCR garbled it: obfuscated dots, spaces inside the link,
    0/o 1/l/i rn/m vv/w mix-ups, one wrong or missing letter, or a punycode/look-alike spelling. The server's allowed
    domains and Discord's real hosts are never matched. -> ('domain', 'fatowin.com (read as fatow1n.com)', rule id)."""
    rules = ss._c.domains
    if not rules or not text:
        return None
    cleaned = clean_urls(text)
    t = ss.normalize(cleaned)
    scam = [(rid, d, skeleton(registrable(d))) for rid, d in rules]
    hosts: Set[str] = set()
    for h in ss.hosts_in(cleaned):
        hosts.add(h)
        dec = _decode_puny(h)
        if dec != h:
            hosts.add(dec)
    for h in sorted(hosts):
        if ss.is_allowed_domain(h, allowed) or ss.is_official_host(h, t):
            continue
        reg = skeleton(registrable(h))
        label = reg.split(".")[0]
        for rid, d, sk in scam:
            if h == d or h.endswith("." + d):
                return "domain", d, rid
            if reg == sk and len(label) >= 4:
                return "domain", f"{d} (read as {h})", rid
            if len(label) >= MIN_FUZZY_LABEL and len(sk.split(".")[0]) >= MIN_FUZZY_LABEL and _within_one(reg, sk):
                return "domain", f"{d} (read as {h})", rid
    return None


def match(text: str, allowed=()) -> Optional[Tuple[str, str, Optional[int]]]:
    """Run picture text through the normal text rules. -> ('image_text', 'word: fatowin', rule_id) or None."""
    if not text or len(text.strip()) < MIN_TEXT_CHARS:
        return None
    hit = ss.match_text(text, allowed)
    if hit is None:
        hit = match_domains(text, allowed)
    if hit is None:
        return None
    return "image_text", f"{hit[0]}: {hit[1]}", hit[2]


def _has_host(text: str) -> bool:
    return bool(ss.hosts_in(clean_urls(text)))


async def scan(data: bytes, allowed=(), deep: bool = True) -> Optional[Tuple[str, str, Optional[int]]]:
    """Read the picture and run the text rules. Extra passes (address-bar crop, inverted, enlarged, middle crop) only
    run when the first read found nothing: all of them if it got almost no text, just the top strip if it got text
    but no link. Fails open: any problem means None."""
    try:
        first = await read_text(data)
        hit = match(first, allowed)
        if hit or not deep or not available():
            return hit                    # deep=False: the first read only (cheap senders)
        short = len((first or "").strip()) < MIN_TEXT_CHARS
        if not short and _has_host(first):
            return None
        more = await read_more(data, only_urlbar=not short)
        if not more:
            return None
        # extra text is matched on its own too, so a short first read can't hide a good second one
        return match(first + "\n" + more, allowed) or match(more, allowed)
    except Exception:
        logger.debug("[scam-ocr] scan failed", exc_info=True)
        return None
