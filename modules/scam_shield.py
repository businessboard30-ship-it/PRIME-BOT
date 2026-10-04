# path: modules/scam_shield.py

"""
Scam Shield — rules, matching and storage for the cross-server scam filter.

What it catches: the "MrBeast crypto-casino giveaway" family of spam that raid bots and hacked
accounts drop everywhere (fatowin.com, "promo code GIFT", fake "withdrawal success" screenshots).

Design rules (they matter for cost):
  * Matching is pure CPU on the bot host. Nothing here talks to the database per message.
  * The rules live in memory and are reloaded every few minutes, or straight away when the owner
    changes them in /admin (`invalidate()`). The database is only touched to reload rules and to
    record an actual catch.
  * Images are only downloaded/hashed when at least one image rule exists, and only small ones.

Rule kinds (table scam_shield_rules):
  word    case-insensitive text match, also on a "squashed" version so "fato win" / "f.a.t.o.w.i.n" match
  domain  a host name; matches the host itself and any subdomain of it
  image   64-bit difference hash (dHash) of a known scam screenshot, matched by Hamming distance
"""

from __future__ import annotations

import io
import logging
import re
import time
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

KINDS = ("word", "domain", "image")
CACHE_SECONDS = 300
IMAGE_MAX_BYTES = 2_000_000
IMAGE_MAX_DISTANCE = 8          # of 64 bits. Re-compressed / resized copies stay well under this
MIN_SQUASH_LEN = 6              # shorter words are never matched on the squashed text (false positives)
HIT_THROTTLE_SECONDS = 30       # one logged flag per user per window, so a raid can't flood the log
MAX_RULES = 500

_ZERO_WIDTH = re.compile("[\u200b-\u200f\u2060\ufeff\u00ad]")
_URL_HOST = re.compile(r"(?:https?://|www\.)([a-z0-9][a-z0-9.\-]*\.[a-z]{2,})", re.I)
_BARE_HOST = re.compile(r"\b([a-z0-9][a-z0-9\-]*(?:\.[a-z0-9\-]+)*\.[a-z]{2,})\b", re.I)

# Built-in heuristic (not stored, can't be removed): a "MrBeast" mention next to casino / bonus bait.
_BEAST = ("mrbeast", "mr beast", "mr.beast")
_BAIT = ("casino", "promo code", "bonus code", "withdraw", "withdrawal", "cryptocurrency casino", "free money")


async def _pool():
    from database import get_pool  # lazy: keeps this module importable in tests
    return await get_pool()


# ── normalisation / parsing ──────────────────────────────────────────────

def normalize(text: str) -> str:
    return _ZERO_WIDTH.sub("", (text or "")).lower()


def squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", normalize(text))


def hosts_in(text: str) -> Set[str]:
    t = normalize(text)
    found = {m.group(1).lower().rstrip(".") for m in _URL_HOST.finditer(t)}
    found |= {m.group(1).lower().rstrip(".") for m in _BARE_HOST.finditer(t)}
    return {h[4:] if h.startswith("www.") else h for h in found}


def parse_domain(text: str) -> Optional[str]:
    """'https://www.Fatowin.com/x' -> 'fatowin.com'. None if it isn't a host name."""
    raw = normalize(text).strip()
    raw = re.sub(r"^[a-z]+://", "", raw).split("/")[0].split("?")[0].split("#")[0].strip()
    raw = raw[4:] if raw.startswith("www.") else raw
    return raw if re.fullmatch(r"[a-z0-9][a-z0-9\-]*(\.[a-z0-9\-]+)*\.[a-z]{2,}", raw) else None


def classify(text: str) -> Tuple[str, str]:
    """What the owner typed in the 'add' box -> ('domain'|'word', cleaned pattern)."""
    t = (text or "").strip()
    dom = parse_domain(t) if (" " not in t and "." in t) else None
    return ("domain", dom) if dom else ("word", normalize(t).strip()[:100])


# ── image hashing ────────────────────────────────────────────────────────

def dhash(data: bytes) -> Optional[int]:
    """64-bit difference hash. None if the bytes aren't a readable image."""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            im.draft("L", (64, 64))
            g = im.convert("L").resize((9, 8), Image.LANCZOS)
            px = list(g.tobytes())          # 'L' mode: one byte per pixel
    except Exception:
        return None
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (1 if px[row * 9 + col] > px[row * 9 + col + 1] else 0)
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


# ── in-memory rule cache ─────────────────────────────────────────────────

class _Cache:
    loaded_at = 0.0
    enabled = True
    words: List[Tuple[int, str]] = []       # (rule id, normalised word)
    domains: List[Tuple[int, str]] = []
    images: List[Tuple[int, int]] = []      # (rule id, dhash)


_c = _Cache()
_last_hit: Dict[Tuple[int, int], float] = {}


def invalidate() -> None:
    _c.loaded_at = 0.0


def is_enabled() -> bool:
    return _c.enabled


def has_image_rules() -> bool:
    return bool(_c.images)


def stale() -> bool:
    return time.monotonic() - _c.loaded_at > CACHE_SECONDS


async def load(force: bool = False) -> None:
    """Reload rules + the on/off switch from the database (cheap: two small queries)."""
    if not force and not stale():
        return
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            rules = await conn.fetch("SELECT id, kind, pattern FROM scam_shield_rules")
            setting = await conn.fetchrow("SELECT value FROM scam_shield_settings WHERE key = 'enabled'")
    except Exception:
        logger.exception("[scam-shield] couldn't reload rules; keeping the ones in memory")
        _c.loaded_at = time.monotonic() - CACHE_SECONDS + 30     # retry in ~30s, don't hammer the DB
        return
    words, domains, images = [], [], []
    for r in rules:
        if r["kind"] == "word":
            words.append((r["id"], normalize(r["pattern"])))
        elif r["kind"] == "domain":
            domains.append((r["id"], r["pattern"].lower()))
        elif r["kind"] == "image":
            try:
                images.append((r["id"], int(r["pattern"], 16)))
            except ValueError:
                logger.warning("[scam-shield] bad image hash in rule %s", r["id"])
    _c.words, _c.domains, _c.images = words, domains, images
    _c.enabled = (setting is None) or (setting["value"] != "off")
    _c.loaded_at = time.monotonic()


# ── matching ─────────────────────────────────────────────────────────────

def is_allowed_domain(domain: str, allowed) -> bool:
    """True when `domain` is one of this server's allowed domains or a subdomain of one."""
    return any(domain == a or domain.endswith("." + a) for a in allowed or ())


def match_text(text: str, allowed=()) -> Optional[Tuple[str, str, Optional[int]]]:
    """Returns (kind, what matched, rule id) or None. Pure and fast. `allowed` is the
    server's own allowed-domain list: those domains never match (words/heuristics still do)."""
    if not text:
        return None
    t = normalize(text)
    sq = squash(text)
    for rid, w in _c.words:
        if not w:
            continue
        if w in t or (len(w) >= MIN_SQUASH_LEN and re.sub(r"[^a-z0-9]", "", w) in sq):
            return "word", w, rid
    if _c.domains:
        for h in hosts_in(text):
            for rid, d in _c.domains:
                if h == d or h.endswith("." + d):
                    if is_allowed_domain(h, allowed):
                        continue
                    return "domain", d, rid
    if any(b in t for b in _BEAST) and any(b in t for b in _BAIT):
        return "heuristic", "MrBeast + casino/bonus bait", None
    return None


def match_image(data: bytes) -> Optional[Tuple[str, str, Optional[int]]]:
    if not _c.images:
        return None
    h = dhash(data)
    if h is None:
        return None
    best = min(((hamming(h, ref), rid) for rid, ref in _c.images), default=None)
    if best and best[0] <= IMAGE_MAX_DISTANCE:
        return "image", f"known scam image (distance {best[0]})", best[1]
    return None


def throttled(guild_id: int, user_id: int) -> bool:
    """True if we already logged a hit for this user a moment ago (the delete still happens)."""
    now = time.monotonic()
    key = (guild_id, user_id)
    if len(_last_hit) > 2000:
        for k in [k for k, v in _last_hit.items() if now - v > HIT_THROTTLE_SECONDS]:
            _last_hit.pop(k, None)
    if now - _last_hit.get(key, -1e9) < HIT_THROTTLE_SECONDS:
        return True
    _last_hit[key] = now
    return False


# ── storage (only used by the panel and when something is caught) ────────

async def list_rules() -> List[dict]:
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, kind, pattern, note, created_at FROM scam_shield_rules ORDER BY id")
    return [dict(r) for r in rows]


async def add_rule(kind: str, pattern: str, added_by: int, note: Optional[str] = None) -> Optional[int]:
    """Returns the new rule id, or None if it already existed / the list is full."""
    if kind not in KINDS or not pattern:
        return None
    pool = await _pool()
    async with pool.acquire() as conn:
        if int(await conn.fetchval("SELECT COUNT(*) FROM scam_shield_rules") or 0) >= MAX_RULES:
            return None
        rid = await conn.fetchval(
            "INSERT INTO scam_shield_rules (kind, pattern, added_by, note) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (kind, pattern) DO NOTHING RETURNING id", kind, pattern, added_by, (note or "")[:200] or None)
    invalidate()
    return rid


async def remove_rule(rule_id: int) -> bool:
    pool = await _pool()
    async with pool.acquire() as conn:
        n = await conn.fetchval("DELETE FROM scam_shield_rules WHERE id = $1 RETURNING id", rule_id)
    invalidate()
    return n is not None


async def set_enabled(on: bool) -> None:
    pool = await _pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO scam_shield_settings (key, value) VALUES ('enabled', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", "on" if on else "off")
    invalidate()


async def log_hit(guild_id: int, channel_id: int, user_id: int, kind: str, matched: str,
                  rule_id: Optional[int], snippet: str, deleted: bool, clone_id: Optional[int] = None) -> None:
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO scam_shield_hits (guild_id, clone_id, channel_id, user_id, kind, matched, rule_id, "
                "snippet, deleted) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
                guild_id, clone_id, channel_id, user_id, kind, matched[:200], rule_id, snippet[:300], deleted)
    except Exception:
        logger.exception("[scam-shield] couldn't record a hit")


async def recent_hits(limit: int = 10) -> List[dict]:
    pool = await _pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT guild_id, user_id, kind, matched, deleted, created_at FROM scam_shield_hits "
            "ORDER BY id DESC LIMIT $1", limit)
        total = int(await conn.fetchval("SELECT COUNT(*) FROM scam_shield_hits") or 0)
    return [dict(r, _total=total) for r in rows] if rows else []


async def hit_total() -> int:
    pool = await _pool()
    async with pool.acquire() as conn:
        return int(await conn.fetchval("SELECT COUNT(*) FROM scam_shield_hits") or 0)


# ── per-server settings (server panel phase 9) ───────────────────────────
# Each server can switch Scam Shield off for itself and allow specific domains. Read only AFTER a
# message already matched (the rare path), and cached, so a clean message still costs no database call.

GUILD_CACHE_SECONDS = 300
MAX_ALLOWED_DOMAINS = 50
_DEFAULT_GUILD = {"enabled": True, "allowed_domains": ()}
_guild_cache: Dict[Tuple[int, Optional[int]], Tuple[float, dict]] = {}


def invalidate_guild(guild_id: int, clone_id: Optional[int] = None) -> None:
    _guild_cache.pop((guild_id, clone_id), None)


async def guild_settings(guild_id: int, clone_id: Optional[int] = None) -> dict:
    """{'enabled': bool, 'allowed_domains': tuple}. Falls back to the defaults (on, nothing allowed)
    if the database can't be read, so a failure never switches protection off."""
    key = (guild_id, clone_id)
    now = time.monotonic()
    hit = _guild_cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT enabled, allowed_domains FROM discord_scam_shield_guild "
                "WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2", guild_id, clone_id)
        value = ({"enabled": bool(row["enabled"]),
                  "allowed_domains": tuple(d.lower() for d in (row["allowed_domains"] or []))}
                 if row else dict(_DEFAULT_GUILD))
        ttl = GUILD_CACHE_SECONDS
    except Exception:
        logger.debug("[scam-shield] couldn't read server settings", exc_info=True)
        value, ttl = dict(_DEFAULT_GUILD), 30
    if len(_guild_cache) > 5000:
        _guild_cache.clear()
    _guild_cache[key] = (now + ttl, value)
    return value


async def guild_hit_count(guild_id: int, clone_id: Optional[int] = None) -> int:
    """How many scam messages were caught in this server (all time)."""
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            return int(await conn.fetchval(
                "SELECT COUNT(*) FROM scam_shield_hits WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2",
                guild_id, clone_id) or 0)
    except Exception:
        logger.debug("[scam-shield] couldn't count server hits", exc_info=True)
        return 0
