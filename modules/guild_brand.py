# path: modules/guild_brand.py
"""Per-server branding a server's admins set themselves, so the bot feels like theirs.

  * levelup_message : their own level-up text. Placeholders {user} {level} {server} {level_name} {xp_name}
  * accent          : one colour (hex) used wherever a feature has no colour of its own (leaderboard, giveaways)
  * level_name      : what they call "level" (rank, tier, stage ...)
  * xp_name         : what they call "XP" (points, stars ...)

Storage: ONE small JSON value per server in bot_global_settings (key guild_brand:<main|clone id>:<server id>), so there is
no schema change. Reads are cached for a few minutes and fail open: any problem means "no branding", never an error in the
feature that asked. All typed text is cleaned here (length, control characters, @everyone/@here and <@mentions> removed;
names limited to plain label characters), and placeholders are filled by plain replacement, never str.format.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

KEY = "guild_brand:{c}:{g}"
MESSAGE_MAX = 200
NAME_MAX = 16
CACHE_TTL = 300
FIELDS = ("levelup_message", "accent", "level_name", "xp_name")
PLACEHOLDERS = ("user", "level", "server", "level_name", "xp_name")

_NAME_OK = re.compile(r"^[\w .'\-]+$", re.UNICODE)
_HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")
_MENTION = re.compile(r"<(?:@[!&]?|#)\d+>")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_cache: Dict[Tuple[int, Optional[int]], Tuple[dict, float]] = {}


def _key(guild_id: int, clone_id: Optional[int]) -> str:
    return KEY.format(c="main" if clone_id is None else clone_id, g=guild_id)


def clean_message(raw: str) -> Optional[str]:
    """Blank -> None (back to the default). Raises ValueError with a friendly reason when refused."""
    text = _CTRL.sub("", str(raw or "")).strip()
    if not text:
        return None
    text = _MENTION.sub("", text)
    text = re.sub(r"@(everyone|here)", "@\u200b\\1", text, flags=re.I)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    if len(text) > MESSAGE_MAX:
        raise ValueError(f"the message can be at most {MESSAGE_MAX} characters")
    if not text:
        return None
    if "{user}" not in text:
        raise ValueError("include {user} so the member is mentioned (also available: {level} {server} {level_name} {xp_name})")
    return text


def clean_name(raw: str, what: str = "name") -> Optional[str]:
    text = re.sub(r"\s+", " ", str(raw or "")).strip()
    if not text:
        return None
    if len(text) > NAME_MAX:
        raise ValueError(f"the {what} can be at most {NAME_MAX} characters")
    if not _NAME_OK.match(text) or not any(ch.isalnum() for ch in text):
        raise ValueError(f"the {what} can only use letters, numbers, spaces, dots, dashes and apostrophes")
    return text


def clean_accent(raw: str) -> Optional[int]:
    text = str(raw or "").strip()
    if not text:
        return None
    m = _HEX.match(text)
    if not m:
        raise ValueError("the colour must be 6 hex digits, like #5865F2")
    return int(m.group(1), 16)


def render(template: str, *, user: str, level, server: str, level_name: str = "level", xp_name: str = "XP") -> str:
    """Fills the placeholders by plain replacement. Unknown {words} are left as typed."""
    out = template
    for k, v in (("user", user), ("level", level), ("server", server), ("level_name", level_name), ("xp_name", xp_name)):
        out = out.replace("{" + k + "}", str(v))
    return out[:1900]


def _parse(raw: Optional[str]) -> dict:
    try:
        data = json.loads(raw) if raw else {}
        return {k: data[k] for k in FIELDS if data.get(k) not in (None, "")} if isinstance(data, dict) else {}
    except Exception:
        logger.debug("swallowed exception in guild_brand._parse", exc_info=True)
        return {}


async def get(guild_id: int, clone_id: Optional[int] = None, db=None) -> dict:
    """The server's branding ({} when none). Cached; never raises."""
    try:
        ck = (guild_id, clone_id)
        hit = _cache.get(ck)
        now = time.monotonic()
        if hit and now - hit[1] < CACHE_TTL:
            return hit[0]
        if db is None:
            from database import db as _db
            db = _db
        data = _parse(await db.get_global_setting(_key(guild_id, clone_id)))
        if len(_cache) > 5000:
            _cache.clear()
        _cache[ck] = (data, now)
        return data
    except Exception:
        logger.debug("swallowed exception in guild_brand.get", exc_info=True)
        return {}


async def set_fields(guild_id: int, clone_id: Optional[int], changes: dict, db=None) -> bool:
    """Merges `changes` (None clears a field). True when saved."""
    try:
        if db is None:
            from database import db as _db
            db = _db
        current = dict(_parse(await db.get_global_setting(_key(guild_id, clone_id))))
        for k, v in changes.items():
            if k not in FIELDS:
                continue
            if v in (None, ""):
                current.pop(k, None)
            else:
                current[k] = v
        await db.set_global_setting(_key(guild_id, clone_id), json.dumps(current, separators=(",", ":")))
        _cache[(guild_id, clone_id)] = (current, time.monotonic())
        return True
    except Exception:
        logger.warning("[guild-brand] couldn't save for guild %s", guild_id, exc_info=True)
        return False


async def accent_int(guild_id: Optional[int], clone_id: Optional[int] = None) -> Optional[int]:
    if not guild_id:
        return None
    v = (await get(guild_id, clone_id)).get("accent")
    return v if isinstance(v, int) and 0 <= v <= 0xFFFFFF else None


async def accent_color(guild_id: Optional[int], clone_id: Optional[int], default):
    """A discord.Color for the server's accent, else `default`."""
    v = await accent_int(guild_id, clone_id)
    if v is None:
        return default
    import discord
    return discord.Color(v)


def level_word(brand: dict) -> str:
    return brand.get("level_name") or "level"


def xp_word(brand: dict) -> str:
    return brand.get("xp_name") or "XP"
