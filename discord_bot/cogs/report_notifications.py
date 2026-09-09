# path: discord_bot/cogs/report_notifications.py

"""
Forwards public "report this server" submissions (see
app/servers/_ServerDirectory.tsx's Report button -> api/server_listings.py
-> database.report_listing) into a Discord channel an admin picked, either
via /setup reportchannel (run in the server, defined in setup_channels.py)
or the one-time DM fallback in _views_report_channel_picker.py.

api/server_listings.py is a separate serverless process with no live
Discord connection, so it can only write a pending row — this cog is
the bot-process half that actually has a gateway connection and does
the posting, same "API writes pending, bot polls and acts" split as
server_listing.py's _voting_panel_poller.
"""

import logging

import discord
from discord.ext import commands, tasks

from database import db
from discord_bot.cogs._views_report_channel_picker import prompt_report_channel

logger = logging.getLogger(__name__)


class ReportNotificationsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._report_poller.start()

    def cog_unload(self):
        self._report_poller.cancel()

    @tasks.loop(seconds=45)
    async def _report_poller(self):
        clone_id = getattr(self.bot, "clone_id", None)
        try:
            config = await db.get_report_notify_channel(clone_id)
        except Exception:
            logger.exception("[report-notifications] config lookup failed")
            return
        if not config or not config.get("channel_id"):
            return  # not configured yet on this process — nothing to do

        channel = self.bot.get_channel(config["channel_id"])
        if channel is None:
            # This process's cache doesn't have that channel (e.g. this is
            # a clone process and the channel lives in a guild only the
            # main bot shares) — leave the rows unnotified for whichever
            # process actually can see it, same race-and-whoever-wins
            # shape as the voting panel poller.
            return

        try:
            reports = await db.list_unnotified_reports(limit=20)
        except Exception:
            logger.exception("[report-notifications] fetch failed")
            return
        if not reports:
            return

        sent_ids = []
        for r in reports:
            embed = discord.Embed(
                title="🚩 Server reported",
                description=r["reason"],
                color=discord.Color.red(),
                timestamp=r["created_at"],
            )
            embed.add_field(name="Guild ID", value=str(r["guild_id"]))
            try:
                await channel.send(embed=embed)
                sent_ids.append(r["id"])
            except (discord.Forbidden, discord.HTTPException):
                logger.exception("[report-notifications] send failed for report %s", r["id"])
                break  # stop this tick rather than losing order — retried next poll

        if sent_ids:
            await db.mark_reports_notified(sent_ids)

    @_report_poller.before_loop
    async def _before_report_poller(self):
        await self.bot.wait_until_ready()
        # Fires the one-time channel-picker DM if nothing's configured yet
        # — safe to call every startup, see prompt_report_channel's own
        # ask-once claim.
        try:
            await prompt_report_channel(self.bot)
        except Exception:
            logger.exception("[report-notifications] channel picker prompt failed")


async def setup(bot: commands.Bot):
    await bot.add_cog(ReportNotificationsCog(bot))
