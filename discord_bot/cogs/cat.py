# path: discord_bot/cogs/cat.py

"""
Cute cat pictures: ONE slash command, /cat, plus the Cat button on the join DM.

Pictures come from The Cat API (thecatapi.com), with Cataas (cataas.com) as the
fallback. Both are free third-party services, so this module is built to be gentle
with them and to keep working when they are not:

  * Several API keys. CAT_API_KEYS (comma/space separated; CAT_API_KEY still works) holds
    one or more keys. Every key has its own per-minute budget (CAT_KEY_RPM, default 100,
    their limit is 120). Requests go to the key with the most room; a key that gets a 429
    is paused, and one that is rejected (401/403) is paused for an hour. Keys are never
    logged: only "key #2".
  * A shared pool. One request fetches up to 25 pictures (the maximum with a key) and they
    are handed out one at a time, so a few requests a minute can serve a lot of /cat uses.
    The pool tops itself up in the background. Without any key a request returns 1 picture,
    so the budget is tiny and the Cataas fallback does most of the work.
  * Only https URLs on the two services are ever shown, and the last good URLs are kept so
    a cat still appears if both services are down at once.

Premium servers get extras on the SAME command (so no new slash-command slot): a breed
option, up to 3 cats at once, and a shorter cooldown. Plain /cat stays free for everyone.

Deliberately ONE top-level slash command: Discord caps a bot at 100 GLOBAL slash commands
and this project has crash-looped at that ceiling before (see custom_role.py). setup()
catches CommandLimitReached: with no free slot /cat is skipped and logged, the bot still
starts, and the join DM Cat button (which only needs fetch_cat_urls) keeps working.
"""

import asyncio
import logging
import random
import re
import time
from collections import deque
from typing import List, Optional, Tuple
from urllib.parse import urlencode, urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import config
from database import db

logger = logging.getLogger(__name__)

CAT_API_URL = "https://api.thecatapi.com/v1/images/search"
CAT_BREEDS_URL = "https://api.thecatapi.com/v1/breeds"
CATAAS_URL = "https://cataas.com/cat?json=true"
ALLOWED_HOSTS = ("thecatapi.com", "cataas.com")

COOLDOWN_SECONDS = 5.0              # free
PREMIUM_COOLDOWN_SECONDS = 2.0
MAX_COUNT = 3                       # cats per /cat for Premium servers
BATCH = 25                          # pictures per request with a key (the API maximum)
ANON_RPM = 10                       # budget when no key is configured at all
POOL_MAX = 150
REFILL_BELOW = 10                   # top up in the background under this many
BREED_BATCH = 10
BREEDS_TTL = 24 * 3600
_TIMEOUT = aiohttp.ClientTimeout(total=6)
_BREED_ID_RE = re.compile(r"^[a-z0-9_]{2,12}$")

# Only used if the breeds list can't be fetched; the API answers with an empty list for a bad id.
FALLBACK_BREEDS = [("abys", "Abyssinian"), ("beng", "Bengal"), ("birm", "Birman"), ("bsho", "British Shorthair"),
                   ("mcoo", "Maine Coon"), ("norw", "Norwegian Forest Cat"), ("pers", "Persian"),
                   ("ragd", "Ragdoll"), ("siam", "Siamese"), ("sphy", "Sphynx")]

_general: deque = deque()           # shared pool of unseen pictures
_by_breed: dict = {}                # breed id -> deque (Premium only)
_recent: deque = deque(maxlen=50)   # last served URLs, the last-resort fallback
_last_use: dict = {}                # user id -> monotonic time of their last cat
_breeds_cache = {"at": 0.0, "items": []}
_lock_state = {"loop": None, "lock": None}
_topup_task = None
_last_exhausted_log = 0.0


# ───────────────────────────── helpers ─────────────────────────────

def safe_image_url(url) -> bool:
    """https only, and the host must be one of the two services (or a subdomain, e.g. cdn2.thecatapi.com)."""
    try:
        u = urlparse(str(url))
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


def cooldown_left(user_id: int, seconds: float = COOLDOWN_SECONDS) -> float:
    """Seconds this person still has to wait (0 means go ahead, and records the use)."""
    now = time.monotonic()
    left = seconds - (now - _last_use.get(user_id, -1e9))
    if left > 0:
        return left
    _last_use[user_id] = now
    if len(_last_use) > 5000:
        for k in [k for k, t in _last_use.items() if now - t > COOLDOWN_SECONDS]:
            _last_use.pop(k, None)
    return 0.0


def _lock() -> asyncio.Lock:
    """One refill at a time. Rebuilt if the event loop changed (tests start a fresh loop per call)."""
    loop = asyncio.get_running_loop()
    if _lock_state["loop"] is not loop:
        _lock_state.update(loop=loop, lock=asyncio.Lock())
    return _lock_state["lock"]


def reset_state() -> None:
    """Forget pools, caches and key budgets (used by tests)."""
    global _pool, _topup_task
    _general.clear(); _by_breed.clear(); _recent.clear(); _last_use.clear()
    _breeds_cache.update(at=0.0, items=[])
    _pool = None
    _topup_task = None


# ───────────────────────────── API keys ─────────────────────────────

class _Key:
    __slots__ = ("secret", "label", "rpm", "hits", "blocked_until")

    def __init__(self, secret, label, rpm):
        self.secret, self.label, self.rpm = secret, label, rpm
        self.hits = deque()
        self.blocked_until = 0.0

    def used(self, now) -> int:
        while self.hits and now - self.hits[0] >= 60:
            self.hits.popleft()
        return len(self.hits)

    def available(self, now) -> bool:
        return now >= self.blocked_until and self.used(now) < self.rpm


class KeyPool:
    """Hands out the key with the most room in its per-minute budget, and pauses keys that misbehave."""

    def __init__(self, secrets, rpm):
        self.signature = (tuple(secrets), rpm)
        self.keys = [_Key(s, f"key #{i + 1}", rpm) for i, s in enumerate(secrets)] or [_Key(None, "no key", ANON_RPM)]

    def acquire(self, now=None, exclude=()):
        now = time.monotonic() if now is None else now
        ok = [k for k in self.keys if k not in exclude and k.available(now)]
        if not ok:
            return None
        key = min(ok, key=lambda k: len(k.hits))
        key.hits.append(now)
        return key

    def block(self, key, seconds, now=None):
        now = time.monotonic() if now is None else now
        key.blocked_until = max(key.blocked_until, now + seconds)

    def stats(self, now=None) -> list:
        now = time.monotonic() if now is None else now
        return [{"key": k.label, "used_last_minute": k.used(now), "paused": now < k.blocked_until} for k in self.keys]


_pool: Optional[KeyPool] = None


def key_pool() -> KeyPool:
    global _pool
    secrets = tuple(getattr(config, "CAT_API_KEYS", ()) or ())
    if not secrets and getattr(config, "CAT_API_KEY", ""):
        secrets = (config.CAT_API_KEY,)
    rpm = int(getattr(config, "CAT_KEY_RPM", 100) or 100)
    if _pool is None or _pool.signature != (secrets, rpm):
        _pool = KeyPool(secrets, rpm)
    return _pool


def _retry_after(resp, default: float) -> float:
    try:
        v = float((getattr(resp, "headers", None) or {}).get("Retry-After", default))
    except (TypeError, ValueError):
        v = default
    return max(1.0, min(v, 3600.0))


def _note_exhausted(pool: KeyPool) -> None:
    global _last_exhausted_log
    now = time.monotonic()
    if now - _last_exhausted_log > 300:
        _last_exhausted_log = now
        logger.warning("[cat] every Cat API key is at its limit or paused (%s); using the fallback. "
                       "Add more keys to CAT_API_KEYS.", pool.stats())


async def _get_json(session, url, headers=None):
    async with session.get(url, headers=headers or {}, timeout=_TIMEOUT) as r:
        if r.status != 200:
            return None
        return await r.json(content_type=None)


async def _request_images(session, breed: Optional[str] = None) -> List[str]:
    """One batch from The Cat API, trying up to 3 different keys. [] if nothing usable came back."""
    pool = key_pool()
    tried: set = set()
    for _ in range(3):
        key = pool.acquire(exclude=tried)
        if key is None:
            _note_exhausted(pool)
            return []
        tried.add(key)
        if breed and not key.secret:
            return []                                    # breed filtering needs a key
        params = {"limit": (BREED_BATCH if breed else BATCH) if key.secret else 1}
        if breed:
            params["breed_ids"] = breed
        headers = {"x-api-key": key.secret} if key.secret else {}
        try:
            async with session.get(f"{CAT_API_URL}?{urlencode(params)}", headers=headers, timeout=_TIMEOUT) as r:
                if r.status == 429:
                    pool.block(key, _retry_after(r, 300))
                    logger.warning("[cat] %s hit its rate limit; pausing it", key.label)
                    continue
                if r.status in (401, 403):
                    pool.block(key, 3600)
                    logger.warning("[cat] %s was rejected (HTTP %s); pausing it for an hour", key.label, r.status)
                    continue
                if r.status != 200:
                    continue
                data = await r.json(content_type=None)
        except Exception:
            logger.warning("[cat] request with %s failed", key.label, exc_info=True)
            continue
        urls = [d.get("url") for d in data if isinstance(d, dict)] if isinstance(data, list) else []
        urls = [u for u in urls if safe_image_url(u)]
        if urls:
            return urls
    return []


async def _from_cataas(session) -> Optional[str]:
    try:
        data = await _get_json(session, CATAAS_URL)
    except Exception:
        logger.warning("[cat] cataas failed", exc_info=True)
        return None
    if not isinstance(data, dict):
        return None
    url = data.get("url")
    if not url:
        cat_id = data.get("id") or data.get("_id")
        if cat_id and str(cat_id).replace("-", "").replace("_", "").isalnum():
            url = f"https://cataas.com/cat/{cat_id}"
    return url if url and safe_image_url(url) else None


# ───────────────────────────── the picture pool ─────────────────────────────

def _extend(store: deque, urls) -> None:
    for u in urls:
        if u not in store and len(store) < POOL_MAX:
            store.append(u)


async def _fallback(session, n: int) -> List[str]:
    out: List[str] = []
    for _ in range(n):
        u = await _from_cataas(session)
        if u and u not in out:
            out.append(u)
    while len(out) < n and _recent:
        out.append(random.choice(list(_recent)))
    return out


async def _topup() -> None:
    try:
        async with aiohttp.ClientSession() as s:
            async with _lock():
                if len(_general) < REFILL_BELOW:
                    _extend(_general, await _request_images(s))
    except Exception:
        logger.warning("[cat] background top-up failed", exc_info=True)


def _schedule_topup() -> None:
    global _topup_task
    if _topup_task is None or _topup_task.done():
        _topup_task = asyncio.ensure_future(_topup())


async def fetch_cat_urls(count: int = 1, breed: Optional[str] = None, session=None) -> List[str]:
    """Up to `count` picture URLs (fewer, or none, if nothing is available). Never raises.
    With `breed` (a Cat API breed id) only that breed is returned, never a substitute."""
    count = max(1, min(int(count), MAX_COUNT))
    own = session is None
    if own:
        session = aiohttp.ClientSession()
    try:
        if breed:
            store = _by_breed.setdefault(breed, deque())
        else:
            store = _general
        if len(store) < count:
            async with _lock():
                if len(store) < count:                   # another request may have filled it while we waited
                    _extend(store, await _request_images(session, breed))
        out = [store.popleft() for _ in range(min(count, len(store)))]
        if not breed and len(out) < count:
            out += await _fallback(session, count - len(out))
        for u in out:
            _recent.append(u)
        if own and not breed and len(_general) < REFILL_BELOW:
            _schedule_topup()
        return out
    except Exception:
        logger.warning("[cat] fetch failed", exc_info=True)
        return []
    finally:
        if own:
            await session.close()


async def fetch_cat_url(session=None) -> Optional[str]:
    urls = await fetch_cat_urls(1, None, session)
    return urls[0] if urls else None


def pool_stats() -> dict:
    return {"pool": len(_general), "breed_pools": {k: len(v) for k, v in _by_breed.items()}, "keys": key_pool().stats()}


# ───────────────────────────── breeds (Premium) ─────────────────────────────

async def get_breeds(session=None) -> List[Tuple[str, str]]:
    """[(id, name)], cached for a day. Falls back to a short built-in list if the API can't be reached."""
    now = time.monotonic()
    if _breeds_cache["items"] and now - _breeds_cache["at"] < BREEDS_TTL:
        return _breeds_cache["items"]
    own = session is None
    if own:
        session = aiohttp.ClientSession()
    try:
        key = key_pool().acquire()
        if key is not None:
            data = await _get_json(session, CAT_BREEDS_URL, {"x-api-key": key.secret} if key.secret else None)
            items = []
            if isinstance(data, list):
                for b in data:
                    if isinstance(b, dict) and _BREED_ID_RE.match(str(b.get("id", ""))) and b.get("name"):
                        items.append((str(b["id"]), str(b["name"])[:100]))
            if items:
                _breeds_cache.update(at=now, items=sorted(items, key=lambda x: x[1].lower()))
                return _breeds_cache["items"]
    except Exception:
        logger.warning("[cat] breed list failed", exc_info=True)
    finally:
        if own:
            await session.close()
    return _breeds_cache["items"] or FALLBACK_BREEDS


def resolve_breed(text: str, breeds) -> Optional[Tuple[str, str]]:
    """Match what the person typed (an id, a full name, or an unambiguous start of one) to (id, name)."""
    t = (text or "").strip().lower()
    if not t:
        return None
    for i, n in breeds:
        if t == i or t == n.lower():
            return i, n
    starts = [(i, n) for i, n in breeds if n.lower().startswith(t)]
    return starts[0] if len(starts) == 1 else None


# ───────────────────────────── Discord side ─────────────────────────────

def cat_embed(url: str, title: Optional[str] = "🐱 Here's a cat", footer: bool = True) -> discord.Embed:
    embed = discord.Embed(title=title, color=discord.Color.from_rgb(255, 182, 193))
    embed.set_image(url=url)
    if footer:
        embed.set_footer(text="Pictures from thecatapi.com and cataas.com")
    return embed


async def is_premium(interaction: discord.Interaction) -> bool:
    gid = getattr(interaction, "guild_id", None)
    if not gid:
        return False
    try:
        return bool(await asyncio.wait_for(
            db.is_guild_premium_active(gid, getattr(interaction.client, "clone_id", None)), 2.5))
    except Exception:
        logger.warning("[cat] premium check failed for guild %s; treating as free", gid, exc_info=True)
        return False


async def send_cat(interaction: discord.Interaction, count: int = 1, breed: Optional[Tuple[str, str]] = None,
                   premium: bool = False) -> None:
    """Shared by /cat and the join DM Cat button. The caller has NOT responded yet.
    count > 1 and breed are only ever passed by /cat for Premium servers."""
    wait = cooldown_left(interaction.user.id, PREMIUM_COOLDOWN_SECONDS if premium else COOLDOWN_SECONDS)
    if wait > 0:
        await interaction.response.send_message(f"😺 Easy there, another cat in {wait:.0f}s.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    urls = await fetch_cat_urls(count, breed[0] if breed else None)
    if not urls:
        msg = (f"😿 I couldn't find a **{breed[1]}** right now. Try again in a moment." if breed
               else "😿 I couldn't find a cat right now. Try again in a moment.")
        await interaction.followup.send(msg, ephemeral=True)
        return
    title = f"🐱 {breed[1]}" if breed else ("🐱 Here are your cats" if len(urls) > 1 else "🐱 Here's a cat")
    embeds = [cat_embed(u, title if i == 0 else None, footer=(i == len(urls) - 1)) for i, u in enumerate(urls)]
    await interaction.followup.send(embeds=embeds)


PERK_NUDGE = ("✨ Picking a **breed** and getting **several cats at once** are perks for **Premium** servers "
              "(and Premium servers get a shorter cooldown too). A plain `/cat` is always free. "
              "Run `/start` to see Premium.")


async def breed_autocomplete(interaction: discord.Interaction, current: str):
    try:
        breeds = await asyncio.wait_for(get_breeds(), 2.0)
    except Exception:
        breeds = _breeds_cache["items"] or FALLBACK_BREEDS
    cur = (current or "").lower()
    hits = [(i, n) for i, n in breeds if cur in n.lower() or cur in i][:25]
    return [app_commands.Choice(name=n[:100], value=i) for i, n in hits]


class CatCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="cat", description="A random cute cat picture 🐱")
    @app_commands.describe(breed="Pick a breed (Premium servers)", count="How many cats, up to 3 (Premium servers)")
    @app_commands.autocomplete(breed=breed_autocomplete)
    async def cat(self, interaction: discord.Interaction, breed: Optional[str] = None,
                  count: app_commands.Range[int, 1, MAX_COUNT] = 1):
        premium = await is_premium(interaction)
        picked = None
        if breed or count > 1:
            if not premium:
                await interaction.response.send_message(PERK_NUDGE, ephemeral=True)
                return
            if breed:
                picked = resolve_breed(breed, await get_breeds())
                if picked is None:
                    await interaction.response.send_message(
                        "I don't know that breed. Start typing and pick one from the list.", ephemeral=True)
                    return
        await send_cat(interaction, count=count, breed=picked, premium=premium)


async def setup(bot):
    try:
        await bot.add_cog(CatCog(bot))
    except app_commands.CommandLimitReached:
        logger.warning("[cat] no global slash-command slot left: /cat skipped (the join DM Cat button still works)")
