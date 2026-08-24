"""
Bot Manager — Discord equivalent of handlers/bot_manager_handler.py, using
modules/discord_bot_manager.py (which itself wraps discord_clone_service.py's
token validation + a couple of new Discord REST calls).

/botmanager add       — register a bot you own by pasting its token
/botmanager list       — your registered bots
/botmanager view        — one bot's details
/botmanager setname      — PATCH /users/@me on that bot
/botmanager setdescription — PATCH /applications/@me on that bot
/botmanager setcommands   — overwrite that bot's global slash commands
/botmanager remove       — drop the registration (does not stop/delete the bot)

Tokens are sent as slash-command options, which Discord does NOT mark
ephemeral by default in the invoking user's own client history — same
caveat Discord itself surfaces for any "paste a secret into a command"
UX. Every response here is ephemeral so at least the bot's own replies
don't leak the token into the channel.
"""

import discord
from discord import app_commands
from discord.ext import commands

from modules.discord_bot_manager import (
    verify_bot_token, add_managed_bot, get_user_bots, get_managed_bot,
    remove_managed_bot, set_bot_username, set_bot_description, sync_bot_commands,
)
from discord_bot.cogs._views_bot_manager import BotManagePanelView


def _mask(token: str) -> str:
    return f"{token[:8]}...{token[-4:]}" if len(token) > 14 else "•" * len(token)


class BotManagerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    botmanager = app_commands.Group(name="botmanager", description="Manage Discord bots you own by token")

    @botmanager.command(name="add", description="Register a bot you own (paste its token)")
    @app_commands.describe(token="The bot's token, from the Discord Developer Portal")
    async def botmanager_add(self, interaction: discord.Interaction, token: str):
        await interaction.response.defer(ephemeral=True)
        result = await verify_bot_token(token.strip())
        if not result.get("ok"):
            await interaction.followup.send(f"❌ {result.get('error', 'Could not verify that token.')}", ephemeral=True)
            return
        added = await add_managed_bot(interaction.user.id, token.strip(), result["bot_user_id"], result["bot_username"])
        if not added:
            await interaction.followup.send("You've already registered that bot.", ephemeral=True)
            return
        await interaction.followup.send(f"✅ Registered **{result['bot_username']}**.", ephemeral=True)

    @botmanager.command(name="list", description="List your registered bots")
    async def botmanager_list(self, interaction: discord.Interaction):
        bots = await get_user_bots(interaction.user.id)
        if not bots:
            await interaction.response.send_message("You haven't registered any bots yet — try `/botmanager add`.", ephemeral=True)
            return
        embed = discord.Embed(title="Your registered bots", color=discord.Color.blurple())
        for b in bots:
            embed.add_field(name=f"#{b['id']} — {b['name']}", value=f"`{_mask(b['token'])}`", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @botmanager.command(name="view", description="View one of your registered bots")
    @app_commands.describe(bot_id="The bot's id from /botmanager list")
    async def botmanager_view(self, interaction: discord.Interaction, bot_id: int):
        b = await get_managed_bot(interaction.user.id, bot_id)
        if not b:
            await interaction.response.send_message("No bot with that id belongs to you.", ephemeral=True)
            return
        embed = discord.Embed(title=b["name"], color=discord.Color.blurple())
        embed.add_field(name="Token", value=f"`{_mask(b['token'])}`")
        embed.add_field(name="Username", value=b["username"] or "—")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @botmanager.command(name="manage", description="Pick a registered bot from a dropdown and manage it")
    async def botmanager_manage(self, interaction: discord.Interaction):
        bots = await get_user_bots(interaction.user.id)
        if not bots:
            await interaction.response.send_message("You haven't registered any bots yet — try `/botmanager add`.", ephemeral=True)
            return
        view = BotManagePanelView(bots, interaction.user.id)
        await interaction.response.send_message("Pick a bot below to view, rename, edit, or remove it.", view=view, ephemeral=True)

    @botmanager.command(name="setname", description="Change one of your bots' Discord username")
    @app_commands.describe(bot_id="The bot's id from /botmanager list", username="New username (2-32 chars)")
    async def botmanager_setname(self, interaction: discord.Interaction, bot_id: int, username: str):
        b = await get_managed_bot(interaction.user.id, bot_id)
        if not b:
            await interaction.response.send_message("No bot with that id belongs to you.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        result = await set_bot_username(b["token"], username.strip())
        if not result.get("ok"):
            await interaction.followup.send(f"❌ {result.get('error', 'Discord rejected that change.')}", ephemeral=True)
            return
        await interaction.followup.send(f"✅ Renamed to **{username.strip()}**.", ephemeral=True)

    @botmanager.command(name="setdescription", description="Change one of your bots' public description")
    @app_commands.describe(bot_id="The bot's id from /botmanager list", description="New description (up to 400 chars)")
    async def botmanager_setdescription(self, interaction: discord.Interaction, bot_id: int, description: str):
        b = await get_managed_bot(interaction.user.id, bot_id)
        if not b:
            await interaction.response.send_message("No bot with that id belongs to you.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        result = await set_bot_description(b["token"], description.strip())
        if not result.get("ok"):
            await interaction.followup.send(f"❌ {result.get('error', 'Discord rejected that change.')}", ephemeral=True)
            return
        await interaction.followup.send("✅ Description updated.", ephemeral=True)

    @botmanager.command(name="setcommands", description="Overwrite one of your bots' global slash commands")
    @app_commands.describe(
        bot_id="The bot's id from /botmanager list",
        application_id="The bot's application id (Developer Portal -> General Information)",
        commands="Comma-separated name:description pairs, e.g. ping:Check latency, help:Show help",
    )
    async def botmanager_setcommands(
        self, interaction: discord.Interaction, bot_id: int, application_id: str, commands: str,
    ):
        b = await get_managed_bot(interaction.user.id, bot_id)
        if not b:
            await interaction.response.send_message("No bot with that id belongs to you.", ephemeral=True)
            return
        try:
            app_id = int(application_id.strip())
        except ValueError:
            await interaction.response.send_message("Application id must be numeric.", ephemeral=True)
            return

        parsed = []
        for pair in commands.split(","):
            if ":" not in pair:
                await interaction.response.send_message(f"Couldn't parse `{pair.strip()}` — use `name:description`.", ephemeral=True)
                return
            name, desc = pair.split(":", 1)
            parsed.append({"name": name.strip()[:32], "description": desc.strip()[:100] or "No description", "type": 1})

        await interaction.response.defer(ephemeral=True)
        result = await sync_bot_commands(b["token"], app_id, parsed)
        if not result.get("ok"):
            await interaction.followup.send(f"❌ {result.get('error', 'Discord rejected those commands.')}", ephemeral=True)
            return
        await interaction.followup.send(f"✅ Synced {len(parsed)} command(s). Can take up to an hour to show everywhere.", ephemeral=True)

    @botmanager.command(name="remove", description="Unregister one of your bots (doesn't stop the bot itself)")
    @app_commands.describe(bot_id="The bot's id from /botmanager list")
    async def botmanager_remove(self, interaction: discord.Interaction, bot_id: int):
        ok = await remove_managed_bot(interaction.user.id, bot_id)
        await interaction.response.send_message("✅ Removed." if ok else "❌ No bot with that id belongs to you.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(BotManagerCog(bot))
