# path: modules/clone_hosting.py

"""
"What does this clone need on Railway if I host it as its own service?" — sent to the owner by each clone.

Only what you would really have to type is listed:
  * DATABASE_URL and ENCRYPTION_KEY: always needed (they reach the database and decrypt the clone's token, so they can
    never live in the database).
  * Any other variable that is set on the service this clone runs under today AND that the code reads, MINUS everything
    already saved in the database through /admin envsetup (modules/owner_env.py loads those into the environment by
    itself on every start, so they are never repeated here).

The DM carries NO secret values. It gives a block for Railway's Raw Editor that uses reference variables
(`NAME=${{<service>.NAME}}`), so the new service reads the values from the existing one and nothing is typed or pasted.

Sent once per clone, and again only if the list of needed variables changes (tracked in bot_global_settings).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

REQUIRED = ("DATABASE_URL", "ENCRYPTION_KEY")

# A clone logs in with the token stored (encrypted) in the database, never with the main bot's token, so those are not copied.
NOT_FOR_CLONES = frozenset({"DISCORD_BOT_TOKEN", "SINOBANED2_BOT_TOKEN"})
# Belong to the service/host, not to the bot: a new service gets its own.
SERVICE_SPECIFIC = frozenset({"PORT", "DATA_DIR", "SERVICE_ROLE", "CLONE_MANAGER_EXCLUDE_IDS", "OWNER_ENV_DISABLE"})
PLATFORM_PREFIXES = ("RAILWAY_", "NIXPACKS_", "RAILPACK_", "PYTHON", "LC_", "XDG_")
PLATFORM_NAMES = frozenset({"PATH", "HOME", "PWD", "SHLVL", "LANG", "TERM", "HOSTNAME", "USER", "SHELL", "TMPDIR", "TZ",
                            "LOGNAME", "OLDPWD", "_", "VIRTUAL_ENV", "NODE_ENV", "CI"})

SETTINGS_KEY = "clone_hosting_dm:{clone_id}"
MAX_DM_CHARS = 1900

# Any UPPER_CASE string literal: env reads go through many helpers (getenv, _int("X"), _id_set_from_env("X"), ...), and an
# over-inclusive set is harmless because it is only ever intersected with the names that are actually set.
_ENV_READ = re.compile(r"""["']([A-Z][A-Z0-9_]{3,})["']""")
_SKIP_DIRS = {"tests", "node_modules", ".git", "__pycache__", "venv", ".venv", "dashboard", "website", "public"}


@lru_cache(maxsize=1)
def names_read_by_code(root: Optional[str] = None) -> frozenset:
    """Every UPPER_CASE name that appears as a string in the python code. Used only to drop unrelated host variables
    (SHLVL, HOSTNAME, ...) from the list."""
    base = Path(root) if root else Path(__file__).resolve().parents[1]
    found = set()
    for path in base.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.relative_to(base).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        found.update(_ENV_READ.findall(text))
    return frozenset(found)


def _is_noise(name: str) -> bool:
    return name in PLATFORM_NAMES or name.startswith(PLATFORM_PREFIXES)


def needed_variables(environ: Dict[str, str], saved: Iterable[str], read_by_code: Iterable[str]) -> Tuple[List[str], List[str]]:
    """(required, extra). `saved` = names already stored in the database (never repeated)."""
    saved_set = set(saved)
    code = set(read_by_code)
    required = [n for n in REQUIRED]
    extra = sorted(
        n for n, v in environ.items()
        if v and n in code and n not in REQUIRED and n not in saved_set
        and n not in NOT_FOR_CLONES and n not in SERVICE_SPECIFIC and not _is_noise(n)
    )
    return required, extra


def _ref(name: str, service: Optional[str]) -> str:
    return f"{name}=${{{{{service}.{name}}}}}" if service else f"{name}="


def build_messages(clone_id: int, label: str, required: Sequence[str], extra: Sequence[str], saved_count: int,
                   service: Optional[str]) -> List[str]:
    """The DM, already split to fit Discord's 2000-character limit (the variable block is its own message(s))."""
    head = [
        f"**Hosting clone #{clone_id} ({label}) as its own Railway service**",
        "",
        "1. New service from this same repo. Start command: `python -m discord_bot.bot --clone-id " + str(clone_id) + "`",
        "2. Open the new service's **Variables** tab, click **Raw Editor**, and paste the block below. It copies the "
        "values from the existing service" + (f" (`{service}`)" if service else "") + ", so there is nothing to type"
        " and no secret appears in this DM.",
        f"3. On the service that runs the clone manager, add `{clone_id}` to **CLONE_MANAGER_EXCLUDE_IDS** "
        "(comma-separated if there are several), otherwise two copies log in with the same token and fight.",
        "",
    ]
    if saved_count:
        head.append(f"{saved_count} other setting(s) are saved in the database and load by themselves, so they are not repeated.")
    head.append("The clone's own bot token is stored in the database too; the main bot's token is not needed.")
    if not service:
        head.append("(Couldn't tell which Railway service this runs under, so the lines below are bare `NAME=`; "
                    "fill them from the existing service.)")
    intro = "\n".join(head)

    lines = [_ref(n, service) for n in list(required) + list(extra)]
    blocks: List[str] = []
    cur: List[str] = []
    size = 0
    for line in lines:
        if cur and size + len(line) + 1 > MAX_DM_CHARS - 20:
            blocks.append("```\n" + "\n".join(cur) + "\n```")
            cur, size = [], 0
        cur.append(line)
        size += len(line) + 1
    if cur:
        blocks.append("```\n" + "\n".join(cur) + "\n```")
    out = [intro[:MAX_DM_CHARS]] + blocks
    return out


def signature(required: Sequence[str], extra: Sequence[str], service: Optional[str]) -> str:
    raw = "|".join(list(required) + ["--"] + list(extra) + ["--", service or ""])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


async def _saved_names() -> List[str]:
    """Names saved in the database that the bots really load at startup (value readable). Empty when loading from the
    database is switched off, since then nothing is applied automatically."""
    if os.getenv("OWNER_ENV_DISABLE", "").strip().lower() in ("1", "true", "yes"):
        return []
    from modules import owner_env
    saved = await owner_env.load_all()
    return [n for n, v in saved.items() if v]


async def send_hosting_guide(bot, delay: Optional[float] = None) -> bool:
    """Called by a clone after it is online. DMs the owner (once per distinct list). Never raises."""
    clone_id = getattr(bot, "clone_id", None)
    if clone_id is None:
        return False
    try:
        import config
        from database import db
        from discord_bot.dm_send import dm_user

        # Small random wait so a restart of many clones doesn't fire every DM in the same second.
        await asyncio.sleep(random.uniform(5, 60) if delay is None else delay)

        saved = await _saved_names()
        names = await asyncio.to_thread(names_read_by_code)
        required, extra = needed_variables(dict(os.environ), saved, names)
        service = (os.environ.get("RAILWAY_SERVICE_NAME") or "").strip() or None
        sig = signature(required, extra, service)
        key = SETTINGS_KEY.format(clone_id=clone_id)
        if await db.get_global_setting(key) == sig:
            return False

        clone = await db.get_discord_clone(clone_id) or {}
        label = clone.get("bot_username") or (str(bot.user) if getattr(bot, "user", None) else f"clone {clone_id}")
        parts = build_messages(clone_id, label, required, extra, len(saved), service)

        own_token = getattr(getattr(bot, "http", None), "token", None)
        # The clone's own DM only works if it shares a server with the owner; otherwise the main bot (which does) sends it.
        tokens = [t for t in (own_token, getattr(config, "DISCORD_BOT_TOKEN", "")) if t]
        for owner_id in sorted(getattr(config, "DISCORD_OWNER_BROADCAST_IDS", None) or ()):
            sent = False
            for token in tokens:
                if not await dm_user(owner_id, parts[0], token):
                    continue            # this bot can't reach the owner: try the next one, nothing was sent yet
                sent = True
                for part in parts[1:]:  # same bot for the rest so the message arrives whole and from one sender
                    if not await dm_user(owner_id, part, token):
                        sent = False
                        break
                break
            if not sent:
                logger.info("[clone-hosting] couldn't DM owner %s for clone #%s (closed DMs / no shared server); "
                            "will try again on the next start", owner_id, clone_id)
                return False
        await db.set_global_setting(key, sig)
        logger.info("[clone-hosting] sent the hosting variable list for clone #%s (%d to type, %d from the database)",
                    clone_id, len(required) + len(extra), len(saved))
        return True
    except Exception:
        logger.warning("[clone-hosting] couldn't send the hosting guide for clone #%s", clone_id, exc_info=True)
        return False
