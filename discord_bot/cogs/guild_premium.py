# path: discord_bot/cogs/guild_premium.py

"""Premium renewal reminders, plus /premium (the self-serve entry point into
the "Go Premium" pitch).

Gumroad memberships renew themselves, so only non-auto-renewing (Paystack,
30-days-per-payment) Premiums get a reminder: config.PREMIUM_REMINDER_DAYS
before expiry, one DM per expiry window to whoever paid last, with a Renew
button. Every clone process runs its own copy of this loop, so each one only
touches rows belonging to its own clone_id.

/premium was deliberately left out for a long time: Discord caps a bot at
100 GLOBAL slash commands total, and this project was sitting right at that
ceiling (see custom_role.py's docstring for the exact CommandLimitReached
crash-loop this caused before). Removing the old Discord premium-groups
feature (/pay /status /createpremium /listpremium /premiumadmin /editpremium
/togglepremium /verify — 8 commands) freed enough headroom to add this one
without risking that wall again."""

import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from database import db
from discord_bot.cogs._views_premium import PremiumSubscribeButton, send_premium_pitch

logger = logging.getLogger(__name__)


class GuildPremiumCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._reminder_loop.start()

    @app_commands.command(name="premium", description="Go Premium — unlock every feature for this whole server")
    @app_commands.guild_only()
    async def premium(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        clone_id = getattr(interaction.client, "clone_id", None)
        await send_premium_pitch(interaction, interaction.guild_id, clone_id)

    async def cog_unload(self):
        self._reminder_loop.cancel()

    @tasks.loop(hours=1)
    async def _reminder_loop(self):
        clone_id = getattr(self.bot, "clone_id", None)
        try:
            rows = await db.get_premium_needing_reminder(clone_id, config.PREMIUM_REMINDER_DAYS)
        except Exception:
            logger.exception("[premium] reminder query failed")
            return
        for row in rows:
            guild = self.bot.get_guild(row["guild_id"])
            if guild is None or not row.get("activated_by"):
                continue
            ts = int(row["expires_at"].timestamp())
            view = discord.ui.View(timeout=None)
            view.add_item(PremiumSubscribeButton(row["guild_id"], clone_id))
            try:
                user = await self.bot.fetch_user(int(row["activated_by"]))
                await user.send(
                    f"💎 **Premium** for **{guild.name}** ends <t:{ts}:R> (<t:{ts}:D>). "
                    "Tap below to renew — the new 30 days are added on top of the time you have left.",
                    view=view,
                )
            except discord.HTTPException:
                logger.info("[premium] couldn't DM renewal reminder for guild %s", row["guild_id"])
            except Exception:
                logger.exception("[premium] reminder DM failed for guild %s", row["guild_id"])
            # Marked even when the DM failed (closed DMs) so we don't retry hourly.
            await db.mark_premium_reminded(row["guild_id"], clone_id, row["expires_at"])

    @_reminder_loop.before_loop
    async def _before_reminder_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(GuildPremiumCog(bot))
