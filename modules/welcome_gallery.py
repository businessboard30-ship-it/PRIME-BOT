"""Welcome Card Studio Phase 2a: which gallery picture a newcomer gets (plan section 3.1).

Pure logic only: no database, no Discord. Never raises, so a join can never fail here.
The caller (a later slice) passes the filled slots, the member number and the saved rotation state.
"""
from __future__ import annotations

import random
from typing import Optional, Sequence

MAX_PICTURES = 10

MODE_PIN = "pin"          # same picture every time
MODE_RANDOM = "random"    # random, never the same twice in a row (default)
MODE_ORDER = "order"      # one after another
MODE_MEMBER = "member"    # member number N gets picture N mod count
MODES = (MODE_PIN, MODE_RANDOM, MODE_ORDER, MODE_MEMBER)
DEFAULT_MODE = MODE_RANDOM

# Plain words for the wizard select.
MODE_LABELS = {
    MODE_PIN: "Same picture every time",
    MODE_RANDOM: "Random, never the same twice in a row",
    MODE_ORDER: "In order, one after another",
    MODE_MEMBER: "By member number",
}


def clean_mode(mode: Optional[str]) -> str:
    return mode if mode in MODES else DEFAULT_MODE


def pick_slot(
    mode: Optional[str],
    slots: Sequence[int],
    member_number: Optional[int] = None,
    last_slot: Optional[int] = None,
    counter: int = 0,
    pinned: Optional[int] = None,
    rng: Optional[random.Random] = None,
) -> Optional[int]:
    """Return the slot number to use, or None when there are no usable pictures.

    `slots` are the slot numbers that currently hold a usable picture (missing or flagged ones
    already removed by the caller). Order is by slot number.
    """
    try:
        usable = sorted({int(s) for s in slots if 1 <= int(s) <= MAX_PICTURES})
        if not usable:
            return None
        if len(usable) == 1:
            return usable[0]
        mode = clean_mode(mode)
        if mode == MODE_PIN:
            return pinned if pinned in usable else usable[0]
        if mode == MODE_ORDER:
            return usable[int(counter) % len(usable)]
        if mode == MODE_MEMBER:
            n = int(member_number) if member_number else 0
            return usable[n % len(usable)]
        choices = [s for s in usable if s != last_slot] or usable
        return (rng or random).choice(choices)
    except Exception:
        return None


# ── Phase 2d: picture preparation and the daily upload limit ──────────────────────────────────────────────
import io
import time
from collections import deque

CARD_WIDTH = 1536                 # pictures are stored no wider than this (plan 3.1)
MAX_HEIGHT = 3072                 # a very tall picture is still limited
MAX_SOURCE_PIXELS = 40_000_000    # refuse decompression bombs before decoding
JPEG_QUALITY = 88
DAILY_UPLOAD_LIMIT = 20           # pictures a server may add per rolling 24 hours (flood guard for the hosting channel)
MAX_PER_UPLOAD = 10               # Discord's file field takes up to 10 files


def prepare_picture(data: bytes) -> Optional[bytes]:
    """Downscale to the card width and re-encode as JPEG. None when it is not a usable image. Never raises.

    Cuts hosting storage and render time (plan 3.1). Never upscales. Honors the camera rotation flag.
    """
    try:
        from PIL import Image, ImageOps
        img = Image.open(io.BytesIO(data))
        w, h = img.size
        if w < 1 or h < 1 or w * h > MAX_SOURCE_PIXELS:
            return None
        img = ImageOps.exif_transpose(img).convert("RGB")
        img.thumbnail((CARD_WIDTH, MAX_HEIGHT))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return out.getvalue()
    except Exception:
        return None


_RECENT: dict = {}   # (guild_id, clone_key) -> deque of upload times; catches add/remove loops inside one process


def uploads_left(guild_id: int, clone_id: Optional[int], db_recent: int = 0, now: Optional[float] = None) -> int:
    """How many more pictures this server may add right now. `db_recent` is the stored count from the last 24 hours;
    the in-memory count covers pictures added then removed. The larger of the two counts."""
    try:
        now = time.time() if now is None else now
        q = _RECENT.get((int(guild_id), -1 if clone_id is None else int(clone_id)))
        mem = 0
        if q is not None:
            while q and now - q[0] > 86400:
                q.popleft()
            mem = len(q)
        return max(0, DAILY_UPLOAD_LIMIT - max(int(db_recent or 0), mem))
    except Exception:
        return 0


def note_upload(guild_id: int, clone_id: Optional[int], now: Optional[float] = None) -> None:
    try:
        key = (int(guild_id), -1 if clone_id is None else int(clone_id))
        _RECENT.setdefault(key, deque(maxlen=DAILY_UPLOAD_LIMIT * 2)).append(time.time() if now is None else now)
        if len(_RECENT) > 5000:       # keep memory bounded
            for k in list(_RECENT)[:1000]:
                _RECENT.pop(k, None)
    except Exception:
        pass
