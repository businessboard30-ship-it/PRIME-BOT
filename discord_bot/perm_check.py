# path: discord_bot/perm_check.py

"""
Shared "tell the admin exactly which permission is missing" helpers.

  channel_problem(channel, me, perms)   None, or a ready-to-send sentence
  role_problem(guild, role)             None, or why the bot can't hand the role out
  flag / clear / lines                  in-memory "needs attention" notices shown
                                        as a banner at the top of setup wizards
                                        (background tasks that fail have nobody
                                        to reply to — the notice waits for the
                                        next time an admin opens the wizard).
                                        Memory only: resets on restart and comes
                                        back on the next failure.
"""

import time

import discord

LABELS = {
    "view_channel": "View Channel", "send_messages": "Send Messages", "embed_links": "Embed Links",
    "attach_files": "Attach Files", "read_message_history": "Read Message History",
    "manage_roles": "Manage Roles", "manage_channels": "Manage Channels", "manage_messages": "Manage Messages",
    "add_reactions": "Add Reactions", "use_external_emojis": "Use External Emojis",
    "kick_members": "Kick Members", "ban_members": "Ban Members", "moderate_members": "Timeout Members",
    "create_public_threads": "Create Public Threads", "send_messages_in_threads": "Send Messages in Threads",
}

# What a bot needs to post a normal rich message (welcome cards, panels, logs, feeds).
POST = ("view_channel", "send_messages", "embed_links", "attach_files")


def missing_in_channel(channel, me, perms=POST) -> list:
    if channel is None or me is None:
        return []
    have = channel.permissions_for(me)
    return [LABELS.get(p, p.replace("_", " ").title()) for p in perms if not getattr(have, p, False)]


def channel_problem(channel, me, perms=POST):
    """None if fine, else a sentence naming exactly what to fix."""
    miss = missing_in_channel(channel, me, perms)
    if not miss:
        return None
    return (f"I'm missing **{', '.join(miss)}** in {channel.mention}. Open the channel's permissions "
            f"(or my role's), allow those for me, then try again.")


def role_problem(guild: discord.Guild, role: discord.Role):
    me = guild.me
    if role.is_default():
        return "@everyone can't be given out as a role."
    if role.managed:
        return f"{role.mention} is managed by an integration, so I can't assign it."
    if me is None or not me.guild_permissions.manage_roles:
        return "I'm missing the **Manage Roles** permission. Enable it on my role, then try again."
    if role >= me.top_role:
        return (f"{role.mention} is above my highest role, so I can't assign it. "
                f"Drag my role **above** {role.mention} in Server Settings → Roles.")
    return None


# ── needs-attention notices ───────────────────────────────────────────────

_ATTN: dict = {}
_TTL = 7 * 24 * 3600


def flag(guild_id: int, clone_id, key: str, message: str) -> None:
    _ATTN.setdefault((guild_id, clone_id), {})[key] = (message, time.time())


def clear(guild_id: int, clone_id, key: str) -> None:
    d = _ATTN.get((guild_id, clone_id))
    if d:
        d.pop(key, None)


def lines(guild_id: int, clone_id) -> list:
    d = _ATTN.get((guild_id, clone_id)) or {}
    now = time.time()
    for k in [k for k, (_, t) in d.items() if now - t > _TTL]:
        d.pop(k, None)
    return [f"⚠️ **Needs attention:** {m}" for m, _ in d.values()]


# ── member actions (kick / ban / timeout) and generic Forbidden text ──────

def member_problem(guild: discord.Guild, member, perm: str, verb: str):
    """Why the bot can't `verb` this member, or None if it can't tell
    (caller then falls back to its generic message)."""
    me = guild.me
    if member is not None and getattr(member, "id", None) == guild.owner_id:
        return f"I can't {verb} the server owner."
    if me is not None and not getattr(me.guild_permissions, perm, False):
        return f"I'm missing the **{LABELS.get(perm, perm)}** permission. Enable it on my role, then try again."
    top = getattr(member, "top_role", None)
    if me is not None and top is not None and top >= me.top_role:
        return (f"{member.mention}'s highest role ({top.mention}) is at or above mine, so I can't {verb} them. "
                f"Drag my role **above** it in Server Settings → Roles.")
    return None


def forbidden_hint(guild: discord.Guild, *perms: str) -> str:
    """One line naming the guild-level permissions the bot lacks among `perms`."""
    me = guild.me if guild else None
    miss = [LABELS.get(p, p) for p in perms if me is not None and not getattr(me.guild_permissions, p, False)]
    if miss:
        return f"I'm missing **{', '.join(miss)}**. Enable it on my role, then try again."
    return "Discord blocked that — check my role position and this channel's permission overrides."
