# path: discord_bot/cogs/style.py

"""/style — single command entry point for the channel-name/font wizard
(_views_style_wizard.py). No subcommands on purpose: everything (convert
text, ask AI for name ideas, apply to a channel) lives as buttons/selects
on the one wizard message, so there's nothing to memorize beyond the one
command name. The same wizard is also reachable from the combined owner
join DM's "styles" feature button (_views_join_dm.py FEATURE_TOGGLES).

/stylepreview and /stylerename below are real, independently-runnable
slash commands (not just wizard buttons) for exactly one reason: adding
them to modules/ai_command_allowlist.py's AI_COMMANDS is what lets
/aichat (and reply/mention chat) call them as natural-language tool
calls — "show me #general in bold" or "rename #chat to Gaming Lounge in
small caps, implement it" now actually runs through here, using the
existing AI-command tool-calling pipeline in ai_command_guard.py/
ai_tools.py rather than a bespoke parser. /stylepreview is read-only
(requires_confirmation=False in the allowlist) so the AI can just run it
and show the result. /stylerename mutates a real channel, so it's
requires_confirmation=True there — the AI must get an explicit Confirm
click (AIConfirmView) before this callback ever runs, same as kick/ban/
purge."""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from discord_bot.cogs._views_style_wizard import StyleWizardLayoutView
from modules.text_styles import apply_style, style_choices

logger = logging.getLogger(__name__)

_STYLE_APP_CHOICES = [app_commands.Choice(name=label, value=key) for key, label, _emoji in style_choices()]


class StyleCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="style",
        description="Style channel names/text into fancy fonts, or ask AI for name ideas",
    )
    async def style(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id if interaction.guild else None
        clone_id = getattr(self.bot, "clone_id", None)
        view = StyleWizardLayoutView(guild_id, clone_id) if guild_id else None
        if view is None:
            await interaction.response.send_message(
                "Run this in a server \u2014 renaming channels only makes sense there, and text "
                "conversion needs a channel to reply in.", ephemeral=True,
            )
            return
        await interaction.response.send_message(view=view, ephemeral=True)

    @app_commands.command(name="stylepreview", description="Preview text in a fancy unicode font (no changes made)")
    @app_commands.describe(text="The text to style", style="Which font")
    @app_commands.choices(style=_STYLE_APP_CHOICES)
    async def stylepreview(self, interaction: discord.Interaction, text: str, style: app_commands.Choice[str]):
        styled = apply_style(text[:80], style.value)
        await interaction.response.send_message(f"**{style.name}:** {styled}", ephemeral=True)

    @app_commands.command(name="stylerename", description="Style text into a font and rename a channel to it")
    @app_commands.guild_only()
    @app_commands.describe(channel="Channel to rename", text="Base name (plain text)", style="Which font")
    @app_commands.choices(style=_STYLE_APP_CHOICES)
    @app_commands.checks.has_permissions(manage_channels=True)
    async def stylerename(self, interaction: discord.Interaction, channel: discord.TextChannel,
                           text: str, style: app_commands.Choice[str]):
        styled_name = apply_style(text[:80], style.value)
        perms = channel.permissions_for(interaction.guild.me)
        if not perms.manage_channels:
            await interaction.response.send_message(
                "I don't have **Manage Channels** permission there \u2014 grant it and try again.", ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await channel.edit(name=styled_name[:100], reason=f"Styled via /stylerename by {interaction.user}")
        except discord.HTTPException as e:
            await interaction.followup.send(f"\u274c Discord rejected that name: {e.text}", ephemeral=True)
            return
        await interaction.followup.send(f"\u2705 Renamed {channel.mention} to **{styled_name}**.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(StyleCog(bot))
