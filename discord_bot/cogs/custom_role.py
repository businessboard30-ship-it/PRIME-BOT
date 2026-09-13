"""Custom Role perk: /customrole launches the styling wizard for a buyer
who already paid (see payments_manual.py's "custom_role" UNLOCK_HANDLERS
entry), or shows a Selar "buy" button for one who hasn't yet — same
one-time, self-serve-forever shape as the welcome/ultra packs, just scoped
per-user instead of per-guild.

Deliberately ONE top-level slash command, not two: Discord caps a bot at
100 GLOBAL slash commands total, and this project was already sitting
right at that ceiling — adding a separate /customroleadmin command pushed
it over and crash-looped every clone with
CommandLimitReached("maximum number of slash commands exceeded 100
globally") on startup (see discord_bot/cogs/clone_admin.py's setup(),
which is where bot.add_cog() first hits the 100-command wall during
extension loading). So the admin on/off toggle is folded into this same
command as an optional `disable_feature` parameter instead of its own
command — same net command count as before this feature existed.

The persistent #custom-roles panel (posted via the join-DM "Custom Role"
quickstart button, see _views_join_dm.py) hits the exact same
launch_custom_role() entry point as this slash command's no-argument path.

See discord_bot/cogs/_views_custom_role.py for the wizard, the panel
button, and config.py's CUSTOM_ROLE_FEE_USD / CUSTOM_ROLE_FONT_STYLES /
CUSTOM_ROLE_COLOR_PALETTE for pricing and styling data.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from database import db
from discord_bot.cogs._views_custom_role import launch_custom_role

logger = logging.getLogger(__name__)


class CustomRoleCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="customrole", description="Create/restyle your custom role, or (admins) turn the feature on/off")
    @app_commands.describe(
        disable_feature="[Admins only] True to turn Custom Role off for this server, False to turn it back on — leave blank to open your own role"
    )
    @app_commands.guild_only()
    async def customrole(self, interaction: discord.Interaction, disable_feature: bool | None = None):
        clone_id = getattr(self.bot, "clone_id", None)

        if disable_feature is not None:
            perms = interaction.user.guild_permissions
            if not (perms.manage_guild or interaction.user.id == interaction.guild.owner_id):
                await interaction.response.send_message(
                    "Only someone with **Manage Server** can turn this feature on/off — "
                    "leave `disable_feature` blank to open your own custom role.",
                    ephemeral=True,
                )
                return
            await db.set_custom_role_feature_disabled(interaction.guild.id, disable_feature, clone_id=clone_id)
            state = "disabled" if disable_feature else "enabled"
            await interaction.response.send_message(f"Custom Role feature is now **{state}** for this server.", ephemeral=True)
            return

        await launch_custom_role(interaction, clone_id)


async def setup(bot: commands.Bot):
    await bot.add_cog(CustomRoleCog(bot))
