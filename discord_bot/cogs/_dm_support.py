"""
Phase 1 (DM support) shared helper.

Two layers, used together on every guild-only cog:

1. `@app_commands.guild_only()` on each top-level command/Group — this is
   enforced by Discord itself (command simply won't show/run in a DM), so
   most users never hit an error at all. Per discord.py, this decorator is
   a no-op on *subcommands* of a Group, so it's applied to the Group object
   itself, which is sufficient — Discord scopes context restriction at the
   top-level command/group, not per-subcommand.
2. `GuildOnlyCog.interaction_check` — a functional fallback for anything
   that slips past #1 (stale client cache, a guild that hasn't picked up
   the app-command context update yet, etc). Any cog that should be
   guild-only can inherit `GuildOnlyCog` instead of `commands.Cog` to get
   a friendly ephemeral reply instead of an unhandled exception.
"""

import discord
from discord.ext import commands

GUILD_ONLY_MESSAGE = "🚫 This command only works inside a server."


class GuildOnlyCog(commands.Cog):
    """Base class for cogs whose commands all require a guild. Subclass
    this instead of commands.Cog — no other change needed."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            if not interaction.response.is_done():
                await interaction.response.send_message(GUILD_ONLY_MESSAGE, ephemeral=True)
            return False
        return True
