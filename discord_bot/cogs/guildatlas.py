"""GuildAtlas integration cog.

* Review buttons on submission notifications (custom_id ``ga:r:<action>:<listingId>``): the click is only a request;
  the site re-verifies the reviewer (admin list / reviewer list / live role check) and the status transition.
* Every minute: poll the public site health (alert after 3 failures, with cooldown and a recovery message) and
  nudge the site to retry queued notifications. Every 5 minutes: read the signed internal health for dead or stuck
  notifications.
* Every 10 minutes, in every process (main bot AND clones): member counts of the listed servers that process is in, so a
  server with only a clone still gets its numbers. Invites and bot server counts work the same way.
* Every 30 seconds (main bot): post the site's activity feed (bots/servers added with queue numbers, votes, edits, reviews,
  reports, boosts) to a private #web-logs channel it creates in the support server (needs Manage Channels).
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

from discord_bot.cogs._adaptive_skip import AdaptiveSkip

from database import db
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
        self._reported: set = set()  # server ids last reported to the site (main bot only)
        self._weblog_channel_id: int = 0

    async def cog_load(self):
        import config
        if getattr(config, "GUILDATLAS_POLLING_PAUSED", False):
            self.client = None  # no loops, no knowledge fetches, no queue lookups while paused
            logger.info("GuildAtlas polling is PAUSED (config.GUILDATLAS_POLLING_PAUSED). Nothing is checked on the site.")
            return
        if not self.client:
            logger.info("GuildAtlas integration is OFF (set GUILDATLAS_SITE_URL and a 32+ char GUILDATLAS_INTERNAL_SECRET).")
            return
        # Every process (main bot and each clone) creates the permanent Disguild invites for servers it is itself in.
        self._invite_gate = AdaptiveSkip(idle_threshold=3, idle_skip=1, max_skip=2, name="guildatlas_invites")  # no invite requests: sleeps up to ~3 min
        self._weblog_gate = AdaptiveSkip(idle_threshold=4, idle_skip=1, max_skip=4, name="guildatlas_weblog")  # quiet site: sleeps up to ~2.5 min
        self.invite_loop.start()
        self.stats_loop.start()
        self.members_loop.start()  # a server that only has a clone still gets its member count
        if not self._main:
            return  # clones do not own the review messages and must not poll the rest
        ga.knowledge_cache.client = self.client
        self.minute_loop.start()
        self.knowledge_loop.start()
        self.presence_loop.start()
        self.weblog_loop.start()
        logger.info("GuildAtlas integration is ON (site %s).", self.settings.site_url)

    async def cog_unload(self):
        self.minute_loop.cancel()
        self.knowledge_loop.cancel()
        self.presence_loop.cancel()
        self.weblog_loop.cancel()
        self.invite_loop.cancel()
        self.stats_loop.cancel()
        self.members_loop.cancel()
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

    # ── clones and server invites ──────────────────────────────────────────
    @tasks.loop(minutes=10)
    async def presence_loop(self):
        """Main bot only: tell the site which servers any bot (main or clone) is in, so clone servers can be listed. Ids only."""
        try:
            ids = set(await db.list_active_guild_ids()) | {g.id for g in self.bot.guilds}
            added, removed = ga.presence_delta(self._reported, ids)
            ok = True
            for part in ga.chunked(added):
                ok = await self.client.push_presence(part, []) and ok
            for part in ga.chunked(removed):
                ok = await self.client.push_presence([], part) and ok
            if ok:
                self._reported = ids
            else:
                logger.warning("guildatlas presence report was not accepted; will retry")
        except Exception:
            logger.exception("guildatlas presence loop error")

    @tasks.loop(minutes=10)
    async def stats_loop(self):
        """Every process (main bot and each clone): report this bot's own server and shard count for its Disguild listing. Counts only."""
        try:
            if not self.bot.is_ready():
                return
            entry = ga.stats_entry(self.bot.application_id, len(self.bot.guilds), getattr(self.bot, "shard_count", None))
            if entry is None:
                return
            if not await self.client.push_stats([entry]):
                logger.warning("guildatlas stats report was not accepted; will retry")
        except Exception:
            logger.exception("guildatlas stats loop error")

    @tasks.loop(minutes=10)
    async def members_loop(self):
        """Every process (main bot and each clone): report the member count of each LISTED server this process is in.
        Whichever bot is in the server does it, so a server where only a clone is present is covered. Counts only."""
        try:
            if not self.bot.is_ready():
                return
            listed = await self.client.listed_servers()
            if not listed:
                return
            entries = ga.guild_stats_entries(self.bot.guilds, listed)
            for part in ga.chunked(entries, ga.GUILD_STATS_CHUNK):
                if not await self.client.push_guild_stats(part):
                    logger.warning("guildatlas member counts were not accepted; will retry")
                    break
        except Exception:
            logger.exception("guildatlas members loop error")

    @tasks.loop(seconds=60)
    async def invite_loop(self):
        """Every process: create the permanent invite for server listings whose manager asked for one, in servers this bot is in."""
        if not self._invite_gate.should_run():
            return
        try:
            pending = await self.client.pending_invites()
            self._invite_gate.record(found_something=bool(pending))
            for gid in pending[:20]:
                if self.bot.get_guild(gid) is not None:
                    await self._handle_invite_request(gid)
        except Exception:
            logger.exception("guildatlas invite loop error")

    async def _make_invite(self, guild: discord.Guild):
        """-> (code, error). Never expires, unlimited uses, its own code (so joins through it can be recognised)."""
        me = guild.me
        if me is None:
            return None, "bot member not available in the server"
        order, seen, last_err = [], set(), None
        for ch in [guild.system_channel, guild.rules_channel, *sorted(guild.text_channels, key=lambda c: c.position)]:
            if ch is not None and ch.id not in seen:
                seen.add(ch.id)
                order.append(ch)
        for ch in order[:10]:
            if not ch.permissions_for(me).create_instant_invite:
                continue
            try:
                inv = await ch.create_invite(max_age=0, max_uses=0, unique=True, reason=ga.INVITE_REASON)
                return inv.code, None
            except (discord.Forbidden, discord.HTTPException) as exc:
                last_err = type(exc).__name__
        return None, last_err or "the bot has no channel with the Create Invite permission"

    async def _handle_invite_request(self, guild_id: int):
        guild = self.bot.get_guild(guild_id)
        if guild is None or not await self.client.claim_invite(guild_id):
            return  # another process won the claim (or the bot is not in the server)
        code, err = await self._make_invite(guild)
        code = ga.invite_code_from(code)
        if not code:
            await self.client.complete_invite(guild_id, error=err)
            logger.info("guildatlas invite for guild %s failed: %s", guild_id, err)
            return
        clone_id = getattr(self.bot, "clone_id", None)
        try:
            await db.record_disguild_invite(guild_id, clone_id, code)
            # Seed the join-tracking baseline so the very first join through this invite is attributed to it.
            cache = await db.get_invite_cache(guild_id, clone_id=clone_id)
            cache[code] = {"uses": 0, "inviter_id": getattr(self.bot.user, "id", None), "is_vanity": False}
            await db.replace_invite_cache(guild_id, clone_id, cache)
        except Exception:
            logger.exception("guildatlas could not record the new invite for guild %s (invite still reported to the site)", guild_id)
        await self.client.complete_invite(guild_id, code=code)
        logger.info("guildatlas permanent invite created for guild %s", guild_id)

    # ── web log (support server) ───────────────────────────────────────────
    async def _weblog_channel(self):
        """The private web-logs channel in the support server; found by its saved id, then by name, else created. None when not possible."""
        from config import DISCORD_SUPPORT_SERVER_ID
        guild = self.bot.get_guild(DISCORD_SUPPORT_SERVER_ID) if DISCORD_SUPPORT_SERVER_ID else None
        if guild is None:
            return None
        saved = self._weblog_channel_id or int(await db.get_global_setting(ga.WEBLOG_CHANNEL_KEY) or 0)
        ch = guild.get_channel(saved) if saved else None
        if ch is None:
            ch = discord.utils.get(guild.text_channels, name=ga.WEBLOG_CHANNEL_NAME)
        if ch is None:
            me = guild.me
            if me is None or not guild.me.guild_permissions.manage_channels:
                logger.warning("guildatlas web log: no Manage Channels permission in the support server, cannot create #%s", ga.WEBLOG_CHANNEL_NAME)
                return None
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, read_message_history=True),
            }
            ch = await guild.create_text_channel(ga.WEBLOG_CHANNEL_NAME, overwrites=overwrites, reason="Disguild web log (activity feed from the site)",
                                                 topic="Live activity from the Disguild site: bots and servers added with queue numbers, votes, edits, reviews, reports, boosts.")
            logger.info("guildatlas web log channel created in the support server")
        if ch.id != saved:
            await db.set_global_setting(ga.WEBLOG_CHANNEL_KEY, str(ch.id))
        self._weblog_channel_id = ch.id
        return ch

    @tasks.loop(seconds=30)
    async def weblog_loop(self):
        """Main bot only: post new site events to #web-logs. The cursor only moves after Discord accepted the message, so nothing is lost
        or doubled; a brand-new reader starts at the newest event (no history replay)."""
        if not self._weblog_gate.should_run():
            return
        try:
            raw = await db.get_global_setting(ga.WEBLOG_CURSOR_KEY)
            cursor = int(raw) if raw and raw.isdigit() else None
            ok, events, latest = await self.client.web_events(cursor)
            if not ok:
                return
            self._weblog_gate.record(found_something=bool(events) or cursor is None)
            if cursor is None:
                await db.set_global_setting(ga.WEBLOG_CURSOR_KEY, str(latest))
                await self._weblog_channel()  # create the channel right away so it exists before the first event
                return
            if not events:
                return
            ch = await self._weblog_channel()
            if ch is None:
                return  # keep the cursor: events wait until the channel can exist
            batch, last_id = [], cursor
            for ev in events:
                emb = ga.format_web_event(ev, self.settings.site_url)
                last_id = ev["id"]
                if emb:
                    e = discord.Embed(title=emb["title"][:250], description=emb["description"][:3900], color=emb["color"])
                    at = ev.get("at")
                    if isinstance(at, str):
                        try:
                            from datetime import datetime
                            e.timestamp = datetime.fromisoformat(at.replace("Z", "+00:00"))
                        except ValueError:
                            pass
                    batch.append(e)
            for i in range(0, len(batch), ga.WEBLOG_EMBEDS_PER_MESSAGE):
                await ch.send(embeds=batch[i:i + ga.WEBLOG_EMBEDS_PER_MESSAGE], allowed_mentions=NO_MENTIONS)
            await db.set_global_setting(ga.WEBLOG_CURSOR_KEY, str(last_id))
        except discord.HTTPException as exc:
            logger.warning("guildatlas web log post failed (%s); will retry", type(exc).__name__)
            self._weblog_channel_id = 0
        except Exception:
            logger.exception("guildatlas web log loop error")

    @minute_loop.before_loop
    @weblog_loop.before_loop
    @knowledge_loop.before_loop
    @presence_loop.before_loop
    @invite_loop.before_loop
    @members_loop.before_loop
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
