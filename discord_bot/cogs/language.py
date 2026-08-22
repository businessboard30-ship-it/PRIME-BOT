"""
/language — lets a Discord user pick their preferred language for this bot.

Shares the same `users.language` column as the Telegram side
(handlers/language_handler.py) via db.get_user_language/set_user_language,
so the choice follows the person across platforms and clones. Once set,
every cog that calls discord_bot.i18n_helpers.tr(...) picks it up
automatically — see i18n.py's module docstring for how those translations
are produced (AI-generated locale cache + live fallback via Groq).
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from database import db
from i18n import SUPPORTED_LANGUAGES
from discord_bot.i18n_helpers import get_lang, tr

logger = logging.getLogger(__name__)


def _clone_id_of(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None) or 0


class LanguageSelect(discord.ui.Select):
    def __init__(self, current: str):
        options = [
            discord.SelectOption(label=name, value=code, default=(code == current))
            for code, name in SUPPORTED_LANGUAGES.items()
        ]
        super().__init__(placeholder="Choose a language...", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        code = self.values[0]
        await db.set_user_language(interaction.user.id, code, clone_id=_clone_id_of(interaction))
        confirmation = await tr(
            "Language set to **{lang_name}**. New messages from me will use it going forward.",
            code,
            lang_name=SUPPORTED_LANGUAGES[code],
        )
        await interaction.response.edit_message(content=confirmation, view=None)


class LanguageView(discord.ui.View):
    def __init__(self, current: str):
        super().__init__(timeout=60)
        self.add_item(LanguageSelect(current))


class LanguageCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="language", description="Choose the language I reply to you in")
    async def language(self, interaction: discord.Interaction):
        current = await get_lang(interaction)
        prompt = await tr("Pick a language below — I'll use it for every command you run from now on.", current)
        await interaction.response.send_message(prompt, view=LanguageView(current), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(LanguageCog(bot))
