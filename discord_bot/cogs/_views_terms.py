"""Buttons of the Terms & Privacy wizard (modules/terms_accept.py). Persistent (DynamicItem): they keep working after a
restart or redeploy, on the main bot and on every clone."""
from __future__ import annotations

import logging
import re

import discord

from modules import terms_accept as ta

logger = logging.getLogger(__name__)

_ACCEPT_RE = re.compile(r"terms_accept:(\d+)")
_DECLINE_RE = re.compile(r"terms_decline:(\d+)")
_NOT_ALLOWED = "Only someone with **Manage Server** can accept or decline for this server."


def _links(view: discord.ui.View) -> None:
    view.add_item(discord.ui.Button(label="Terms of Service", style=discord.ButtonStyle.link, url=ta.terms_url()))
    view.add_item(discord.ui.Button(label="Privacy Policy", style=discord.ButtonStyle.link, url=ta.privacy_url()))


def build_view(guild_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(TermsAcceptButton(guild_id))
    view.add_item(TermsDeclineButton(guild_id))
    _links(view)
    return view


def accepted_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    _links(view)
    return view


class TermsAcceptButton(discord.ui.DynamicItem[discord.ui.Button], template=_ACCEPT_RE.pattern):
    def __init__(self, guild_id: int):
        super().__init__(discord.ui.Button(label="I accept", emoji="✅", style=discord.ButtonStyle.success,
                                           custom_id=f"terms_accept:{guild_id}"))
        self.guild_id = guild_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message("This button isn't for this server.", ephemeral=True)
            return
        if not ta.can_decide(interaction.user):
            await interaction.response.send_message(_NOT_ALLOWED, ephemeral=True)
            return
        clone_id = getattr(interaction.client, "clone_id", None)
        try:
            newly, row = await ta.accept(self.guild_id, clone_id, interaction.user.id)
        except Exception:
            logger.exception("[terms] couldn't record the acceptance for guild %s", self.guild_id)
            await interaction.response.send_message("I couldn't save that just now. Please tap again in a moment.", ephemeral=True)
            return
        guild_name = interaction.guild.name if interaction.guild else "this server"
        await interaction.response.edit_message(embed=ta.build_embed(guild_name, row), view=accepted_view())
        if not newly:
            await interaction.followup.send("This server had already accepted. I've updated the message.", ephemeral=True)


class _ConfirmLeave(discord.ui.View):
    def __init__(self, user_id: int, guild: discord.Guild):
        super().__init__(timeout=60)
        self.user_id, self.guild = user_id, guild

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This confirmation isn't yours.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes, remove the bot", style=discord.ButtonStyle.danger)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Okay, leaving the server. You can add me again any time.", view=None)
        try:
            await self.guild.leave()
        except discord.HTTPException:
            logger.info("[terms] couldn't leave guild %s", self.guild.id, exc_info=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled. The bot stays, and you can still accept above.", view=None)


class TermsDeclineButton(discord.ui.DynamicItem[discord.ui.Button], template=_DECLINE_RE.pattern):
    def __init__(self, guild_id: int):
        super().__init__(discord.ui.Button(label="Decline", emoji="✖️", style=discord.ButtonStyle.secondary,
                                           custom_id=f"terms_decline:{guild_id}"))
        self.guild_id = guild_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        if interaction.guild_id != self.guild_id or interaction.guild is None:
            await interaction.response.send_message("This button isn't for this server.", ephemeral=True)
            return
        if not ta.can_decide(interaction.user):
            await interaction.response.send_message(_NOT_ALLOWED, ephemeral=True)
            return
        await interaction.response.send_message(
            "Declining means I can't stay in this server. Remove me from **" + interaction.guild.name + "**?",
            view=_ConfirmLeave(interaction.user.id, interaction.guild), ephemeral=True)


DYNAMIC_ITEMS = (TermsAcceptButton, TermsDeclineButton)
