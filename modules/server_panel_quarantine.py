"""
Server Owners Panel — Phase 11 helpers (Quarantine role, channel lock, slow mode).

Same rules as modules/server_panel.py: every change writes one audit row, everything is keyed by
(guild_id, clone_id), and nothing here is reachable except through the panel (no new slash command).

Quarantine
  * Moves a member to ONE role (the quarantine role) and remembers the roles it took away, so a
    single tap restores them. Roles the bot can't remove (managed/booster/integration roles, roles
    above the bot) stay on the member.
  * Staff, the server owner, bots, and anyone ranked at or above the bot (or above the person
    acting) are never quarantined.
  * Someone who leaves while quarantined and rejoins is put straight back (apply_on_join).
  * Releasing restores the saved roles that still exist and that the bot can still hand out.

Channel lock
  * Denies Send Messages for @everyone in one text channel and saves what @everyone had before
    (allowed / denied / not set), so unlocking puts back exactly that.
  * If someone changed the overwrite by hand after the lock, unlock leaves it alone.

Slow mode is stateless: the panel just sets the channel's slowmode delay.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import discord

from modules.server_panel import record_change

logger = logging.getLogger(__name__)

SLOWMODE_CHOICES: List[Tuple[str, int]] = [
    ("Off", 0), ("5 seconds", 5), ("10 seconds", 10), ("30 seconds", 30), ("1 minute", 60),
    ("5 minutes", 300), ("15 minutes", 900), ("1 hour", 3600), ("6 hours", 21600),
]
SLOWMODE_SECONDS = {secs for _label, secs in SLOWMODE_CHOICES}
MAX_HIDE_EDITS = 150          # permission edits per "hide channels" tap
LIST_LIMIT = 25               # the release select holds at most 25 options

_STAFF_PERMS = ("administrator", "manage_guild", "ban_members", "kick_members",
                "moderate_members", "manage_messages")


def staff_member(member) -> bool:
    """Same notion of 'staff' as anti-raid: these people are never actioned."""
    perms = getattr(member, "guild_permissions", None)
    return bool(perms and any(getattr(perms, p, False) for p in _STAFF_PERMS))


def _prev_to_value(prev: str) -> Optional[bool]:
    return {"allow": True, "deny": False}.get(prev)


def _value_to_prev(value: Optional[bool]) -> str:
    return "allow" if value is True else "deny" if value is False else "none"


def _bot_can_assign(me, role) -> bool:
    return (role is not None and not role.is_default() and not getattr(role, "managed", False)
            and me is not None and role < me.top_role)


# ── state ────────────────────────────────────────────────────────────────

async def quarantine_state(guild, clone_id: Optional[int]) -> Dict[str, Any]:
    """{'role': Role|None, 'role_id': int|None, 'members': [rows], 'visible': int|None, 'problem': str|None}"""
    from database import db
    try:
        role_id = await db.get_quarantine_role(guild.id, clone_id)
    except Exception:
        logger.debug("[server-panel] quarantine role read failed", exc_info=True)
        role_id = None
    try:
        members = await db.list_quarantined(guild.id, clone_id, LIST_LIMIT)
    except Exception:
        logger.debug("[server-panel] quarantine list read failed", exc_info=True)
        members = []
    role = guild.get_role(int(role_id)) if role_id else None
    return {"role_id": role_id, "role": role, "members": members,
            "visible": channels_visible_to(guild, role) if role is not None else None,
            "problem": role_problem(guild, role) if role_id else None}


def role_problem(guild, role) -> Optional[str]:
    """Why the configured role can't work right now, or None."""
    if role is None:
        return "The quarantine role was deleted — pick a new one."
    me = getattr(guild, "me", None)
    if me is None or not me.guild_permissions.manage_roles:
        return "I need the **Manage Roles** permission to quarantine anyone."
    if getattr(role, "managed", False) or role.is_default():
        return "That role can't be handed out by the bot — pick a normal role."
    if not role < me.top_role:
        return "Move my role above the quarantine role in Server Settings → Roles."
    return None


def channels_visible_to(guild, role) -> int:
    """How many channels the role can still see (a quarantine role should see almost none)."""
    n = 0
    for ch in guild.channels:
        if isinstance(ch, discord.CategoryChannel):
            continue
        try:
            if ch.permissions_for(role).view_channel:
                n += 1
        except Exception:
            logger.debug("[server-panel] permissions_for failed", exc_info=True)
    return n


async def lock_state(guild, clone_id: Optional[int]) -> List[dict]:
    from database import db
    try:
        return await db.list_channel_locks(guild.id, clone_id)
    except Exception:
        logger.debug("[server-panel] channel lock read failed", exc_info=True)
        return []


# ── quarantine role setup ────────────────────────────────────────────────

async def set_quarantine_role(guild, clone_id, actor_id: int, role) -> Optional[str]:
    """None on success, else a reason. Validates before writing."""
    from database import db
    problem = role_problem(guild, role)
    if problem:
        return problem
    old = await db.get_quarantine_role(guild.id, clone_id)
    await db.set_quarantine_role(guild.id, clone_id, role.id)
    await record_change(guild.id, clone_id, actor_id, "quarantine.role", old, role.id)
    return None


async def hide_channels(guild, clone_id, actor_id: int, role) -> Tuple[int, int]:
    """Deny View Channel for the role everywhere it can still see. Categories first (channels that
    follow their category are then covered), then any channel that still shows. -> (changed, failed)."""
    me = getattr(guild, "me", None)
    ordered = sorted(guild.channels, key=lambda c: 0 if isinstance(c, discord.CategoryChannel) else 1)
    changed = failed = 0
    for ch in ordered:
        if changed + failed >= MAX_HIDE_EDITS:
            break
        try:
            if not ch.permissions_for(role).view_channel:
                continue
            if me is not None and not ch.permissions_for(me).manage_roles:
                failed += 1
                continue
            await ch.set_permissions(role, view_channel=False, reason="Quarantine role: hidden from the panel")
            changed += 1
        except (discord.Forbidden, discord.HTTPException):
            failed += 1
    if changed:
        await record_change(guild.id, clone_id, actor_id, "quarantine.hide_channels", None, f"{changed} channel(s)")
    return changed, failed


async def allow_appeal_channel(guild, clone_id, actor_id: int, role, channel) -> Optional[str]:
    """Let the quarantine role see and write in ONE channel (where they can ask staff for help)."""
    me = getattr(guild, "me", None)
    if me is not None and not channel.permissions_for(me).manage_roles:
        return "I need **Manage Permissions** in that channel."
    try:
        await channel.set_permissions(role, view_channel=True, send_messages=True, read_message_history=True,
                                      reason="Quarantine role: appeal channel")
    except (discord.Forbidden, discord.HTTPException):
        return "Discord refused that change — check my permissions in that channel."
    await record_change(guild.id, clone_id, actor_id, "quarantine.appeal_channel", None, channel.id)
    return None


# ── quarantine / release ─────────────────────────────────────────────────

def _why_not(guild, member, actor, role) -> Optional[str]:
    if member is None:
        return "That person isn't in the server."
    me = guild.me
    if member.bot:
        return "Bots are never quarantined."
    if member.id == guild.owner_id:
        return "The server owner can't be quarantined."
    if staff_member(member):
        return "Staff can't be quarantined (they have moderation permissions)."
    if me is None or member.top_role >= me.top_role:
        return "I can't change that person — their top role is at or above mine."
    if actor is not None and actor.id != guild.owner_id and member.top_role >= actor.top_role:
        return "You can only quarantine people ranked below you."
    if role in member.roles:
        return "They're already quarantined."
    return None


async def quarantine_member(guild, clone_id, actor, member, reason: str = "") -> Tuple[bool, str]:
    """-> (ok, message). The roles taken away are saved first and the row is dropped again if Discord refuses."""
    from database import db
    st_role_id = await db.get_quarantine_role(guild.id, clone_id)
    role = guild.get_role(int(st_role_id)) if st_role_id else None
    problem = role_problem(guild, role) if st_role_id else "Pick a quarantine role first."
    if problem:
        return False, problem
    why = _why_not(guild, member, actor, role)
    if why:
        return False, why
    me = guild.me
    saved = [r for r in member.roles if r != role and _bot_can_assign(me, r)]
    kept = [r for r in member.roles if not r.is_default() and r not in saved]
    await db.add_quarantined(guild.id, clone_id, member.id, [r.id for r in saved], reason, getattr(actor, "id", None))
    try:
        await member.edit(roles=kept + [role], reason=f"[quarantine] {reason or 'no reason given'}"[:400])
    except (discord.Forbidden, discord.HTTPException):
        await db.remove_quarantined(guild.id, clone_id, member.id)
        return False, "Discord refused to change their roles — check my permissions."
    await record_change(guild.id, clone_id, getattr(actor, "id", 0), "quarantine.add", None, member.id)
    return True, f"🔒 {member.mention} is quarantined ({len(saved)} role(s) saved for release)."


async def release_member(guild, clone_id, actor_id: int, user_id: int) -> Tuple[bool, str]:
    """One-tap release: put back the saved roles that still exist and that I can still give."""
    from database import db
    row = await db.get_quarantined(guild.id, clone_id, user_id)
    if row is None:
        return False, "They're not on the quarantine list."
    st_role_id = await db.get_quarantine_role(guild.id, clone_id)
    member = guild.get_member(user_id)
    if member is None:
        await db.remove_quarantined(guild.id, clone_id, user_id)
        await record_change(guild.id, clone_id, actor_id, "quarantine.release", user_id, "left the server")
        return True, "They've left the server — removed from the list, so they'll come back unquarantined."
    me = guild.me
    restore = []
    for rid in row.get("saved_role_ids") or []:
        r = guild.get_role(int(rid))
        if r is not None and _bot_can_assign(me, r):
            restore.append(r)
    keep = [r for r in member.roles if not r.is_default() and not (st_role_id and r.id == int(st_role_id))]
    new_roles = keep + [r for r in restore if r not in keep]
    try:
        await member.edit(roles=new_roles, reason="[quarantine] released")
    except (discord.Forbidden, discord.HTTPException):
        return False, "Discord refused to change their roles — check my permissions. They stay on the list."
    await db.remove_quarantined(guild.id, clone_id, user_id)
    await record_change(guild.id, clone_id, actor_id, "quarantine.release", user_id, f"{len(restore)} role(s) restored")
    lost = len(row.get("saved_role_ids") or []) - len(restore)
    return True, (f"🔓 {member.mention} is released ({len(restore)} role(s) restored"
                  + (f", {lost} no longer available" if lost > 0 else "") + ").")


async def release_all(guild, clone_id, actor_id: int) -> str:
    from database import db
    rows = await db.list_quarantined(guild.id, clone_id, 200)
    done = failed = 0
    for row in rows:
        ok, _msg = await release_member(guild, clone_id, actor_id, int(row["user_id"]))
        done += ok
        failed += (not ok)
    return f"🔓 Released {done} member(s)" + (f"; {failed} could not be released and stay listed." if failed else ".")


async def apply_on_join(member, clone_id: Optional[int]) -> bool:
    """Called when someone joins: if they left while quarantined, give the quarantine role straight back."""
    from database import db
    guild = member.guild
    row = await db.get_quarantined(guild.id, clone_id, member.id)
    if row is None:
        return False
    role_id = await db.get_quarantine_role(guild.id, clone_id)
    role = guild.get_role(int(role_id)) if role_id else None
    if role_problem(guild, role):
        return False
    try:
        await member.add_roles(role, reason="[quarantine] rejoined while quarantined")
        return True
    except (discord.Forbidden, discord.HTTPException):
        logger.debug("[quarantine] couldn't re-apply on join", exc_info=True)
        return False


# ── channel lock / slow mode ─────────────────────────────────────────────

def lockable(channel) -> bool:
    return isinstance(channel, discord.TextChannel)


async def lock_channel(guild, clone_id, actor_id: int, channel) -> Tuple[bool, str]:
    from database import db
    if not lockable(channel):
        return False, "Only text channels can be locked."
    me = getattr(guild, "me", None)
    if me is None or not channel.permissions_for(me).manage_roles:
        return False, "I need **Manage Permissions** in that channel."
    existing = await db.get_channel_lock(guild.id, clone_id, channel.id)
    if existing is not None:
        return False, f"{channel.mention} is already locked."
    ow = channel.overwrites_for(guild.default_role)
    if ow.send_messages is False:
        return False, f"@everyone already can't write in {channel.mention} — nothing to lock."
    await db.add_channel_lock(guild.id, clone_id, channel.id, _value_to_prev(ow.send_messages), actor_id)
    ow.send_messages = False
    try:
        await channel.set_permissions(guild.default_role, overwrite=ow, reason="[panel] channel locked")
    except (discord.Forbidden, discord.HTTPException):
        await db.remove_channel_lock(guild.id, clone_id, channel.id)
        return False, "Discord refused that change — check my permissions in that channel."
    await record_change(guild.id, clone_id, actor_id, "channel_lock.lock", None, channel.id)
    return True, f"🔒 {channel.mention} is locked for @everyone."


async def unlock_channel(guild, clone_id, actor_id: int, channel_id: int) -> Tuple[bool, str]:
    from database import db
    row = await db.get_channel_lock(guild.id, clone_id, channel_id)
    if row is None:
        return False, "That channel isn't locked by the panel."
    channel = guild.get_channel(channel_id)
    if channel is None:
        await db.remove_channel_lock(guild.id, clone_id, channel_id)
        return True, "That channel no longer exists — removed from the list."
    ow = channel.overwrites_for(guild.default_role)
    if ow.send_messages is not False:
        await db.remove_channel_lock(guild.id, clone_id, channel_id)
        return True, f"Someone already changed {channel.mention} by hand — I left it as it is."
    ow.send_messages = _prev_to_value(row.get("prev_send", "none"))
    try:
        await channel.set_permissions(guild.default_role, overwrite=ow, reason="[panel] channel unlocked")
    except (discord.Forbidden, discord.HTTPException):
        return False, "Discord refused that change — check my permissions in that channel. It stays locked."
    await db.remove_channel_lock(guild.id, clone_id, channel_id)
    await record_change(guild.id, clone_id, actor_id, "channel_lock.unlock", channel_id, None)
    return True, f"🔓 {channel.mention} is unlocked."


async def unlock_all(guild, clone_id, actor_id: int) -> str:
    done = failed = 0
    for row in await lock_state(guild, clone_id):
        ok, _msg = await unlock_channel(guild, clone_id, actor_id, int(row["channel_id"]))
        done += ok
        failed += (not ok)
    return f"🔓 Unlocked {done} channel(s)" + (f"; {failed} could not be unlocked and stay locked." if failed else ".")


async def set_slowmode(guild, clone_id, actor_id: int, channel, seconds: int) -> Tuple[bool, str]:
    if seconds not in SLOWMODE_SECONDS:
        return False, "Pick one of the slow-mode choices."
    if not isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
        return False, "Slow mode works on text channels."
    me = getattr(guild, "me", None)
    if me is None or not channel.permissions_for(me).manage_channels:
        return False, "I need **Manage Channel** in that channel."
    old = getattr(channel, "slowmode_delay", 0)
    try:
        await channel.edit(slowmode_delay=seconds, reason="[panel] slow mode")
    except (discord.Forbidden, discord.HTTPException):
        return False, "Discord refused that change — check my permissions in that channel."
    await record_change(guild.id, clone_id, actor_id, "slowmode", f"{old}s", f"{seconds}s")
    label = next(lbl for lbl, s in SLOWMODE_CHOICES if s == seconds)
    return True, (f"🐢 Slow mode in {channel.mention}: **{label}**." if seconds else f"🐇 Slow mode is off in {channel.mention}.")
