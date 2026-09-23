# path: discord_bot/cogs/bump_setup.py

"""
/bumpsetup — configure this server's bump-network settings entirely
through buttons/selects, no typed arguments.

`/bumpsetup` was already referenced from help.py, analytics.py,
setup_channels.py and the "Enable Bump Network" toggle's own
confirmation message, but the command itself was never implemented —
this fills that gap, following the same invoker-gated WizardView
pattern already used by /setupverification (verification.py) and the
invites setup flow (_views_invites.py): one ephemeral message, a few
selects/buttons that mutate in-memory wizard state, and a Finish
button that writes it all to the DB in one call.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from discord_bot.cogs._dm_support import GuildOnlyCog
from database import db

logger = logging.getLogger(__name__)

# intensity_level isn't matched against anything yet (bump_find_targets
# doesn't filter on it) — it's a plain 1-5 scale for how often/energetic
# this server wants incoming bump posts to be, stored for future use by
# whatever schedules bump_enqueue's drip_seconds per guild.
INTENSITY_LABELS = {
    1: "Low — occasional bumps only",
    2: "Light",
    3: "Normal (default)",
    4: "Frequent",
    5: "High — as many as the network sends",
}

# 'any' (the DB default) matches every other guild's language regardless
# of what they've set, per bump_find_targets. Kept short and specific to
# what a bump audience would plausibly filter on.
LANGUAGE_OPTIONS = [
    ("any", "Any language (default)"),
    ("en", "English"),
    ("fr", "French"),
    ("es", "Spanish"),
    ("pt", "Portuguese"),
    ("ar", "Arabic"),
]


def _clone_id_of(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None)


class BumpChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, wizard: "BumpWizardView"):
        self.wizard = wizard
        super().__init__(
            placeholder="1. Choose the bump channel",
            channel_types=[discord.ChannelType.text],
            default_values=[discord.Object(id=wizard.channel_id)] if wizard.channel_id else [],
        )

    async def callback(self, interaction: discord.Interaction):
        self.wizard.channel_id = self.values[0].id
        self.default_values = self.values
        await self.wizard.refresh(interaction)


class BumpLanguageSelect(discord.ui.Select):
    def __init__(self, wizard: "BumpWizardView"):
        self.wizard = wizard
        super().__init__(
            placeholder="2. Choose a language filter",
            options=[
                discord.SelectOption(label=label, value=code, default=(code == wizard.language))
                for code, label in LANGUAGE_OPTIONS
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        self.wizard.language = self.values[0]
        for opt in self.options:
            opt.default = (opt.value == self.wizard.language)
        await self.wizard.refresh(interaction)


class BumpIntensitySelect(discord.ui.Select):
    def __init__(self, wizard: "BumpWizardView"):
        self.wizard = wizard
        super().__init__(
            placeholder="3. Choose how often you want bumps",
            options=[
                discord.SelectOption(label=label, value=str(level), default=(level == wizard.intensity_level))
                for level, label in INTENSITY_LABELS.items()
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        self.wizard.intensity_level = int(self.values[0])
        for opt in self.options:
            opt.default = (int(opt.value) == self.wizard.intensity_level)
        await self.wizard.refresh(interaction)


class BumpNsfwToggleButton(discord.ui.Button):
    def __init__(self, wizard: "BumpWizardView"):
        self.wizard = wizard
        super().__init__(
            label=self._label(wizard.nsfw_opt_in),
            style=self._style(wizard.nsfw_opt_in),
            row=3,
        )

    @staticmethod
    def _label(on: bool) -> str:
        return "NSFW bumps: ON — tap to turn off" if on else "NSFW bumps: OFF — tap to turn on"

    @staticmethod
    def _style(on: bool) -> discord.ButtonStyle:
        return discord.ButtonStyle.danger if on else discord.ButtonStyle.secondary

    async def callback(self, interaction: discord.Interaction):
        self.wizard.nsfw_opt_in = not self.wizard.nsfw_opt_in
        self.label = self._label(self.wizard.nsfw_opt_in)
        self.style = self._style(self.wizard.nsfw_opt_in)
        await self.wizard.refresh(interaction)


class BumpFinishButton(discord.ui.Button):
    def __init__(self, wizard: "BumpWizardView"):
        self.wizard = wizard
        super().__init__(label="✅ Save", style=discord.ButtonStyle.success, row=4)

    async def callback(self, interaction: discord.Interaction):
        wizard = self.wizard
        if not wizard.channel_id:
            await interaction.response.send_message(
                "Pick a bump channel first — that's the one field this needs.", ephemeral=True,
            )
            return
        await db.bump_set_guild_config(
            guild_id=interaction.guild_id,
            clone_id=_clone_id_of(interaction),
            configured_by=interaction.user.id,
            bump_channel_id=wizard.channel_id,
            language=wizard.language,
            nsfw_opt_in=wizard.nsfw_opt_in,
            intensity_level=wizard.intensity_level,
            receives_bumps=True,
        )
        for item in wizard.children:
            item.disabled = True
        embed = wizard.build_embed()
        embed.set_footer(text="Saved ✅")
        await interaction.response.edit_message(embed=embed, view=wizard)
        wizard.stop()


class BumpCancelButton(discord.ui.Button):
    def __init__(self, wizard: "BumpWizardView"):
        self.wizard = wizard
        super().__init__(label="Cancel", style=discord.ButtonStyle.secondary, row=4)

    async def callback(self, interaction: discord.Interaction):
        for item in self.wizard.children:
            item.disabled = True
        embed = self.wizard.build_embed()
        embed.set_footer(text="Cancelled — nothing was changed.")
        await interaction.response.edit_message(embed=embed, view=self.wizard)
        self.wizard.stop()


class BumpWizardView(discord.ui.View):
    def __init__(self, invoker_id: int, current: dict):
        super().__init__(timeout=600)
        self.invoker_id = invoker_id
        self.channel_id = current.get("bump_channel_id")
        self.language = current.get("language") or "any"
        self.nsfw_opt_in = bool(current.get("nsfw_opt_in") or False)
        self.intensity_level = current.get("intensity_level") or 3

        self.add_item(BumpChannelSelect(self))
        self.add_item(BumpLanguageSelect(self))
        self.add_item(BumpIntensitySelect(self))
        self.add_item(BumpNsfwToggleButton(self))
        self.add_item(BumpFinishButton(self))
        self.add_item(BumpCancelButton(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Only the person who ran /bumpsetup can use this.", ephemeral=True)
            return False
        return True

    def build_embed(self) -> discord.Embed:
        def fmt_channel(cid):
            return f"<#{cid}>" if cid else "*not set*"

        lang_label = dict(LANGUAGE_OPTIONS).get(self.language, self.language)

        embed = discord.Embed(
            title="Bump network setup",
            description=(
                "Configure how this server sends and receives bumps. Pick your options below, "
                "then hit **Save**."
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Bump channel", value=fmt_channel(self.channel_id), inline=True)
        embed.add_field(name="Language filter", value=lang_label, inline=True)
        embed.add_field(name="Frequency", value=INTENSITY_LABELS.get(self.intensity_level, str(self.intensity_level)), inline=True)
        embed.add_field(name="NSFW bumps", value="On" if self.nsfw_opt_in else "Off", inline=True)
        return embed

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


class BumpSetupCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="bumpsetup", description="Set up this server's bump network settings")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def bumpsetup(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        clone_id = _clone_id_of(interaction)
        current = await db.bump_get_guild_config(interaction.guild_id, clone_id=clone_id) or {}
        wizard = BumpWizardView(interaction.user.id, current)
        await interaction.followup.send(embed=wizard.build_embed(), view=wizard, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(BumpSetupCog(bot))
