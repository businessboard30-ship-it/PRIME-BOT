# path: discord_bot/cogs/connect.py

"""
/connections — the only command for YouTube + Roblox. Opens the wizard in
_views_connect.py (account linking, rich lookups, notification feeds).

Background work in this cog (each process — main bot or clone — only touches
feed rows carrying its own clone_id, so nothing is ever posted twice):
  - YouTube upload feeds  (public RSS, no API key)   every 10 min
  - Roblox game updates   (public games API)         every 10 min
  - on_member_join: re-grant the Roblox-verified role to already-linked members
"""

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from discord_bot.cogs import _connect_core as core
from discord_bot.cogs._views_connect import open_hub, sync_roblox_role
from modules import connections_api as api
from modules.connections_api import ConnectError

logger = logging.getLogger(__name__)

POLL_MINUTES = 10
MAX_POSTS_PER_SWEEP = 3        # per feed, so a burst of uploads can't flood a channel


class ConnectCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await core.ensure_tables()
        self._poll_youtube.start()
        self._poll_roblox.start()

    def cog_unload(self):
        self._poll_youtube.cancel()
        self._poll_roblox.cancel()

    @property
    def _clone_id(self):
        return getattr(self.bot, "clone_id", None)

    # ── the one command ──────────────────────────────────────────────────
    @app_commands.command(
        name="connections",
        description="Link YouTube & Roblox, look up videos/channels/games, and set up notifications",
    )
    async def connections(self, interaction: discord.Interaction):
        await open_hub(interaction)

    # ── verified role for returning members ──────────────────────────────
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        try:
            link = await core.get_link(member.id, "roblox")
            if link:
                await sync_roblox_role(member.guild, member.id, self._clone_id, add=True)
        except Exception:
            logger.debug("[connect] join role sync failed", exc_info=True)

    # ── shared posting helper ────────────────────────────────────────────
    async def _channel_for(self, feed: dict):
        ch = self.bot.get_channel(int(feed["channel_id"]))
        if ch is not None:
            return ch
        if self.bot.get_guild(int(feed["guild_id"])) is None:
            return None            # this process isn't in that guild (yet) — leave the row alone
        try:
            return await self.bot.fetch_channel(int(feed["channel_id"]))
        except discord.NotFound:
            await core.remove_feed(feed["id"], feed["guild_id"])
            logger.info(f"[connect] feed {feed['id']} removed: channel deleted")
        except discord.HTTPException:
            pass
        return None

    # ── YouTube uploads ──────────────────────────────────────────────────
    @tasks.loop(minutes=POLL_MINUTES)
    async def _poll_youtube(self):
        try:
            feeds = await core.feeds_of_kind("yt", self._clone_id)
        except Exception:
            logger.exception("[connect] couldn't load YouTube feeds")
            return
        by_channel: dict = {}
        for f in feeds:
            by_channel.setdefault(f["external_id"], []).append(f)
        for channel_id, rows in by_channel.items():
            try:
                entries = await api.fetch_channel_feed(channel_id)
            except ConnectError:
                continue
            except Exception:
                logger.exception("[connect] feed fetch crashed")
                continue
            if not entries:
                continue
            newest = max(e["published"] for e in entries)
            for f in rows:
                try:
                    await self._deliver_youtube(f, entries, newest)
                except Exception:
                    logger.exception(f"[connect] delivery failed for feed {f['id']}")
            await asyncio.sleep(0.5)

    async def _deliver_youtube(self, f: dict, entries: list, newest: str):
        state = core.feed_state(f)
        last = state.get("last")
        if not last:                                   # first sight: baseline, don't post history
            await core.set_feed_state(f["id"], {"last": newest})
            return
        fresh = sorted((e for e in entries if e["published"] > last), key=lambda e: e["published"])
        if not fresh:
            return
        channel = await self._channel_for(f)
        if channel is None:
            return
        role = f["role_id"]
        ping = f"<@&{role}> " if role else ""
        mentions = discord.AllowedMentions(everyone=False, users=False, roles=[discord.Object(id=role)] if role else False)
        for e in fresh[-MAX_POSTS_PER_SWEEP:]:
            try:
                await channel.send(
                    f"{ping}📺 **{e['author'] or f['external_name']}** just uploaded: "
                    f"https://www.youtube.com/watch?v={e['id']}",
                    allowed_mentions=mentions,
                )
            except discord.Forbidden:
                logger.debug(f"[connect] no permission to post in {channel.id}")
                break
            except discord.HTTPException:
                break
        await core.set_feed_state(f["id"], {"last": newest})

    # ── Roblox game updates ──────────────────────────────────────────────
    @tasks.loop(minutes=POLL_MINUTES)
    async def _poll_roblox(self):
        try:
            feeds = await core.feeds_of_kind("rbx_game", self._clone_id)
        except Exception:
            logger.exception("[connect] couldn't load Roblox feeds")
            return
        by_universe: dict = {}
        for f in feeds:
            by_universe.setdefault(f["external_id"], []).append(f)
        for universe, rows in by_universe.items():
            try:
                now = await api.rbx_game_state(int(universe))
            except ConnectError:
                continue
            except Exception:
                logger.exception("[connect] roblox poll crashed")
                continue
            if not now or not now.get("updated"):
                continue
            for f in rows:
                try:
                    await self._deliver_roblox(f, now)
                except Exception:
                    logger.exception(f"[connect] delivery failed for feed {f['id']}")
            await asyncio.sleep(0.5)

    async def _deliver_roblox(self, f: dict, now: dict):
        state = core.feed_state(f)
        if state.get("updated") == now["updated"]:
            return
        state_new = dict(state, updated=now["updated"])
        if not state.get("updated"):                   # no baseline yet
            await core.set_feed_state(f["id"], state_new)
            return
        channel = await self._channel_for(f)
        if channel is None:
            return
        try:
            game = await api.rbx_get_game(str(state.get("place") or ""))
        except ConnectError:
            game = None
        role = f["role_id"]
        ping = f"<@&{role}> " if role else ""
        mentions = discord.AllowedMentions(everyone=False, users=False, roles=[discord.Object(id=role)] if role else False)
        if game:
            embed = core.roblox_game_embed(game)
            embed.title = "🔄 " + (embed.title or "")
            await channel.send(f"{ping}**{f['external_name']}** just got an update!", embed=embed, allowed_mentions=mentions)
        else:
            await channel.send(f"{ping}🔄 **{f['external_name']}** just got an update!", allowed_mentions=mentions)
        await core.set_feed_state(f["id"], state_new)

    @_poll_youtube.before_loop
    @_poll_roblox.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(ConnectCog(bot))
