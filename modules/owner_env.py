"""Shared settings that live in the database instead of the Railway Variables tab.

Why: every bot (main, clone manager, API, each clone) reads the same keys from the environment, so changing one meant
editing Railway and redeploying. Now the owner fills them in once, through the V2 form the main bot DMs
(`/admin envsetup`, discord_bot/cogs/owner_env.py), and every process picks them up on its next start.

How it works
- Values are stored in `bot_global_settings` under `env:<NAME>`. Secrets are encrypted with the same SecretManager that
  protects clone bot tokens (ENCRYPTION_KEY), prefixed `enc:`.
- `bootstrap()` runs from config.py right after load_dotenv(), BEFORE anything reads the environment. It copies the
  saved values into os.environ. A value saved here wins over the Railway one (that is the point: Railway becomes the
  fallback). `Reset` in the form deletes the saved value and the Railway one applies again at the next start.
- It never touches the keys that are needed to reach the database or decrypt (DATABASE_URL, ENCRYPTION_KEY, the bot
  tokens, ADMIN_ID): those stay on Railway. Any failure here is swallowed, so a database hiccup can never stop a bot
  from starting; it just falls back to the Railway values.

This module must not import config (config imports it).
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("owner_env")

PREFIX = "env:"
PREV = "envprev:"      # the value a key had before its last change (one step of undo)
LOG_KEY = "envlog"     # last changes, names only, never values
PREV_NAMES: set = set()  # names that currently have a previous value, filled by load_all()
ENC = "enc:"
MAX_LEN = 400
PAGE = 5   # a Discord modal holds at most 5 fields

# Never managed here: needed to reach the DB / decrypt, or identify the bot itself.
LOCKED = frozenset({
    "DATABASE_URL", "ENCRYPTION_KEY", "DISCORD_BOT_TOKEN", "SINOBANED2_BOT_TOKEN", "ADMIN_ID",
    "PORT", "DATA_DIR", "RAILWAY_SERVICE_NAME", "SERVICE_ROLE",
})


class Spec:
    __slots__ = ("name", "label", "group", "kind", "secret")

    def __init__(self, name: str, label: str, group: str, kind: str = "str", secret: bool = False):
        self.name, self.label, self.group, self.kind, self.secret = name, label, group, kind, secret


def _build() -> List[Spec]:
    s: List[Spec] = []

    def add(group, items):
        for it in items:
            name, label = it[0], it[1]
            kind = it[2] if len(it) > 2 else "str"
            secret = it[3] if len(it) > 3 else False
            s.append(Spec(name, label, group, kind, secret))

    add("Paystack", [
        ("PAYSTACK_SECRET_KEY", "Paystack secret key", "str", True),
        ("PAYSTACK_PUBLIC_KEY", "Paystack public key"),
        ("PAYSTACK_PLAN_CARD_PLAN", "Plan code: card plan"),
        ("PAYSTACK_PLAN_DEV_MONTHLY", "Plan code: dev monthly"),
        ("PAYSTACK_PLAN_DEV_YEARLY", "Plan code: dev yearly"),
        ("PAYSTACK_PLAN_ANIME_PLUS", "Plan code: anime plus"),
    ])
    add("Gumroad keys", [
        ("GUMROAD_ACCESS_TOKEN", "Gumroad access token", "str", True),
        ("GUMROAD_WEBHOOK_SECRET", "Gumroad webhook secret", "str", True),
    ])
    links = [("XP_BOOST_3", "XP boost 3"), ("XP_BOOST_5", "XP boost 5"), ("PREMIUM", "Premium"),
             ("PREMIUM_YEARLY", "Premium yearly"), ("PREMIUM_LIFETIME", "Premium lifetime"),
             ("PREMIUM_PLUS", "Premium plus"), ("PREMIUM_PLUS_YEARLY", "Premium plus yearly"),
             ("HARDCORE_ROAST", "Hardcore roast"), ("AD_PLACEMENT", "Ad placement"), ("CARD_PLAN", "Card plan"),
             ("LEVEL_PASS", "Level pass"), ("DEV_MONTHLY", "Dev monthly"), ("DEV_YEARLY", "Dev yearly"),
             ("ANIME_PLUS", "Anime plus")]
    add("Gumroad links", [(f"GUMROAD_{k}_LINK", f"Link: {lab}", "url") for k, lab in links])
    add("Gumroad product IDs", [
        (f"GUMROAD_{'PREMIUM_PRODUCT' if k == 'PREMIUM' else k}_ID", f"ID: {lab}") for k, lab in links])
    add("AI keys", [
        ("GROQ_API_KEY", "Groq key", "str", True),
        ("GROQ_API_KEY_BACKUP", "Groq key (backup)", "str", True),
        ("GROQ_API_KEY_BACKUP2", "Groq key (backup 2)", "str", True),
        ("OPENAI_API_KEY", "OpenAI key", "str", True),
        ("GEMINI_API_KEY", "Gemini key", "str", True),
        ("ANTHROPIC_API_KEY", "Anthropic key", "str", True),
        ("FAL_API_KEY", "Fal key", "str", True),
        ("YOUTUBE_API_KEY", "YouTube key", "str", True),
        ("CAT_API_KEY", "Cat API key", "str", True),
    ])
    add("Bot lists", [
        ("TOPGG_TOKEN", "top.gg token", "str", True),
        ("TOPGG_WEBHOOK_SECRET", "top.gg webhook secret", "str", True),
        ("TOPGG_BOT_ID", "top.gg bot ID"),
        ("DTHINGS_API_KEY", "Discord Things key", "str", True),
        ("DTHINGS_BOT_ID", "Discord Things bot ID"),
    ])
    add("Security and logins", [
        ("TURNSTILE_SECRET_KEY", "Turnstile secret", "str", True),
        ("VERIFY_SIGNING_SECRET", "Verify signing secret", "str", True),
        ("CRON_SECRET", "Cron secret", "str", True),
        ("DISCORD_OAUTH_CLIENT_SECRET", "Discord OAuth secret", "str", True),
        ("GITHUB_OAUTH_CLIENT_ID", "GitHub OAuth client ID"),
        ("GITHUB_OAUTH_CLIENT_SECRET", "GitHub OAuth secret", "str", True),
        ("GOOGLE_CLIENT_ID", "Google client ID"),
        ("GOOGLE_CLIENT_SECRET", "Google client secret", "str", True),
    ])
    add("Sites and links", [
        ("WEBSITE_URL", "Website URL", "url"),
        ("DASHBOARD_BASE_URL", "Dashboard URL", "url"),
        ("NEXT_PUBLIC_SITE_URL", "Public site URL", "url"),
        ("NEXT_PUBLIC_SUPPORT_SERVER_INVITE", "Support server invite", "url"),
        ("SETUP_VIDEO_URL", "Setup video URL", "url"),
        ("UNLOCK_PAGES_BASE_URL", "Unlock pages URL", "url"),
        ("TURNSTILE_PAGES_URL", "Turnstile pages URL", "url"),
        ("GUILDATLAS_SITE_URL", "GuildAtlas site URL", "url"),
        ("GOOGLE_REDIRECT_URI", "Google redirect URI", "url"),
    ])
    add("Channels and IDs", [
        ("DISCORD_SUPPORT_SERVER_ID", "Support server ID", "int"),
        ("IMAGE_HOST_CHANNEL_ID", "Image host channel ID", "int"),
        ("DEV_STORAGE_CHANNEL_ID", "Dev storage channel ID", "int"),
        ("TRACE_CHANNEL_ID", "Trace channel ID", "int"),
        ("STORE_ADMIN_ROLE_ID", "Store admin role ID", "int"),
    ])
    add("Fees and timing", [
        ("ANIME_PLUS_FEE_USD", "Anime plus fee (USD)", "float"),
        ("AD_PLACEMENT_FEE_USD", "Ad placement fee (USD)", "float"),
        ("DISGUILD_BOOST_7D_FEE_USD", "Boost 7 day fee (USD)", "float"),
        ("DISGUILD_BOOST_30D_FEE_USD", "Boost 30 day fee (USD)", "float"),
        ("DISGUILD_DONATE_FEE_USD", "Donate fee (USD)", "float"),
        ("TOPGG_VOTE_MULTIPLIER", "Vote XP multiplier", "float"),
        ("TOPGG_VOTE_HOURS", "Vote boost hours", "float"),
        ("USD_GHS_FALLBACK_RATE", "USD to GHS fallback rate", "float"),
        ("BOT_PROFILE_VOTE_GATE", "Vote gate on bot profile (1/0)", "bool"),
        ("CATCH_SUPPORT_SERVER_ONLY", "Catch only in support server (1/0)", "bool"),
    ])
    return [x for x in s if x.name not in LOCKED]


SPECS: List[Spec] = _build()
BY_NAME: Dict[str, Spec] = {x.name: x for x in SPECS}
GROUPS: List[str] = list(dict.fromkeys(x.group for x in SPECS))


def group_specs(group: str) -> List[Spec]:
    return [x for x in SPECS if x.group == group]


def pages(group: str) -> List[List[Spec]]:
    items = group_specs(group)
    return [items[i:i + PAGE] for i in range(0, len(items), PAGE)]


# ── validation ──────────────────────────────────────────────────────────

def clean(spec: Spec, raw: str) -> Tuple[bool, str]:
    """(ok, value_or_reason). Strips whitespace and wrapping quotes, rejects anything that would break a header or URL."""
    v = (raw or "").strip().strip("\"'").strip()
    if not v:
        return False, "empty"
    if len(v) > MAX_LEN:
        return False, f"longer than {MAX_LEN} characters"
    if any(c in v for c in "\r\n\t\x00") or (spec.kind != "str" and " " in v):
        return False, "contains spaces or line breaks"
    if spec.kind == "url":
        v = v.rstrip("/") if spec.name.endswith(("_BASE_URL", "WEBSITE_URL", "SITE_URL")) else v
        if not v.startswith("https://") or any(c in v for c in "\"'<>"):
            return False, "must be a full https:// link"
    elif spec.kind == "int":
        try:
            int(v)
        except ValueError:
            return False, "must be a whole number"
    elif spec.kind == "float":
        try:
            float(v)
        except ValueError:
            return False, "must be a number"
    elif spec.kind == "bool":
        low = v.lower()
        if low in ("1", "true", "yes", "on"):
            v = "1"
        elif low in ("0", "false", "no", "off"):
            v = "0"
        else:
            return False, "use 1 or 0"
    return True, v


def shown(spec: Spec, value: Optional[str]) -> str:
    """What the form may display. Secrets are never echoed, not even their last characters."""
    if value is None or value == "":
        return "empty"
    if spec.secret:
        return "set (hidden)"
    return value if len(value) <= 38 else value[:35] + "..."


# ── encryption ──────────────────────────────────────────────────────────

_FERNET = None


def _cipher():
    """Same cipher as utils.crypto.SecretManager (Fernet keyed by ENCRYPTION_KEY), built directly: importing
    utils.crypto would run utils/__init__.py, which imports config, which is still loading when bootstrap() runs."""
    global _FERNET
    if _FERNET is None:
        from cryptography.fernet import Fernet
        key = os.getenv("ENCRYPTION_KEY")
        if not key:
            raise RuntimeError("ENCRYPTION_KEY is not set")
        _FERNET = Fernet(key.encode())
    return _FERNET


def pack(spec: Spec, value: str) -> str:
    return ENC + _cipher().encrypt(value.encode()).decode() if spec.secret else value


def unpack(spec: Spec, stored: Optional[str]) -> Optional[str]:
    if stored is None:
        return None
    if stored.startswith(ENC):
        return _cipher().decrypt(stored[len(ENC):].encode()).decode()
    return stored


# ── early boot (sync, before config reads the environment) ──────────────

APPLIED: Dict[str, str] = {}      # name -> value this process started with, for values that came from the database
RAILWAY_VALUE: Dict[str, str] = {}   # the Railway value of each key, kept so a live reset can put it back
RAILWAY_HAD = set()               # names that were already set in the environment before the database values went in
_done = False


async def _fetch_rows(url: str) -> List[Tuple[str, str]]:
    import asyncpg
    conn = await asyncpg.connect(url, timeout=6, statement_cache_size=0, command_timeout=8)
    try:
        rows = await conn.fetch("SELECT key, value FROM bot_global_settings WHERE key LIKE 'env:%'")
        return [(r["key"], r["value"]) for r in rows]
    finally:
        await conn.close()


def bootstrap() -> int:
    """Copy saved values into os.environ. Idempotent, never raises. Returns how many were applied."""
    global _done
    if _done:
        return len(APPLIED)
    _done = True
    if os.getenv("OWNER_ENV_DISABLE", "").strip().lower() in ("1", "true", "yes"):
        return 0
    url = os.getenv("DATABASE_URL", "")
    if not url:
        return 0
    for name in BY_NAME:
        if os.environ.get(name):
            RAILWAY_HAD.add(name)
            RAILWAY_VALUE[name] = os.environ[name]
    box: Dict[str, object] = {}

    def run():   # its own thread + loop: config can be imported while another loop is already running
        try:
            box["rows"] = asyncio.run(_fetch_rows(url))
        except Exception as e:  # table not created yet on a brand new database, network blip, ...
            box["err"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(10)
    if "rows" not in box:
        logger.warning("[owner_env] database values not loaded (%s); using Railway values", box.get("err") or "timed out")
        return 0
    bad = 0
    for key, stored in box["rows"]:  # type: ignore[assignment]
        name = key[len(PREFIX):]
        spec = BY_NAME.get(name)
        if spec is None:
            continue
        try:
            value = unpack(spec, stored)
        except Exception:
            value = None
        if not value:
            bad += 1
            continue
        os.environ[name] = value
        APPLIED[name] = value
    logger.info("[owner_env] applied %d saved setting(s) from the database%s", len(APPLIED),
                f" ({bad} unreadable, skipped)" if bad else "")
    return len(APPLIED)


# ── runtime store (used by the owner form) ──────────────────────────────

async def load_all() -> Dict[str, Optional[str]]:
    """name -> saved value (decrypted) for everything in the database. Unreadable values come back as None."""
    from database import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT key, value FROM bot_global_settings WHERE key LIKE 'env:%' OR key LIKE 'envprev:%'")
    out: Dict[str, Optional[str]] = {}
    PREV_NAMES.clear()
    for r in rows:
        if r["key"].startswith(PREV):
            PREV_NAMES.add(r["key"][len(PREV):])
            continue
        name = r["key"][len(PREFIX):]
        spec = BY_NAME.get(name)
        if spec is None:
            continue
        try:
            out[name] = unpack(spec, r["value"])
        except Exception:
            out[name] = None
    return out


async def _raw(key: str) -> Optional[str]:
    from database import db
    return await db.get_global_setting(key)


async def _drop(key: str) -> None:
    from database import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM bot_global_settings WHERE key = $1", key)


async def log_change(name: str, action: str) -> None:
    """Keep the last 40 changes (key name and what happened, never a value). Best effort."""
    try:
        import json
        from database import db
        raw = await db.get_global_setting(LOG_KEY)
        entries = json.loads(raw) if raw else []
        entries.append({"t": int(time.time()), "n": name, "a": action})
        await db.set_global_setting(LOG_KEY, json.dumps(entries[-40:]))
    except Exception:
        logger.warning("[owner_env] couldn't write the change log", exc_info=True)


async def history(limit: int = 15) -> List[dict]:
    try:
        import json
        raw = await _raw(LOG_KEY)
        return list(reversed(json.loads(raw)))[:limit] if raw else []
    except Exception:
        return []


async def save(name: str, value: str) -> None:
    spec = BY_NAME[name]
    from database import db
    old = await _raw(PREFIX + name)
    if old:
        await db.set_global_setting(PREV + name, old)
    await db.set_global_setting(PREFIX + name, pack(spec, value))
    await log_change(name, "saved")


async def reset(name: str) -> None:
    from database import db
    old = await _raw(PREFIX + name)
    if old:
        await db.set_global_setting(PREV + name, old)
    await _drop(PREFIX + name)
    await log_change(name, "reset")


async def undo(name: str) -> bool:
    """Swap the current value with the previous one. False when there is no previous value."""
    from database import db
    prev = await _raw(PREV + name)
    if not prev:
        return False
    cur = await _raw(PREFIX + name)
    await db.set_global_setting(PREFIX + name, prev)
    if cur:
        await db.set_global_setting(PREV + name, cur)
    else:
        await _drop(PREV + name)
    await log_change(name, "restored the previous value")
    return True


# ── "does this key work": one cheap call per provider ───────────────────

TESTABLE = frozenset({
    "GROQ_API_KEY", "GROQ_API_KEY_BACKUP", "GROQ_API_KEY_BACKUP2", "OPENAI_API_KEY", "GEMINI_API_KEY",
    "ANTHROPIC_API_KEY", "YOUTUBE_API_KEY", "CAT_API_KEY", "PAYSTACK_SECRET_KEY", "GUMROAD_ACCESS_TOKEN",
    "TURNSTILE_SECRET_KEY",
})


async def test_key(name: str, value: str) -> Tuple[str, str]:
    """("ok" | "bad" | "unknown", short text). Never raises, never returns the key."""
    import aiohttp
    bearer = {"Authorization": f"Bearer {value}"}
    method, url, kw = "GET", "", {}
    if name.startswith("GROQ_API_KEY"):
        url, kw = "https://api.groq.com/openai/v1/models", {"headers": bearer}
    elif name == "OPENAI_API_KEY":
        url, kw = "https://api.openai.com/v1/models", {"headers": bearer}
    elif name == "GEMINI_API_KEY":
        url, kw = "https://generativelanguage.googleapis.com/v1beta/models", {"headers": {"x-goog-api-key": value}}
    elif name == "ANTHROPIC_API_KEY":
        url, kw = "https://api.anthropic.com/v1/models", {"headers": {"x-api-key": value, "anthropic-version": "2023-06-01"}}
    elif name == "YOUTUBE_API_KEY":
        url, kw = "https://www.googleapis.com/youtube/v3/videos", {"params": {"part": "id", "id": "dQw4w9WgXcQ", "key": value}}
    elif name == "CAT_API_KEY":
        url, kw = "https://api.thecatapi.com/v1/images/search", {"headers": {"x-api-key": value}, "params": {"limit": "1"}}
    elif name == "PAYSTACK_SECRET_KEY":
        url, kw = "https://api.paystack.co/balance", {"headers": bearer}
    elif name == "GUMROAD_ACCESS_TOKEN":
        url, kw = "https://api.gumroad.com/v2/user", {"params": {"access_token": value}}
    elif name == "TURNSTILE_SECRET_KEY":
        method, url, kw = "POST", "https://challenges.cloudflare.com/turnstile/v0/siteverify", {"data": {"secret": value, "response": "x"}}
    else:
        return "unknown", "no quick test for this one"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as sess:
            async with sess.request(method, url, **kw) as r:
                if name == "TURNSTILE_SECRET_KEY":
                    data = await r.json(content_type=None)
                    return ("bad", "rejected") if "invalid-input-secret" in (data.get("error-codes") or []) else ("ok", "accepted")
                if r.status == 200:
                    return "ok", "works"
                if r.status in (400, 401, 403) and name != "YOUTUBE_API_KEY":
                    return "bad", f"rejected (HTTP {r.status})"
                if name == "YOUTUBE_API_KEY" and r.status in (400, 403):
                    return "bad", f"rejected (HTTP {r.status})"
                return "unknown", f"couldn't tell (HTTP {r.status})"
    except Exception as e:
        return "unknown", f"couldn't reach the service ({type(e).__name__})"


def status(name: str, saved: Dict[str, Optional[str]]) -> Tuple[str, bool]:
    """(short state text, needs_restart) for one key."""
    spec = BY_NAME[name]
    running = os.environ.get(name) or None
    in_db = name in saved and saved[name]
    if in_db:
        restart = running != saved[name] and name not in LIVE
        src = "saved here" + (" (overrides Railway)" if name in RAILWAY_HAD else "")
        return f"{src}: {shown(spec, saved[name])}", restart
    if name in APPLIED:                       # was applied at start, deleted since
        if name in LIVE:
            return "reset", False
        return "reset, Railway value applies after restart" if name in RAILWAY_HAD else "reset, empty after restart", True
    if running:
        return f"Railway: {shown(spec, running)}", False
    return "empty", False


# ── live keys: read at call time, no restart ────────────────────────────

# Keys that are re-read from the database while the bot runs. Anything that reads them must read os.environ at call
# time (modules/groq_keys.py does). Everything else needs a restart (the Restart button in the form).
LIVE = frozenset({
    "GROQ_API_KEY", "GROQ_API_KEY_BACKUP", "GROQ_API_KEY_BACKUP2",
    "OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "FAL_API_KEY", "YOUTUBE_API_KEY", "CAT_API_KEY",
    "TOPGG_TOKEN", "DTHINGS_API_KEY",
    "PAYSTACK_SECRET_KEY", "PAYSTACK_PUBLIC_KEY", "GUMROAD_ACCESS_TOKEN", "GUMROAD_WEBHOOK_SECRET",
})
LIVE_TTL = 30
_live_at = 0.0
_live_task: Optional["asyncio.Task"] = None


def _enabled() -> bool:
    return bool(os.getenv("DATABASE_URL")) and os.getenv("OWNER_ENV_DISABLE", "").strip().lower() not in ("1", "true", "yes")


async def refresh_live(force: bool = False) -> None:
    """Pull the live keys from the database into os.environ. Throttled; never raises."""
    global _live_at
    now = time.monotonic()
    if not _enabled() or (not force and now - _live_at < LIVE_TTL):
        return
    _live_at = now
    try:
        saved = await load_all()
    except Exception:
        return
    for name in LIVE:
        v = saved.get(name)
        if v:
            os.environ[name] = v
            APPLIED[name] = v
        elif name in APPLIED:        # reset since: put the Railway value back, or clear it
            APPLIED.pop(name, None)
            if name in RAILWAY_VALUE:
                os.environ[name] = RAILWAY_VALUE[name]
            else:
                os.environ.pop(name, None)


def kick_refresh() -> None:
    """Sync-safe: start a throttled background refresh if an event loop is running."""
    global _live_task
    if time.monotonic() - _live_at < LIVE_TTL or (_live_task is not None and not _live_task.done()):
        return
    try:
        _live_task = asyncio.get_running_loop().create_task(refresh_live())
    except RuntimeError:
        pass


def env(name: str, default: str = "") -> str:
    """The value of a live key right now (call-time read). Starts a throttled refresh from the database."""
    kick_refresh()
    return os.environ.get(name, default) or default


# ── DM the owner when a needed key is empty ─────────────────────────────

# What breaks when a key is empty. Used by the DM alerts and the "Missing keys" screen. Keys that have another way to
# be filled in (top.gg and Discord Things tokens can come from the DM modal) are left out on purpose.
FEATURE: Dict[str, str] = {
    "GROQ_API_KEY": "AI chat and the roast battles",
    "GEMINI_API_KEY": "Scam Shield's image check and cat pictures check",
    "OPENAI_API_KEY": "the AI store's OpenAI models and card art",
    "ANTHROPIC_API_KEY": "the AI store's Claude models",
    "YOUTUBE_API_KEY": "the YouTube connection",
    "PAYSTACK_SECRET_KEY": "Paystack payments",
    "GUMROAD_ACCESS_TOKEN": "Gumroad products and sale checks",
    "GUMROAD_WEBHOOK_SECRET": "Gumroad sale alerts",
    "TURNSTILE_SECRET_KEY": "captcha verification",
    "VERIFY_SIGNING_SECRET": "verification links",
    "DISCORD_OAUTH_CLIENT_SECRET": "dashboard login with Discord",
}


def missing_keys(saved: Dict[str, Optional[str]]) -> List[str]:
    return [n for n in FEATURE if n in BY_NAME and not (saved.get(n) or (n not in saved and os.environ.get(n)))]


SUMMARY_EVERY = 24 * 3600


async def alert_summary() -> bool:
    """One DM listing every empty key a feature needs. At most once a day, never raises."""
    try:
        if os.getenv("OWNER_ENV_ALERTS_OFF"):
            return False
        token = os.getenv("DISCORD_BOT_TOKEN", "")
        try:
            owner = int(os.getenv("ADMIN_ID", "0") or 0)
        except ValueError:
            owner = 0
        if not token or not owner:
            return False
        miss = missing_keys(await load_all())
        if not miss:
            return False
        from database import db
        now = time.time()
        last = await db.get_global_setting("envalert:summary")
        if last and now - float(last) < SUMMARY_EVERY:
            return False
        await db.set_global_setting("envalert:summary", str(now))
        lines = "\n".join(f"- **{BY_NAME[n].label}**: {FEATURE[n]}" for n in miss[:15])
        more = f"\n...and {len(miss) - 15} more" if len(miss) > 15 else ""
        from discord_bot.dm_send import dm_user
        return await dm_user(
            owner, f"{len(miss)} key(s) a feature needs are empty:\n{lines}{more}\nRun `/admin envsetup` to fill them in.", token)
    except Exception:
        logger.exception("[owner_env] summary alert failed")
        return False


ALERT_EVERY = 6 * 3600     # per key, across all processes (stored in the database)
_alerted: Dict[str, float] = {}
_alert_tasks: set = set()


async def alert_missing(name: str, feature: str) -> bool:
    """DM the owner once per ALERT_EVERY that `name` is empty and `feature` needs it. Never raises."""
    try:
        spec = BY_NAME.get(name)
        if spec is None or os.environ.get(name) or os.getenv("OWNER_ENV_ALERTS_OFF"):
            return False
        now = time.time()
        if now - _alerted.get(name, 0) < ALERT_EVERY:
            return False
        _alerted[name] = now
        token = os.getenv("DISCORD_BOT_TOKEN", "")
        try:
            owner = int(os.getenv("ADMIN_ID", "0") or 0)
        except ValueError:
            owner = 0
        if not token or not owner:
            return False
        key = f"envalert:{name}"
        try:
            from database import db
            last = await db.get_global_setting(key)
            if last and now - float(last) < ALERT_EVERY:
                _alerted[name] = float(last)
                return False
            await db.set_global_setting(key, str(now))
        except Exception:
            pass   # database down: the in-memory throttle still applies
        from discord_bot.dm_send import dm_user
        return await dm_user(
            owner,
            f"**{spec.label}** is empty and {feature} needs it.\n"
            f"Run `/admin envsetup`, open **{spec.group}** and fill it in. It goes live on its own"
            f"{'' if name in LIVE else ' after you press Restart in that form'}.",
            token)
    except Exception:
        logger.exception("[owner_env] alert for %s failed", name)
        return False


def note_missing(name: str, feature: str) -> None:
    """Sync-safe fire and forget version of alert_missing, for `if not KEY:` branches."""
    if os.environ.get(name) or time.time() - _alerted.get(name, 0) < ALERT_EVERY:
        return
    try:
        t = asyncio.get_running_loop().create_task(alert_missing(name, feature))
    except RuntimeError:
        return
    _alert_tasks.add(t)
    t.add_done_callback(_alert_tasks.discard)


# ── restart from the form ───────────────────────────────────────────────

RESTART_KEY = "envrestart:at"
_started = time.time()


async def request_restart() -> None:
    """Ask every process that runs the watcher to restart. The caller restarts its own process afterwards."""
    from database import db
    await db.set_global_setting(RESTART_KEY, str(time.time()))


async def restart_watcher(poll: int = 20) -> None:
    """Run forever in a process: exit (so Railway starts it again) once a restart was requested after it started.
    Also keeps the live keys fresh."""
    while True:
        await asyncio.sleep(poll)
        try:
            await refresh_live()
            from database import db
            at = await db.get_global_setting(RESTART_KEY)
            if at and float(at) > _started + 1:
                logger.warning("[owner_env] restart requested from the owner form, exiting")
                os._exit(1)
        except Exception:
            pass
