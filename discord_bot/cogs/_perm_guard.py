"""
Global command guard: many server owners hand out roles with broad
"manage" permissions without realizing it (often via a giveaway/leveling
role, a "trusted member" role, etc). If a guild has more than
MAX_PRIVILEGED_MEMBERS non-bot members holding administrator or any
manage_* permission, the bot refuses to run ANY slash command in that
guild until the owner cleans the roles up.

Wired in as bot.tree.interaction_check in bot.py, same pattern already
used for bot.tree.on_error.
"""

import time
import logging

import discord
from discord import app_commands

logger = logging.getLogger("discord_bot.perm_guard")

# How many non-bot members may hold a "manage" permission before the bot
# locks the guild out. Tune here — no other code needs to change.
MAX_PRIVILEGED_MEMBERS = 20

# How long a guild's computed count is trusted before recomputing, so a
# busy guild doesn't pay the full member-scan cost on every interaction.
CACHE_TTL_SECONDS = 600

# Permissions that count as "manage" power for this check. administrator
# implies all of these, so it's checked separately and short-circuits.
_MANAGE_PERMS = (
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "manage_messages",
    "manage_webhooks",
    "manage_nicknames",
    "manage_emojis_and_stickers",
    "manage_events",
    "manage_threads",
    "moderate_members",  # timeout members — same "trusted with power" bucket
    "ban_members",
    "kick_members",
)

GUARD_MESSAGE = (
    "\N{LOCK} This server has more than {limit} members with **Manage**-type "
    "permissions ({count} found), so commands are disabled here until that's "
    "cleaned up. This usually happens by accident — a role like \"Member\" or "
    "\"Trusted\" ends up with Manage Roles/Channels/Server etc. Go to "
    "**Server Settings > Roles**, check which roles have those toggles on, "
    "and remove them from roles that shouldn't have them."
)

# guild_id -> (unix_timestamp_computed, count)
_cache: dict[int, tuple[float, int]] = {}


def _member_has_manage_power(member: discord.Member) -> bool:
    perms = member.guild_permissions
    if perms.administrator:
        return True
    return any(getattr(perms, name) for name in _MANAGE_PERMS)


def count_privileged_members(guild: discord.Guild) -> int:
    """Non-bot members holding administrator or any manage_* permission."""
    return sum(
        1
        for m in guild.members
        if not m.bot and _member_has_manage_power(m)
    )


def get_privileged_count(guild: discord.Guild, *, force: bool = False) -> int:
    """Cached wrapper around count_privileged_members. Set force=True to
    bypass the TTL (e.g. from an admin "recheck" command)."""
    now = time.monotonic()
    cached = _cache.get(guild.id)
    if not force and cached is not None and (now - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1]
    count = count_privileged_members(guild)
    _cache[guild.id] = (now, count)
    return count


def invalidate(guild_id: int) -> None:
    """Drop a guild's cached count so the next command recomputes it —
    call this from role-update listeners if/when you want the lockout to
    react immediately instead of waiting out the TTL."""
    _cache.pop(guild_id, None)


class TooManyPrivilegedMembers(app_commands.CheckFailure):
    """Raised by the global interaction_check when a guild is over the
    manage-permission threshold. Handled specially in bot.py's
    _on_app_command_error to show GUARD_MESSAGE instead of the generic
    guild-only fallback message."""

    def __init__(self, count: int, limit: int):
        self.count = count
        self.limit = limit
        super().__init__(f"{count} privileged members (limit {limit})")


async def global_interaction_check(interaction: discord.Interaction) -> bool:
    """Assigned to bot.tree.interaction_check in bot.py. Runs before every
    slash command, in every guild. DMs (interaction.guild is None) are
    left untouched here — GuildOnlyCog / guild_only() already handle those."""
    guild = interaction.guild
    if guild is None:
        return True

    count = get_privileged_count(guild)
    if count > MAX_PRIVILEGED_MEMBERS:
        logger.info(
            f"Blocked /{interaction.command.qualified_name if interaction.command else '?'} "
            f"in guild {guild.id} — {count} members over the {MAX_PRIVILEGED_MEMBERS} manage-perm limit"
        )
        raise TooManyPrivilegedMembers(count, MAX_PRIVILEGED_MEMBERS)
    return True
