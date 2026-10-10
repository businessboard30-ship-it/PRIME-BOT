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


# Second built-in heuristic: the "fake giveaway post" wording used by the fatawin variant of the scam
# (giveaway + promo code + register/withdraw/casino), and its "this post will be deleted" bait.
_GIVEAWAY = ("giving away", "giveaway")
_PROMO = ("promo code", "promocode", "promo-code")
_CLAIM = ("register", "withdraw", "casino")
_DELETED_BAIT = ("will be deleted an hour", "post will be deleted")

# Starter rules added once per database (see _seed_defaults). They land in scam_shield_rules, so the
# owner can still list and remove them in /admin; a removed rule stays removed.
SEED_KEY = "seed_fatawin_2026_10"
SEED_NOTE = "fatawin fake MrBeast giveaway"
SEED_RULES = (
    ("domain", "fatawin.com"),
    ("word", "fatawin"),
    ("image", "37717a7c733abb3b"),   # fake MrBeast pinned post, bonuses page
    ("image", "5c9f69a7a939aea9"),   # fake withdrawal page
    ("image", "9b3b39242323278b"),   # "Withdrawal Success! $5600" popup
    ("image", "800021140c6d19f3"),   # popup next to a phone showing +5600 USDT
)

# Second batch: the "Drake" variant of the same scam (dwinble.com, promo code DRAKE, fake $3,200 withdrawal).
# Each batch has its own marker, so servers that already ran the first one still get this one exactly once.
SEED_DWINBLE_KEY = "seed_dwinble_2026_10"
SEED_DWINBLE_NOTE = "dwinble fake Drake giveaway"
SEED_DWINBLE_RULES = (
    ("domain", "dwinble.com"),
    ("word", "dwinble"),
    ("image", "69b0e0c88fe4a755"),   # "Withdrawal accepted $3,200" popup over the casino lobby
    ("image", "991a129192831114"),   # popup + "@Drake Thank you!" next to a phone showing +3,200 USDT
    ("image", "00133a4438380e3f"),   # promo-code page with DRAKE typed in
    ("image", "64647d3e3bb53b33"),   # fake Drake post on X pushing dwinble.com
)

SEED_BATCHES = (
    (SEED_KEY, SEED_NOTE, SEED_RULES),
    (SEED_DWINBLE_KEY, SEED_DWINBLE_NOTE, SEED_DWINBLE_RULES),
)

# One-time look back through recent history after the dwinble rules are seeded, to remove copies that were
# posted before the bot knew about them. Marker is per bot (main or clone) and written only when it finishes.
BACKFILL_KEY = "backfill_" + SEED_DWINBLE_KEY
BACKFILL_DAYS = 14               # the scam wave is recent; older history is not touched
BACKFILL_PER_CHANNEL = 300       # newest messages looked at per channel
BACKFILL_CHANNEL_PAUSE = 1.0     # seconds between channels, so the sweep never competes with live traffic


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
    strict: Set[Tuple[int, Optional[int]]] = set()   # (guild id, clone id) with Strict mode switched on


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
            await _seed_defaults(conn)
            rules = await conn.fetch("SELECT id, kind, pattern FROM scam_shield_rules")
            setting = await conn.fetchrow("SELECT value FROM scam_shield_settings WHERE key = 'enabled'")
            strict_rows = await conn.fetch(
                "SELECT key FROM scam_shield_settings WHERE key LIKE $1", STRICT_PREFIX + "%")
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
    _c.strict = {k for k in (_parse_strict_key(r["key"]) for r in strict_rows) if k}
    _c.enabled = (setting is None) or (setting["value"] != "off")
    _c.loaded_at = time.monotonic()


async def _seed_defaults(conn) -> None:
    """Insert each seed batch once (marked in scam_shield_settings), so removing a rule in /admin sticks."""
    for key, note, rules in SEED_BATCHES:
        try:
            if await conn.fetchval("SELECT 1 FROM scam_shield_settings WHERE key = $1", key):
                continue
            for kind, pattern in rules:
                await conn.execute(
                    "INSERT INTO scam_shield_rules (kind, pattern, note) VALUES ($1, $2, $3) "
                    "ON CONFLICT (kind, pattern) DO NOTHING", kind, pattern, note)
            await conn.execute(
                "INSERT INTO scam_shield_settings (key, value) VALUES ($1, 'done') ON CONFLICT (key) DO NOTHING", key)
        except Exception:
            logger.exception("[scam-shield] couldn't seed the default rules (%s); will retry on the next reload", key)


# ── matching ─────────────────────────────────────────────────────────────

def is_allowed_domain(domain: str, allowed) -> bool:
    """True when `domain` is one of this server's allowed domains or a subdomain of one."""
    return any(domain == a or domain.endswith("." + a) for a in allowed or ())


# Discord's own domains (and its attachment CDN) can never be scam bait, so a domain rule that happens to
# cover them (an owner typing "discord.com" into the add box, a rule on a parent domain, ...) must not
# delete normal messages. Invite and gift links (discord.gg / discord.gift) are deliberately NOT listed.
OFFICIAL_DOMAINS = ("discord.com", "discordapp.com", "discordapp.net", "discord.media", "discordstatus.com",
                    "discord.dev", "discord.new")


def is_official_host(host: str, t: str = "") -> bool:
    """True for a real Discord host. `t` is the normalized message: a host followed by '@'
    (https://discord.com@evil.xyz/) is the classic look-alike trick, so that is never trusted."""
    h = (host or "").lower()
    if not any(h == d or h.endswith("." + d) for d in OFFICIAL_DOMAINS):
        return False
    return not re.search(r"(?:https?://|www\.)" + re.escape(h) + r"[^\s/]*@", t or "")


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
                    if is_allowed_domain(h, allowed) or is_official_host(h, t):
                        continue
                    return "domain", d, rid
    if any(b in t for b in _BEAST) and any(b in t for b in _BAIT):
        return "heuristic", "MrBeast + casino/bonus bait", None
    has_promo = any(b in t for b in _PROMO)
    if has_promo and any(b in t for b in _GIVEAWAY) and any(b in t for b in _CLAIM):
        return "heuristic", "fake giveaway + promo code bait", None
    if has_promo and any(b in t for b in _DELETED_BAIT):
        return "heuristic", "promo code + 'post will be deleted' bait", None
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


# ── strict mode (premium) ─────────────────────────────────────────────────
# Extra, more aggressive checks a server can switch on for itself. Off by default, never touches the
# normal rules, and only runs for servers in _c.strict (an in-memory set, so a normal server costs nothing).
# Premium is checked by the caller on the rare path (a strict check actually matched), never per message.

STRICT_PREFIX = "strict:"

# Brands scammers imitate, with the real domains. A link is only suspicious when the *host* is a look-alike.
_BRAND_REAL = {
    "discord": ("discord.com", "discordapp.com", "discordapp.net", "discord.gg", "discord.gift", "discord.media",
                "discordstatus.com", "discord.dev", "discord.new"),
    "steam": ("steampowered.com", "steamcommunity.com", "steamstatic.com", "steam.com"),
    "paypal": ("paypal.com", "paypal.me"),
    "binance": ("binance.com",),
    "metamask": ("metamask.io",),
    "coinbase": ("coinbase.com",),
    "roblox": ("roblox.com",),
}
_REAL_DOMAINS = tuple(d for ds in _BRAND_REAL.values() for d in ds)
_BAIT_WORDS = r"(?:gift|nitro|free|promo|airdrop|claim|reward|drop|giveaway|bonus)"
_BRAND_NAMES = "|".join(_BRAND_REAL)
_HOST_BAIT = re.compile(rf"(?:{_BRAND_NAMES})[-.]?{_BAIT_WORDS}|{_BAIT_WORDS}[-.]?(?:{_BRAND_NAMES})")
# Phrases that are only treated as scam bait when the same message also carries a link.
_STRICT_BAIT = ("free nitro", "nitro gift", "discord nitro for free", "claim your reward", "claim your prize",
                "airdrop", "seed phrase", "recovery phrase", "connect your wallet", "verify your wallet",
                "steam gift", "free steam")


def strict_key(guild_id: int, clone_id: Optional[int] = None) -> str:
    return f"{STRICT_PREFIX}{clone_id if clone_id is not None else 'main'}:{guild_id}"


def _parse_strict_key(key: str) -> Optional[Tuple[int, Optional[int]]]:
    try:
        who, gid = key[len(STRICT_PREFIX):].split(":")
        return int(gid), (None if who == "main" else int(who))
    except (ValueError, TypeError):
        return None


def strict_on(guild_id: int, clone_id: Optional[int] = None) -> bool:
    return (guild_id, clone_id) in _c.strict


async def set_strict(guild_id: int, clone_id: Optional[int], on: bool) -> None:
    pool = await _pool()
    async with pool.acquire() as conn:
        if on:
            await conn.execute("INSERT INTO scam_shield_settings (key, value) VALUES ($1, 'on') "
                               "ON CONFLICT (key) DO NOTHING", strict_key(guild_id, clone_id))
        else:
            await conn.execute("DELETE FROM scam_shield_settings WHERE key = $1", strict_key(guild_id, clone_id))
    (_c.strict.add if on else _c.strict.discard)((guild_id, clone_id))


def match_strict(text: str, allowed=()) -> Optional[Tuple[str, str, Optional[int]]]:
    """Strict-mode checks. Only looks at explicit links (http(s):// or www.), never bare words like
    'index.html'. Returns ('strict', why, None) or None. Real Discord / allowed hosts are never flagged."""
    if not text:
        return None
    t = normalize(text)
    hosts = {m.group(1).lower().rstrip(".") for m in _URL_HOST.finditer(t)}
    hosts = {h[4:] if h.startswith("www.") else h for h in hosts}
    hosts = {h for h in hosts if not is_allowed_domain(h, allowed) and not is_official_host(h, t)}
    for h in sorted(hosts):
        if re.search(r"(?:https?://|www\.)" + re.escape(h) + r"[^\s/]*@", t):
            return "strict", "link hides its real address behind '@'", None     # https://discord.com@evil.xyz
        if any(h == d or h.endswith("." + d) for d in _REAL_DOMAINS):
            continue                                              # a genuine brand domain
        for d in _REAL_DOMAINS:
            if (d + ".") in h:                                    # discord.com.verify-login.xyz
                return "strict", f"fake subdomain of {d}", None
        if _HOST_BAIT.search(h):
            return "strict", f"look-alike link ({h})", None
    if hosts:
        for phrase in _STRICT_BAIT:
            if phrase in t:
                return "strict", f"'{phrase}' bait with a link", None
    return None


def check_text(text: str, allowed=(), strict: bool = False) -> Optional[Tuple[str, str, Optional[int]]]:
    """What Scam Shield would do with this text right now: the normal rules, then strict mode if asked."""
    return match_text(text, allowed) or (match_strict(text, allowed) if strict else None)


# ── reports (premium: deep report; free: recent catches) ──────────────────

async def recent_guild_hits(guild_id: int, clone_id: Optional[int] = None, limit: int = 5) -> List[dict]:
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id, channel_id, kind, matched, deleted, created_at FROM scam_shield_hits "
                "WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 ORDER BY id DESC LIMIT $3",
                guild_id, clone_id, limit)
        return [dict(r) for r in rows]
    except Exception:
        logger.debug("[scam-shield] couldn't read recent hits", exc_info=True)
        return []


async def deep_report(guild_id: int, clone_id: Optional[int] = None, days: int = 30) -> dict:
    """30-day breakdown for one server: totals, what matched, where, who, and a per-day trend.
    {} if the database can't be read. Logged hits are throttled (one per user per 30s), so the numbers
    are a floor, not an exact count of every deleted message."""
    base = ("FROM scam_shield_hits WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 "
            "AND created_at >= NOW() - ($3 * INTERVAL '1 day')")
    args = (guild_id, clone_id, days)
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            head = await conn.fetchrow(
                "SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE deleted) AS deleted, "
                "COUNT(DISTINCT user_id) AS users, MAX(created_at) AS last " + base, *args)
            kinds = await conn.fetch(f"SELECT kind, COUNT(*) AS n {base} GROUP BY kind ORDER BY n DESC LIMIT 5", *args)
            matched = await conn.fetch(
                f"SELECT matched, COUNT(*) AS n {base} GROUP BY matched ORDER BY n DESC LIMIT 5", *args)
            channels = await conn.fetch(
                f"SELECT channel_id, COUNT(*) AS n {base} GROUP BY channel_id ORDER BY n DESC LIMIT 3", *args)
            users = await conn.fetch(f"SELECT user_id, COUNT(*) AS n {base} GROUP BY user_id ORDER BY n DESC LIMIT 3", *args)
            daily = await conn.fetch(
                f"SELECT created_at::date AS d, COUNT(*) AS n {base} GROUP BY 1 ORDER BY 1", *args)
    except Exception:
        logger.debug("[scam-shield] couldn't build the deep report", exc_info=True)
        return {}
    return {"days": days, "total": int(head["total"] or 0), "deleted": int(head["deleted"] or 0),
            "users": int(head["users"] or 0), "last": head["last"],
            "kinds": [(r["kind"], r["n"]) for r in kinds], "matched": [(r["matched"], r["n"]) for r in matched],
            "channels": [(r["channel_id"], r["n"]) for r in channels], "users_top": [(r["user_id"], r["n"]) for r in users],
            "daily": [(r["d"], r["n"]) for r in daily]}


# ── one-time history sweep marker ─────────────────────────────────────────

def backfill_marker(clone_id: Optional[int] = None) -> str:
    """Settings key for 'this bot already did the sweep'. One per bot, so clones each sweep their own servers."""
    return f"{BACKFILL_KEY}:{clone_id if clone_id is not None else 'main'}"


async def backfill_done(clone_id: Optional[int] = None) -> bool:
    """True if the sweep already finished. If the database can't be read we say True: better to skip a sweep
    than to repeat one on every restart."""
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            return bool(await conn.fetchval(
                "SELECT 1 FROM scam_shield_settings WHERE key = $1", backfill_marker(clone_id)))
    except Exception:
        logger.exception("[scam-shield] couldn't read the sweep marker; skipping the sweep this start")
        return True


async def seed_done(key: str) -> bool:
    """True once a seed batch has been inserted (its marker exists). The sweep waits for this so it never
    spends its one run on a rule set that is missing the new rules."""
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            return bool(await conn.fetchval("SELECT 1 FROM scam_shield_settings WHERE key = $1", key))
    except Exception:
        logger.exception("[scam-shield] couldn't check the seed marker")
        return False


async def mark_backfill_done(clone_id: Optional[int] = None) -> None:
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO scam_shield_settings (key, value) VALUES ($1, 'done') ON CONFLICT (key) DO NOTHING",
                backfill_marker(clone_id))
    except Exception:
        logger.exception("[scam-shield] couldn't save the sweep marker (it may run again on the next start)")


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
