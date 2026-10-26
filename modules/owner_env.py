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
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("owner_env")

PREFIX = "env:"
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
        rows = await conn.fetch("SELECT key, value FROM bot_global_settings WHERE key LIKE 'env:%'")
    out: Dict[str, Optional[str]] = {}
    for r in rows:
        name = r["key"][len(PREFIX):]
        spec = BY_NAME.get(name)
        if spec is None:
            continue
        try:
            out[name] = unpack(spec, r["value"])
        except Exception:
            out[name] = None
    return out


async def save(name: str, value: str) -> None:
    spec = BY_NAME[name]
    from database import db
    await db.set_global_setting(PREFIX + name, pack(spec, value))


async def reset(name: str) -> None:
    from database import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM bot_global_settings WHERE key = $1", PREFIX + name)


def status(name: str, saved: Dict[str, Optional[str]]) -> Tuple[str, bool]:
    """(short state text, needs_restart) for one key."""
    spec = BY_NAME[name]
    running = os.environ.get(name) or None
    in_db = name in saved and saved[name]
    if in_db:
        restart = running != saved[name]
        src = "saved here" + (" (overrides Railway)" if name in RAILWAY_HAD else "")
        return f"{src}: {shown(spec, saved[name])}", restart
    if name in APPLIED:                       # was applied at start, deleted since
        return "reset, Railway value applies after restart" if name in RAILWAY_HAD else "reset, empty after restart", True
    if running:
        return f"Railway: {shown(spec, running)}", False
    return "empty", False
