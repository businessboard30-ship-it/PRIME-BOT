"""Custom Role perk: /customrole launches the styling wizard for a buyer
who already paid (see payments_manual.py's "custom_role" UNLOCK_HANDLERS
entry), or shows a Selar "buy" button for one who hasn't yet — same
one-time, self-serve-forever shape as the welcome/ultra packs, just scoped
per-user instead of per-guild. /customroleadmin lets a server owner turn
the whole feature off for their guild.

See discord_bot/cogs/_views_custom_role.py for the wizard itself and
config.py's CUSTOM_ROLE_FEE_USD / CUSTOM_ROLE_FONT_STYLES / CUSTOM_ROLE_COLOR_PALETTE
for pricing and styling data.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from database import db
from config import CUSTOM_ROLE_FEE_USD
from payments_manual import start_manual_payment
from discord_bot.cogs._views_custom_role import CustomRoleWizardView

logger = logging.getLogger(__name__)


class _BuyCustomRoleView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=120)
        self.guild_id = guild_id

    @discord.ui.button(label=f"💳 Unlock Custom Role — ${CUSTOM_ROLE_FEE_USD}", style=discord.ButtonStyle.success)
    async def buy(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await start_manual_payment(
            interaction, payment_type="custom_role",
            amount_display=f"${CUSTOM_ROLE_FEE_USD}", guild_id=self.guild_id,
        )


class CustomRoleCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="customrole", description="Create or restyle your personal custom role")
    @app_commands.guild_only()
    async def customrole(self, interaction: discord.Interaction):
        clone_id = getattr(self.bot, "clone_id", None)
        guild = interaction.guild

        if await db.is_custom_role_feature_disabled(guild.id, clone_id=clone_id):
            await interaction.response.send_message(
                "Custom roles are turned off in this server.", ephemeral=True
            )
            return

        entitlement = await db.get_custom_role_entitlement(guild.id, interaction.user.id, clone_id=clone_id)
        if not entitlement:
            await interaction.response.send_message(
                f"Custom Role is a one-time **${CUSTOM_ROLE_FEE_USD}** unlock — style your own role "
                "(name, font, color, optional icon) anytime after via this same command, unlimited edits.",
                view=_BuyCustomRoleView(guild.id), ephemeral=True,
            )
            return

        wizard = CustomRoleWizardView(interaction.user.id, guild.id, clone_id, existing=entitlement)
        await interaction.response.send_message(embed=wizard.build_embed(), view=wizard, ephemeral=True)

    @app_commands.command(name="customroleadmin", description="Enable or disable the Custom Role feature for this server")
    @app_commands.describe(disabled="True to turn the feature off, False to turn it back on")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def customroleadmin(self, interaction: discord.Interaction, disabled: bool):
        clone_id = getattr(self.bot, "clone_id", None)
        await db.set_custom_role_feature_disabled(interaction.guild.id, disabled, clone_id=clone_id)
        state = "disabled" if disabled else "enabled"
        await interaction.response.send_message(f"Custom Role feature is now **{state}** for this server.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(CustomRoleCog(bot))
