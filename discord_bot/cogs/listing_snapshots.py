# path: discord_bot/cogs/listing_snapshots.py

"""
Periodic refresh of two directory-card features (see app/servers/
_ServerDirectory.tsx): the "online now" badge and the member-count growth
trend arrow.

Both need a real "how many members are online right now" reading, which is
NOT available from the gateway cache this bot already keeps warm — that
would require the privileged presence intent (discord_bot/bot.py only sets
intents.members and intents.message_content). Instead this uses Discord's
plain REST GET /guilds/{id}?with_counts=true, which returns
approximate_presence_count / approximate_member_count using just the bot
token — the same call Discord's own server-discovery surfaces use, no
privileged intent needed.

Ticks slowly and deliberately (one guild every FETCH_INTERVAL_SECONDS,
looped every LOOP_MINUTES) rather than firing every listed guild's request
at once — this endpoint is per-guild, and hammering it for a bot with many
listings risks tripping Discord's global rate limit and stalling everything
else the bot does. A stale online_count for a few extra minutes costs
nothing; a rate-limited bot does.

Only the main bot process runs this (see setup() below) — clones share the
same server_listings/listing_member_snapshots rows keyed by
(guild_id, clone_id), and running the same sweep from every clone process
too would just multiply the REST calls for no extra freshness.
"""

import asyncio
import logging

import discord
from discord.ext import commands, tasks

from database import db

logger = logging.getLogger(__name__)

LOOP_MINUTES = 30
FETCH_INTERVAL_SECONDS = 2.0
SNAPSHOT_RETENTION_DAYS = 30


class ListingSnapshotsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._sweep_loop.start()

    def cog_unload(self):
        self._sweep_loop.cancel()

    @tasks.loop(minutes=LOOP_MINUTES)
    async def _sweep_loop(self):
        try:
            refs = await db.get_all_listed_guild_refs()
        except Exception as e:
            logger.error(f"[listing-snapshots] could not load listed guilds: {e}")
            return

        updated = 0
        for ref in refs:
            guild_id = ref["guild_id"]
            clone_id = ref["clone_id"]
            # Only fetch guilds this particular process actually has the bot
            # in — a clone's guild set is disjoint from the main bot's, and
            # get_all_listed_guild_refs() returns every clone's listings
            # regardless of which process is running this loop.
            if self.bot.get_guild(guild_id) is None:
                continue
            try:
                fetched = await self.bot.fetch_guild(guild_id, with_counts=True)
            except discord.NotFound:
                continue
            except discord.HTTPException as e:
                logger.warning(f"[listing-snapshots] fetch_guild failed for {guild_id}: {e}")
                await asyncio.sleep(FETCH_INTERVAL_SECONDS)
                continue

            member_count = fetched.approximate_member_count or 0
            online_count = fetched.approximate_presence_count
            try:
                await db.record_listing_snapshot(guild_id, clone_id, member_count, online_count)
                updated += 1
            except Exception as e:
                logger.error(f"[listing-snapshots] record_listing_snapshot failed for {guild_id}: {e}")

            await asyncio.sleep(FETCH_INTERVAL_SECONDS)

        try:
            deleted = await db.prune_listing_snapshots(SNAPSHOT_RETENTION_DAYS)
        except Exception as e:
            logger.error(f"[listing-snapshots] prune failed: {e}")
            deleted = 0

        logger.info(f"[listing-snapshots] tick complete: updated={updated}/{len(refs)} pruned={deleted}")

    @_sweep_loop.before_loop
    async def _before_sweep(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    # Main-bot-only — see module docstring. Clone instances are constructed
    # with clone_id set (see PrimeBotClient.__init__ above); the main bot
    # process leaves it None.
    if getattr(bot, "clone_id", None) is not None:
        return
    await bot.add_cog(ListingSnapshotsCog(bot))
