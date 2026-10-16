"""GuildAtlas integration cog.

* Review buttons on submission notifications (custom_id ``ga:r:<action>:<listingId>``): the click is only a request;
  the site re-verifies the reviewer (admin list / reviewer list / live role check) and the status transition.
* Every minute: poll the public site health (alert after 3 failures, with cooldown and a recovery message) and
  nudge the site to retry queued notifications. Every 5 minutes: read the signed internal health for dead or stuck
  notifications.
* Every 3 hours (ETag-conditional): refresh the public knowledge used by the AI for GuildAtlas questions.

No slash command is registered (global slot budget). ``!ga_refresh`` (owner only, prefix command) forces a
knowledge refresh. Without GUILDATLAS_SITE_URL and GUILDATLAS_INTERNAL_SECRET the cog loads but stays off.
Clones never run the loops or handle the buttons (those messages belong to the main application).
"""
from __future__ import annotations

import logging
import time

import discord
from discord.ext import commands, tasks

from modules import guildatlas as ga

logger = logging.getLogger(__name__)
NO_MENTIONS = discord.AllowedMentions.none()


class ReasonModal(discord.ui.Modal):
    def __init__(self, cog: "GuildAtlasCog", action: str, listing_id: int):
        super().__init__(title="Request changes" if action == "request_changes" else "Reject submission", timeout=600)
        self.cog, self.action, self.listing_id = cog, action, listing_id
        self.reason = discord.ui.TextInput(label="Reason (shown to the owner)", style=discord.TextStyle.paragraph,
                                           min_length=ga.REASON_MIN, max_length=ga.REASON_MAX, required=True)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        reason, err = ga.clean_reason(self.action, str(self.reason.value))
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.apply(interaction, self.action, self.listing_id, reason)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logger.exception("guildatlas reason modal failed", exc_info=error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send("Something went wrong. Nothing was changed.", ephemeral=True)
            else:
                await interaction.response.send_message("Something went wrong. Nothing was changed.", ephemeral=True)
        except discord.HTTPException:
            pass


class GuildAtlasCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings = ga.load_settings()
        self.client = ga.SiteClient(self.settings) if self.settings.enabled else None
        self.monitor = ga.HealthMonitor()
        self._tick = 0
        self._main = getattr(bot, "clone_id", None) is None

    async def cog_load(self):
        if not self.client:
            logger.info("GuildAtlas integration is OFF (set GUILDATLAS_SITE_URL and a 32+ char GUILDATLAS_INTERNAL_SECRET).")
            return
        if not self._main:
            return  # clones do not own the review messages and must not poll
        ga.knowledge_cache.client = self.client
        self.minute_loop.start()
        self.knowledge_loop.start()
        logger.info("GuildAtlas integration is ON (site %s).", self.settings.site_url)

    async def cog_unload(self):
        self.minute_loop.cancel()
        self.knowledge_loop.cancel()
        if ga.knowledge_cache.client is self.client:
            ga.knowledge_cache.client = None

    # ── review buttons ──────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type is not discord.InteractionType.component or not self.client or not self._main:
            return
        parsed = ga.parse_review_custom_id((interaction.data or {}).get("custom_id", ""))
        if not parsed:
            return
        action, listing_id = parsed
        try:
            if action in ga.NEEDS_REASON:
                await interaction.response.send_modal(ReasonModal(self, action, listing_id))
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            await self.apply(interaction, action, listing_id, None)
        except Exception:
            logger.exception("guildatlas review click failed")
            try:
                if interaction.response.is_done():
                    await interaction.followup.send("Something went wrong. Nothing was changed.", ephemeral=True)
                else:
                    await interaction.response.send_message("Something went wrong. Nothing was changed.", ephemeral=True)
            except discord.HTTPException:
                pass

    async def apply(self, interaction: discord.Interaction, action: str, listing_id: int, reason):
        """The actor is always the interaction user, never anything from the button's custom id."""
        payload = ga.review_payload(listing_id, action, reason, interaction.user.id)
        status, data = await self.client.review_action(payload)
        logger.info("guildatlas review action=%s listing=%s status=%s correlation=%s", action, listing_id, status, payload["correlationId"])
        await interaction.followup.send(ga.describe_review_result(status, data, action), ephemeral=True, allowed_mentions=NO_MENTIONS)

    # ── background work ─────────────────────────────────────────────────────
    async def _alert(self, text: str):
        cid = self.settings.alert_channel_id
        if not cid:
            logger.warning("guildatlas alert (no GUILDATLAS_ALERT_CHANNEL_ID set): %s", text)
            return
        try:
            ch = self.bot.get_channel(cid) or await self.bot.fetch_channel(cid)
            await ch.send(text[:1800], allowed_mentions=NO_MENTIONS)
        except Exception as exc:
            logger.warning("guildatlas alert could not be delivered (%s)", type(exc).__name__)

    @tasks.loop(seconds=60)
    async def minute_loop(self):
        try:
            self._tick += 1
            msg = self.monitor.record_public(await self.client.public_health(), time.time())
            if msg:
                await self._alert(msg)
            await self.client.drain_outbox()  # no-op on the site when nothing is queued
            if self._tick % 5 == 0:
                status, h = await self.client.internal_health()
                if status == 200:
                    alert = self.monitor.internal_alert(h, time.time())
                    if alert:
                        await self._alert(alert)
        except Exception:
            logger.exception("guildatlas minute loop error")  # never let the loop die

    @tasks.loop(hours=3)
    async def knowledge_loop(self):
        try:
            changed = await ga.knowledge_cache.refresh()
            if changed:
                logger.info("GuildAtlas knowledge updated to version %s", ga.knowledge_cache.version)
        except Exception:
            logger.exception("guildatlas knowledge refresh error")

    @minute_loop.before_loop
    @knowledge_loop.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()

    @commands.command(name="ga_refresh", hidden=True)
    async def ga_refresh(self, ctx: commands.Context):
        from config import ADMIN_ID
        if not ADMIN_ID or ctx.author.id != ADMIN_ID or not self.client or not self._main:
            return
        changed = await ga.knowledge_cache.refresh()
        v = ga.knowledge_cache.version
        await ctx.reply(f"GuildAtlas knowledge: {'updated to' if changed else 'unchanged,'} version `{v}`." if v else "Could not fetch GuildAtlas knowledge.", allowed_mentions=NO_MENTIONS)


async def setup(bot: commands.Bot):
    await bot.add_cog(GuildAtlasCog(bot))
