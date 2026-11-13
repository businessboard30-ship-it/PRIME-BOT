# path: modules/role_helper.py
"""Create-the-missing-role helper: the roles some features need but the bot never made for the admin.

  ticket  support role   (tickets: without one only the opener and the bot can see a ticket, so staff can't help)
  welcome member + bot auto-roles
  raid    anti-raid alert role     (optional ping)
  honey   honeypot alert role      (optional ping)

One shared create path used by the persistent buttons (modules' views in discord_bot/cogs/_views_role_helper.py), the
setup screens and the reminder pass:
  * it never asks for more than **Manage Roles**, and every role it makes has NO permissions (a plain label to hand out);
  * a same-named role that already exists and carries no powerful permission is reused instead of duplicated;
  * a role that is above the bot's own is reported with the exact fix instead of failing silently;
  * two taps at once can never create two roles (one lock per server+kind, and the saved setting is re-read inside it);
  * reminder state is one small row per server+kind in bot_global_settings (no schema change), CLAIMED before the
    reminder is sent so a restart or a second process can never post it twice, and given back if the post fails.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# kind -> (what the roles are for, [(config field, role name)])
KINDS: Dict[str, Tuple[str, List[Tuple[str, str]]]] = {
    "ticket": ("ticket support role", [("support_role_id", "Ticket Support")]),
    "welcome": ("welcome auto-roles", [("member_role_id", "New Member"), ("bot_role_id", "Bots")]),
    "raid": ("anti-raid alert role", [("alert_role_id", "Raid Alerts")]),
    "honey": ("honeypot alert role", [("alert_role_id", "Honeypot Alerts")]),
}
NUDGE_STATES = ("sent", "done", "later")
_locks: Dict[Tuple[int, str], asyncio.Lock] = {}

_DANGEROUS = ("administrator", "manage_guild", "manage_roles", "manage_channels", "manage_webhooks", "manage_messages",
              "kick_members", "ban_members", "moderate_members", "mention_everyone", "manage_nicknames", "manage_expressions")


def is_safe_to_adopt(role) -> bool:
    """A role with a powerful permission is never silently reused: handing it to new members/staff would be a surprise."""
    perms = getattr(role, "permissions", None)
    return perms is not None and not any(getattr(perms, p, False) for p in _DANGEROUS)


def role_problem(guild, role) -> Optional[str]:
    from discord_bot import perm_check
    return perm_check.role_problem(guild, role)


@dataclass
class Result:
    created: List = field(default_factory=list)
    adopted: List = field(default_factory=list)
    already: bool = False
    problem: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.problem is None and bool(self.created or self.adopted or self.already)

    def text(self, kind: str) -> str:
        if self.problem:
            return f"⚠️ {self.problem}"
        what = KINDS[kind][0]
        if self.already and not (self.created or self.adopted):
            return f"✅ The {what} is already set up. Nothing to create."
        bits = [f"created {r.mention}" for r in self.created] + [f"reused your existing {r.mention}" for r in self.adopted]
        return (f"✅ I {' and '.join(bits)} and saved it as your {what}. "
                + ("Give it to your staff so they can see tickets." if kind == "ticket" else "You can rename or recolour it any time."))


async def _state(kind: str, guild_id: int, clone_id) -> dict:
    from database import db
    if kind == "ticket":
        return await db.get_ticket_config(guild_id, clone_id=clone_id)
    if kind == "welcome":
        return await db.get_welcome_extras(guild_id, clone_id=clone_id)
    if kind == "raid":
        return await db.get_antiraid_config(guild_id, clone_id=clone_id)
    return await db.get_honeypot_config(guild_id, clone_id=clone_id)


async def _apply(kind: str, guild, clone_id, actor_id: int, fields: dict) -> None:
    from modules import server_panel as sp
    if kind == "ticket":
        await sp.set_tickets(guild.id, clone_id, actor_id, **fields)
    elif kind == "welcome":
        await sp.set_welcome_extras(guild.id, clone_id, actor_id, **fields)
    elif kind == "raid":
        await sp.set_antiraid(guild.id, clone_id, actor_id, **fields)
        try:
            from discord_bot.cogs import antiraid
            antiraid._invalidate(guild.id, clone_id)
        except Exception:
            logger.debug("swallowed exception refreshing antiraid cache", exc_info=True)
    else:
        await sp.set_honeypot(guild.id, clone_id, actor_id, **fields)


async def create_missing(kind: str, guild, clone_id, actor_id: int, state: Optional[dict] = None) -> Result:
    """Creates (or reuses) every role of `kind` that isn't set or was deleted, then saves them. Never raises."""
    import discord
    res = Result()
    if kind not in KINDS:
        res.problem = "I don't know that setup."
        return res
    lock = _locks.setdefault((guild.id, kind), asyncio.Lock())
    async with lock:
        try:
            me = guild.me
            if me is None or not me.guild_permissions.manage_roles:
                res.problem = "I'm missing the **Manage Roles** permission, so I can't create the role. Enable it on my role, then tap the button again."
                return res
            cur = await _state(kind, guild.id, clone_id)           # re-read INSIDE the lock: a second tap sees the first one's result
            fields: dict = {}
            err: Optional[Exception] = None
            for fld, name in KINDS[kind][1]:
                rid = cur.get(fld)
                if rid and guild.get_role(int(rid)) is not None:
                    continue
                existing = next((r for r in guild.roles if r.name == name and not r.managed and not r.is_default()), None)
                if existing is not None and is_safe_to_adopt(existing) and role_problem(guild, existing) is None:
                    fields[fld] = existing.id
                    res.adopted.append(existing)
                    continue
                try:
                    role = await guild.create_role(name=name, permissions=discord.Permissions.none(),
                                                   reason=f"Created for the {KINDS[kind][0]} (button tapped by {actor_id})")
                except (discord.Forbidden, discord.HTTPException) as e:
                    err = e
                    break
                fields[fld] = role.id
                res.created.append(role)
            if fields:                                              # whatever was made is saved even if a later one failed
                await _apply(kind, guild, clone_id, actor_id, fields)
                logger.info("[role-helper] guild=%s kind=%s created=%s adopted=%s", guild.id, kind,
                            [r.id for r in res.created], [r.id for r in res.adopted])
            elif err is None:
                res.already = True
            if isinstance(err, discord.Forbidden):
                res.problem = "Discord refused to create the role. Check that I have **Manage Roles** and that my role isn't restricted."
            elif err is not None:
                res.problem = f"Discord couldn't create the role right now ({getattr(err, 'status', '?')}). Try again in a moment."
            return res
        except Exception:
            logger.exception("[role-helper] unexpected failure guild=%s kind=%s", guild.id, kind)
            res.problem = "Something went wrong while creating the role. Try again in a moment."
            return res


# ── reminder bookkeeping (bot_global_settings; no schema change) ───────────────────────────────────────────────────
def _nkey(guild_id: int, clone_id, kind: str) -> str:
    return f"role_nudge:{'main' if clone_id is None else clone_id}:{guild_id}:{kind}"


async def _pool():
    from database import get_pool
    return await get_pool()


async def claim(guild_id: int, clone_id, kind: str) -> bool:
    """True for exactly one caller per server+kind, ever: inserting the row is the claim."""
    pool = await _pool()
    async with pool.acquire() as conn:
        got = await conn.fetchval(
            "INSERT INTO bot_global_settings (key, value) VALUES ($1, 'sent') ON CONFLICT (key) DO NOTHING RETURNING 1",
            _nkey(guild_id, clone_id, kind))
    return bool(got)


async def release(guild_id: int, clone_id, kind: str) -> None:
    try:
        pool = await _pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM bot_global_settings WHERE key = $1 AND value = 'sent'", _nkey(guild_id, clone_id, kind))
    except Exception:
        logger.exception("[role-helper] couldn't give the reminder claim back for guild %s", guild_id)


async def mark(guild_id: int, clone_id, kind: str, state: str) -> None:
    """Records how the reminder ended ('done' = role created, 'later' = admin declined). Best effort."""
    try:
        from database import db
        await db.set_global_setting(_nkey(guild_id, clone_id, kind), state if state in NUDGE_STATES else "sent")
    except Exception:
        logger.debug("swallowed exception in role_helper.mark", exc_info=True)


async def ticket_candidates(clone_id) -> Dict[int, None]:
    """Servers whose ticket panel is set up but that have no support role, minus any already reminded: ONE query each
    for the whole bot, however many servers it is in. (A support role that was deleted later is not reminded about.)"""
    try:
        pool = await _pool()
        prefix = f"role_nudge:{'main' if clone_id is None else clone_id}:"
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT guild_id FROM discord_ticket_config WHERE clone_id IS NOT DISTINCT FROM $1 AND support_role_id IS NULL "
                "AND (panel_message_id IS NOT NULL OR panel_channel_id IS NOT NULL)", clone_id)
            done = await conn.fetch("SELECT key FROM bot_global_settings WHERE key LIKE $1", prefix + "%:ticket")
        seen = {int(k["key"].split(":")[2]) for k in done}
        return {int(r["guild_id"]): None for r in rows if int(r["guild_id"]) not in seen}
    except Exception:
        logger.exception("[role-helper] couldn't look for servers without a ticket support role")
        return {}


NUDGE_TEXT = {
    "ticket": "\n".join([
        "## 🎫 Your ticket system has no support role yet",
        "Without one, only the person who opens a ticket (and me) can see it, so your staff can't help them.",
        "Tap **Create support role** and I'll make a **Ticket Support** role and set it for you. Then give that role to your staff.",
        "-# Only people with **Manage Server** can use the buttons. This reminder is sent once and never again.",
    ]),
}
