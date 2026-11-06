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
                await user.send(await self._reminder_text(row, guild, ts), view=view)
            except discord.HTTPException:
                logger.info("[premium] couldn't DM renewal reminder for guild %s", row["guild_id"])
            except Exception:
                logger.exception("[premium] reminder DM failed for guild %s", row["guild_id"])
            # Marked even when the DM failed (closed DMs) so we don't retry hourly.
            await db.mark_premium_reminded(row["guild_id"], clone_id, row["expires_at"])

    @staticmethod
    async def _reminder_text(row: dict, guild, ts: int) -> str:
        """The renewal / trial-ending DM. It carries the real numbers of what Premium did, so the decision to renew is
        made on evidence; if the numbers can't be read the DM still goes out without them."""
        from modules import premium_report as pr
        trial = bool(row.get("is_trial"))
        days = config.PREMIUM_TRIAL_DAYS if trial else pr.DEFAULT_DAYS
        try:
            stats = await pr.gather(int(row["guild_id"]), days)
            report = pr.render(stats, days, is_trial=trial)
            if report.startswith("I couldn't read"):
                report = ""
        except Exception:
            logger.debug("[premium] report for reminder failed", exc_info=True)
            report = ""
        if trial:
            head = (f"\U0001f381 Your free **Premium** trial for **{guild.name}** ends <t:{ts}:R> (<t:{ts}:D>). "
                    "Tap below to keep everything \u2014 nothing you set up is lost either way.")
        else:
            head = (f"\u23f3 **Premium** for **{guild.name}** ends <t:{ts}:R> (<t:{ts}:D>). "
                    "Tap below to renew \u2014 the new time is added on top of what you have left.")
        return head + ("\n\n" + report if report else "")

    @_reminder_loop.before_loop
    async def _before_reminder_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(GuildPremiumCog(bot))
