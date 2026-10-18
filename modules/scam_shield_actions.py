"""
What Scam Shield does to the person who posted a caught scam (per server setting, see
discord_scam_shield_guild.punish_action):

    delete      only remove the message (the default, same as before)
    timeout     remove it and time the member out for TIMEOUT_HOURS
    quarantine  remove it and move the member to the server's quarantine role (one-tap release in the panel)
    kick        remove it and kick the member
    ban         remove it and ban the member (also deletes their last hour of messages)

Never actioned: bots, the server owner, staff (any moderation permission), webhooks (no member), and anyone ranked
at or above the bot. A missing permission or a refusal from Discord never raises: the message is already deleted, the
result is just reported back as text for the mod-log.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Dict, Optional, Tuple

import discord

from modules import scam_shield as ss
from modules import server_panel_quarantine as quarantine

logger = logging.getLogger(__name__)

# action -> (button label, emoji, permission the bot needs, one-line description)
ACTIONS: Dict[str, Tuple[str, str, Optional[str], str]] = {
    "delete": ("Delete only", "🗑️", None, "Remove the scam message. Nothing happens to the sender."),
    "timeout": ("Timeout", "⏳", "moderate_members", f"Remove it and time the sender out for {ss.TIMEOUT_HOURS} hours."),
    "quarantine": ("Quarantine", "🔒", "manage_roles",
                   "Remove it and move the sender to a quarantine role that sees nothing. One tap to release."),
    "kick": ("Kick", "👢", "kick_members", "Remove it and kick the sender. They can rejoin."),
    "ban": ("Auto-ban", "🔨", "ban_members", "Remove it and ban the sender, plus their last hour of messages."),
}
_PERM_NAMES = {"moderate_members": "Timeout Members", "manage_roles": "Manage Roles",
               "kick_members": "Kick Members", "ban_members": "Ban Members"}
QUARANTINE_ROLE_NAME = "Quarantined"


def missing_permission(guild, action: str) -> Optional[str]:
    """None when the bot can do `action` here, else a sentence naming the permission to grant."""
    perm = ACTIONS.get(action, (None, None, None, None))[2]
    me = getattr(guild, "me", None)
    if perm is None:
        return None
    if me is None or not getattr(me.guild_permissions, perm, False):
        return f"I need the **{_PERM_NAMES[perm]}** permission on my role first."
    return None


def skip_reason(guild, member) -> Optional[str]:
    """Why this person must not be actioned, or None."""
    if not isinstance(member, discord.Member):
        return "the sender isn't a server member (webhook or left already)"
    if member.bot:
        return "bots are never actioned"
    if member.id == guild.owner_id:
        return "the server owner is never actioned"
    if quarantine.staff_member(member):
        return "staff are never actioned"
    me = guild.me
    if me is None or member.top_role >= me.top_role:
        return "their top role is at or above mine"
    return None


async def ensure_quarantine_role(guild, clone_id, actor_id: int) -> Tuple[bool, str]:
    """One-tap setup used by the join-DM Quarantine option: reuse the configured quarantine role if it still works,
    else create a permission-less "Quarantined" role, save it and hide the server's channels from it."""
    from database import db
    role_id = await db.get_quarantine_role(guild.id, clone_id)
    role = guild.get_role(int(role_id)) if role_id else None
    if role is not None and quarantine.role_problem(guild, role) is None:
        return True, f"Using your existing quarantine role {role.mention}."
    if not guild.me.guild_permissions.manage_roles:
        return False, "I need the **Manage Roles** permission to create the quarantine role."
    try:
        role = discord.utils.get(guild.roles, name=QUARANTINE_ROLE_NAME) or await guild.create_role(
            name=QUARANTINE_ROLE_NAME, permissions=discord.Permissions.none(),
            reason="Scam Shield: quarantine role")
    except (discord.Forbidden, discord.HTTPException):
        return False, "Discord refused to create the role. Check my permissions."
    problem = await quarantine.set_quarantine_role(guild, clone_id, actor_id, role)
    if problem:
        return False, problem
    changed, failed = await quarantine.hide_channels(guild, clone_id, actor_id, role)
    note = f"Created {role.mention} and hid {changed} channel(s) from it."
    if failed:
        note += f" {failed} channel(s) I couldn't change: finish those under **Server panel > Quarantine**."
    return True, note


async def punish(guild, clone_id, member, action: str, reason: str = "Scam Shield caught a scam message") -> Tuple[bool, str]:
    """Apply `action` to `member`. -> (done, short text for the mod-log). 'delete' (or an unknown value) does nothing."""
    if action not in ss.PUNISH_ACTIONS or action == "delete":
        return False, ""
    why = skip_reason(guild, member)
    if why:
        return False, f"No extra action: {why}."
    missing = missing_permission(guild, action)
    if missing:
        return False, f"Couldn't {action}: {missing}"
    reason = reason[:400]
    try:
        if action == "ban":
            await guild.ban(member, delete_message_seconds=3600, reason=f"[scam shield] {reason}")
            return True, "🔨 Sender banned (last hour of their messages deleted)."
        if action == "kick":
            await guild.kick(member, reason=f"[scam shield] {reason}")
            return True, "👢 Sender kicked."
        if action == "timeout":
            await member.timeout(timedelta(hours=ss.TIMEOUT_HOURS), reason=f"[scam shield] {reason}")
            return True, f"⏳ Sender timed out for {ss.TIMEOUT_HOURS} hours."
        ok, msg = await quarantine.quarantine_member(guild, clone_id, None, member, reason)
        return ok, ("🔒 Sender quarantined." if ok else f"Couldn't quarantine: {msg}")
    except (discord.Forbidden, discord.HTTPException) as e:
        logger.warning("[scam-shield] %s failed in guild %s (%s)", action, guild.id, e)
        return False, f"Couldn't {action}: Discord refused (check my role is above theirs)."
