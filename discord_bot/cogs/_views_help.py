# path: discord_bot/cogs/_views_help.py

"""
/help as a Components V2 message: no timeout, survives restarts.

Every interactive piece is a discord.ui.DynamicItem with a fixed custom_id and no
per-message state, so a /help message that is hours old (or was posted before a
deploy) behaves exactly like a fresh one. Which category is showing is never
stored anywhere: the select re-renders the whole message from CATEGORIES each time,
and the guild/clone for the dashboard link come from the interaction itself.

The data (CATEGORIES, search) stays in help.py; this file only renders it.
"""

import logging
import re

import discord

from config import DISCORD_SUPPORT_SERVER_INVITE
from discord_bot.cogs._views_premium import PremiumPitchButton
from discord_bot.cogs.help import (
    CATEGORIES, MAX_SEARCH_RESULTS, _BUMP_NOTE, _line_for, search_commands,
)

logger = logging.getLogger(__name__)

_BLURPLE = discord.Color.blurple()
_ORANGE = discord.Color.orange()


def _ctx(interaction: discord.Interaction):
    return interaction.guild_id, getattr(interaction.client, "clone_id", None)


def _bot_name(interaction: discord.Interaction) -> str:
    user = interaction.client.user
    return user.display_name if user else "this bot"


def category_text(name: str, highlight: str | None = None) -> str:
    lines = []
    for cmds, desc in CATEGORIES[name]:
        line = _line_for(cmds, desc)
        if highlight and highlight in cmds:
            line = f"**{line}**"
        lines.append(line)
    text = "\n".join(lines)
    if name == "📣 Bump Network":
        text += _BUMP_NOTE
    return text


def _assemble(blocks: list[str], accent: discord.Color, guild_id, clone_id,
              selected: str | None = None) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=accent)
    for block in blocks:
        container.add_item(discord.ui.TextDisplay(block))
    container.add_item(discord.ui.Separator())
    container.add_item(discord.ui.ActionRow(HelpCategorySelect(selected)))

    buttons = discord.ui.ActionRow(HelpSearchButton(), PremiumPitchButton(), HelpMyDataButton())
    from utils.dash_links import dashboard_link_button
    link = dashboard_link_button(guild_id, clone_id)
    if link is not None:
        buttons.add_item(link)
    if DISCORD_SUPPORT_SERVER_INVITE:
        buttons.add_item(discord.ui.Button(
            label="Join our support server", style=discord.ButtonStyle.link,
            emoji="🆘", url=DISCORD_SUPPORT_SERVER_INVITE,
        ))
    container.add_item(buttons)
    view.add_item(container)
    return view


def build_home_view(bot_name: str, guild_id=None, clone_id=None) -> discord.ui.LayoutView:
    intro = (
        f"## ✨ {bot_name} — Help\n"
        "Pick a category below, or tap 🔍 **Search** to find a command by name or keyword.\n\n"
        + "\n".join(f"**{name}**" for name in CATEGORIES)
    )
    return _assemble([intro], _BLURPLE, guild_id, clone_id)


def build_category_view(name: str, guild_id=None, clone_id=None, *, highlight: str | None = None,
                        footer: str | None = None) -> discord.ui.LayoutView:
    blocks = [f"## {name}\n{category_text(name, highlight)}"]
    if footer:
        blocks.append(f"-# {footer}")
    return _assemble(blocks, _BLURPLE, guild_id, clone_id, selected=name)


def build_results_view(query: str, matches: list, guild_id=None, clone_id=None) -> discord.ui.LayoutView:
    if not matches:
        text = (f"## 🔍 No matching commands\nNothing found for **{query}**.\n"
                "Try a different keyword, or browse a category below.")
        return _assemble([text], _ORANGE, guild_id, clone_id)
    shown = matches[:MAX_SEARCH_RESULTS]
    lines = [f"`/{cmd}` — {desc}  _({category})_" for cmd, desc, category in shown]
    blocks = [f"## 🔍 Results for \"{query}\"\n" + "\n".join(lines)]
    if len(matches) > MAX_SEARCH_RESULTS:
        blocks.append(f"-# Showing first {MAX_SEARCH_RESULTS} of {len(matches)} matches — try a more specific keyword.")
    return _assemble(blocks, _BLURPLE, guild_id, clone_id)


class SearchModal(discord.ui.Modal, title="Search commands"):
    query = discord.ui.TextInput(
        label="Command name or keyword",
        placeholder="e.g. mute, leveling, bump...",
        max_length=100,
    )

    async def on_submit(self, interaction: discord.Interaction):
        text = str(self.query.value)
        matches = search_commands(text)
        guild_id, clone_id = _ctx(interaction)
        if len(matches) == 1:
            cmd, _desc, category = matches[0]
            view = build_category_view(category, guild_id, clone_id, highlight=cmd,
                                       footer=f"Jumped here from your search: \"{text}\"")
        else:
            view = build_results_view(text, matches, guild_id, clone_id)
        await interaction.response.edit_message(view=view)


class HelpCategorySelect(discord.ui.DynamicItem[discord.ui.Select], template=r"^help:cat$"):
    def __init__(self, selected: str | None = None):
        options = [
            discord.SelectOption(label=name.split(" ", 1)[1], emoji=name.split(" ", 1)[0],
                                 value=name, default=(name == selected))
            for name in CATEGORIES
        ]
        super().__init__(discord.ui.Select(
            custom_id="help:cat", placeholder="Choose a category...",
            options=options, min_values=1, max_values=1,
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        name = self.item.values[0]
        guild_id, clone_id = _ctx(interaction)
        if name not in CATEGORIES:        # a category renamed/removed since this message was posted
            view = build_home_view(_bot_name(interaction), guild_id, clone_id)
        else:
            view = build_category_view(name, guild_id, clone_id)
        await interaction.response.edit_message(view=view)


class HelpSearchButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^help:search$"):
    def __init__(self):
        super().__init__(discord.ui.Button(
            label="Search", emoji="🔍", style=discord.ButtonStyle.secondary, custom_id="help:search"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SearchModal())


class HelpMyDataButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^help:mydata$"):
    def __init__(self):
        super().__init__(discord.ui.Button(
            label="My data", emoji="🔒", style=discord.ButtonStyle.secondary, custom_id="help:mydata"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        from discord_bot.cogs.my_data import show
        await show(interaction)


HELP_DYNAMIC_ITEMS = (HelpCategorySelect, HelpSearchButton, HelpMyDataButton)
