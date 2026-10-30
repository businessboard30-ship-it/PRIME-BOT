# path: discord_bot/cogs/ai_memory.py

"""
/aimemory -- see, add, edit and delete what the AI remembers. Rules live in modules/ai_memory.py.

Deliberately NOT in the AI command allowlist: the AI must never be able to read, write or wipe memories through a tool call
(that would let text inside a conversation change its own memory). People do it here, or by asking in chat
("remember that I like short answers", "what do you remember about me", "forget everything").

  scope "me"     -- your own notes; only you can change them, only your chats use them
  scope "server" -- this server's notes; needs Manage Server to change, used only in this server
"""

import discord
from discord import app_commands
from discord.ext import commands

from modules import ai_memory

SCOPES = [app_commands.Choice(name="me", value=ai_memory.USER), app_commands.Choice(name="server", value=ai_memory.SERVER)]


def _owner(interaction: discord.Interaction, scope: str):
    return interaction.user.id if scope == ai_memory.USER else interaction.guild_id


def _may_change(interaction: discord.Interaction, scope: str) -> bool:
    if scope == ai_memory.USER:
        return True
    return interaction.guild is not None and bool(getattr(interaction.permissions, "manage_guild", False))


async def _deny_server(interaction: discord.Interaction) -> None:
    await interaction.followup.send("Changing the server's memories needs the **Manage Server** permission.", ephemeral=True)


class AIMemoryCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    group = app_commands.Group(name="aimemory", description="See and manage what the AI remembers")

    async def _guard(self, interaction: discord.Interaction, scope: str, change: bool) -> bool:
        await interaction.response.defer(ephemeral=True)
        if scope == ai_memory.SERVER and interaction.guild is None:
            await interaction.followup.send("Server memories only exist inside a server.", ephemeral=True)
            return False
        if change and not _may_change(interaction, scope):
            await _deny_server(interaction)
            return False
        return True

    @group.command(name="list", description="Show what I remember")
    @app_commands.describe(scope="Your notes or this server's notes")
    @app_commands.choices(scope=SCOPES)
    async def list_cmd(self, interaction: discord.Interaction, scope: app_commands.Choice[str] = None):
        sc = scope.value if scope else ai_memory.USER
        if not await self._guard(interaction, sc, change=False):
            return
        rows = await ai_memory.list_memories(sc, _owner(interaction, sc))
        await interaction.followup.send(ai_memory.format_list(rows, sc), ephemeral=True)

    @group.command(name="add", description="Remember something")
    @app_commands.describe(scope="Your notes or this server's notes", text="What to remember (no secrets or personal details)")
    @app_commands.choices(scope=SCOPES)
    async def add(self, interaction: discord.Interaction, scope: app_commands.Choice[str],
                  text: app_commands.Range[str, 3, ai_memory.MAX_CHARS]):
        if not await self._guard(interaction, scope.value, change=True):
            return
        ok, msg = await ai_memory.add_memory(scope.value, _owner(interaction, scope.value), interaction.user.id, text)
        await interaction.followup.send(("\u2705 " if ok else "\u26a0\ufe0f ") + msg, ephemeral=True)

    @group.command(name="edit", description="Change a memory")
    @app_commands.describe(scope="Your notes or this server's notes", memory_id="The # shown in /aimemory list", text="New text")
    @app_commands.choices(scope=SCOPES)
    async def edit(self, interaction: discord.Interaction, scope: app_commands.Choice[str], memory_id: int,
                   text: app_commands.Range[str, 3, ai_memory.MAX_CHARS]):
        if not await self._guard(interaction, scope.value, change=True):
            return
        ok, msg = await ai_memory.update_memory(scope.value, _owner(interaction, scope.value), memory_id, text)
        await interaction.followup.send(("\u2705 " if ok else "\u26a0\ufe0f ") + msg, ephemeral=True)

    @group.command(name="forget", description="Delete one memory")
    @app_commands.describe(scope="Your notes or this server's notes", memory_id="The # shown in /aimemory list")
    @app_commands.choices(scope=SCOPES)
    async def forget(self, interaction: discord.Interaction, scope: app_commands.Choice[str], memory_id: int):
        if not await self._guard(interaction, scope.value, change=True):
            return
        ok = await ai_memory.delete_memory(scope.value, _owner(interaction, scope.value), memory_id)
        await interaction.followup.send("\u2705 Forgotten." if ok else f"No memory `#{memory_id}` here.", ephemeral=True)

    @group.command(name="clear", description="Delete ALL memories in a scope")
    @app_commands.describe(scope="Your notes or this server's notes")
    @app_commands.choices(scope=SCOPES)
    async def clear(self, interaction: discord.Interaction, scope: app_commands.Choice[str]):
        if not await self._guard(interaction, scope.value, change=True):
            return
        n = await ai_memory.clear_memories(scope.value, _owner(interaction, scope.value))
        await interaction.followup.send(f"\u2705 Deleted {n} memor{'y' if n == 1 else 'ies'}.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AIMemoryCog(bot))
