"""
/help — top-level command overview, grouped by category.

Exists mainly so the bot satisfies listing-site requirements (top.gg,
discordbotlist.com, etc. require a `help` command or alias) but it's also
genuinely useful: a new server admin has no other single place that lists
everything this bot can do across ~25 cogs.

Kept as static category text rather than walking self.bot.tree at runtime —
the tree includes admin-only and clone-only commands that shouldn't be
advertised to every user, and a curated list reads better than a raw dump
of 80+ slash commands anyway.
"""

import discord
from discord import app_commands
from discord.ext import commands

from config import DISCORD_SUPPORT_SERVER_INVITE

CATEGORIES = {
    "🛡️ Moderation & Safety": (
        "`/kick` `/ban` `/timeout` `/warn` — manual moderation\n"
        "`/automod` — configurable word/invite/mention filters\n"
        "`/reactionroles` — self-assignable roles via reactions\n"
        "`/welcome` — welcome cards for new members"
    ),
    "🎮 Leveling & Economy": (
        "`/rank` `/leaderboard` — XP and leveling\n"
        "`/balance` `/vote` — server currency and vote bonus\n"
        "`/referrals` — invite your friends for rewards"
    ),
    "🎌 Anime Discovery": (
        "`/discover` — browse trending anime by category\n"
        "`/search` — search for an anime by name"
    ),
    "📣 Bump Network": (
        "`/bumpsetup` — pick this server's bump channel (auto-suggests a description and tags for you)\n"
        "`/bump now` — bump this server to other opted-in servers\n"
        "`/bump bot` — add or bump a bot listing owned by this server\n"
        "Note: your bump channel also receives other servers'/bots' bumps — that's required to use `/bump`"
    ),
    "🤖 AI Tools": (
        "`/ai` — AI-powered chat\n"
        "`/imagesearch` — reverse image search\n"
        "`/aistore` — AI feature marketplace"
    ),
    "🎬 Media & Integrations": (
        "`/plex` `/jellyfin` — connect your media server\n"
        "`/gdrive` — connect Google Drive"
    ),
    "🧬 Bot Cloning": (
        "`/registerclone` — run your own branded clone of this bot (DM only)\n"
        "`/myclones` `/removeclone` — manage clones you own\n"
        "`/clonemonetize` — configure your clone's own pricing"
    ),
    "⚙️ Utility": (
        "`/language` — set your preferred language\n"
        "`/feedback` — send feedback to the bot owner\n"
        "`/help` — this menu"
    ),
}


class CategorySelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=name.split(" ", 1)[1], emoji=name.split(" ", 1)[0], value=name)
            for name in CATEGORIES
        ]
        super().__init__(placeholder="Choose a category...", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        name = self.values[0]
        embed = discord.Embed(title=name, description=CATEGORIES[name], color=discord.Color.blurple())
        await interaction.response.edit_message(embed=embed, view=self.view)


class HelpView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(CategorySelect())
        # Plain link button — no custom_id needed, so it needs no
        # DynamicItem registration and isn't affected by restarts/timeouts
        # on its own. Same "empty means omit rather than send broken" rule
        # as the join DM's version (discord_bot/cogs/_views_join_dm.py).
        if DISCORD_SUPPORT_SERVER_INVITE:
            self.add_item(discord.ui.Button(
                label="Join our support server", style=discord.ButtonStyle.link,
                emoji="🆘", url=DISCORD_SUPPORT_SERVER_INVITE,
            ))


class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="help", description="See everything this bot can do, by category")
    async def help(self, interaction: discord.Interaction):
        # interaction.client is whichever bot process actually answered this
        # (the main bot, or a specific clone's own process/token) — using
        # its real user name here instead of a hardcoded brand means /help
        # says the right thing on every clone, not just the main bot.
        bot_name = interaction.client.user.display_name if interaction.client.user else "this bot"
        embed = discord.Embed(
            title=f"✨ {bot_name} — Help",
            description=(
                "Pick a category below to see its commands.\n\n"
                + "\n".join(f"**{name}**" for name in CATEGORIES)
            ),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, view=HelpView(), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(HelpCog(bot))
