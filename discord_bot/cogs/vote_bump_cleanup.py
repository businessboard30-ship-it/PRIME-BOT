# path: discord_bot/cogs/vote_bump_cleanup.py
"""
One-shot cleanup cog — runs once on startup, then unloads itself.

Removes every vote/boost panel (message + channel) and every bump
channel that this bot created, across all guilds it's currently in.
Clears the corresponding DB rows so the disabled server_listing,
listing_snapshots, and bump cogs leave no orphaned Discord artifacts.

Safe to re-run (idempotent): already-deleted channels/messages are
silently skipped. Once finished it unloads itself so it never runs
again on subsequent restarts unless intentionally re-added.
"""

import logging

import discord
from discord.ext import commands

from database import db

logger = logging.getLogger(__name__)

# The channel name the bot used when auto-creating the vote panel channel.
VOTING_CHANNEL_NAME = "vote-for-us"
# The channel name the bump cog used when auto-creating the bump channel.
BUMP_CHANNEL_NAME = "bump"


class VoteBumpCleanupCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.loop.create_task(self._run_cleanup())

    async def _run_cleanup(self):
        await self.bot.wait_until_ready()
        clone_id = getattr(self.bot, "clone_id", None)

        logger.info("[cleanup] Starting vote-panel + bump-channel cleanup (clone_id=%s)", clone_id)

        vote_deleted_msgs = 0
        vote_deleted_channels = 0
        bump_deleted_channels = 0
        errors = 0

        # ── 1. Vote panels ──────────────────────────────────────────────────
        # Pull every server_listing row that has a stored voting_channel_id.
        # We do a raw query here rather than going through _ensure_voting_panel
        # to keep this cog self-contained and avoid importing the disabled cog.
        try:
            pool = await db._get_pool() if hasattr(db, "_get_pool") else None
            from database import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT guild_id, voting_channel_id, voting_message_id
                    FROM server_listings
                    WHERE voting_channel_id IS NOT NULL
                    """
                )
                listing_rows = [dict(r) for r in rows]
        except Exception:
            logger.exception("[cleanup] failed to query server_listings for voting panels")
            listing_rows = []

        for row in listing_rows:
            guild = self.bot.get_guild(row["guild_id"])
            if guild is None:
                # Bot not in this guild — skip Discord calls, still clear DB.
                await self._clear_voting_panel_db(row["guild_id"])
                continue

            channel_id = row.get("voting_channel_id")
            message_id = row.get("voting_message_id")
            channel = guild.get_channel(channel_id) if channel_id else None

            # Delete the panel message first (if it's not in the channel
            # we're about to delete — deleting the channel auto-removes it,
            # but being explicit avoids a race if the channel survives).
            if channel and message_id and message_id != 0:
                try:
                    msg = await channel.fetch_message(message_id)
                    await msg.delete()
                    vote_deleted_msgs += 1
                    logger.info("[cleanup] deleted vote panel message %s in #%s (guild %s)",
                                message_id, channel.name, guild.id)
                except discord.NotFound:
                    pass  # already gone
                except (discord.Forbidden, discord.HTTPException) as e:
                    logger.warning("[cleanup] couldn't delete vote panel message %s (guild %s): %s",
                                   message_id, guild.id, e)
                    errors += 1

            # Delete the channel only if it's named what we auto-created it as.
            # If the admin renamed or repurposed it, leave it alone.
            if channel and channel.name == VOTING_CHANNEL_NAME:
                try:
                    await channel.delete(reason="Vote/listing feature removed — auto-cleanup")
                    vote_deleted_channels += 1
                    logger.info("[cleanup] deleted #%s (guild %s)", channel.name, guild.id)
                except discord.NotFound:
                    pass
                except (discord.Forbidden, discord.HTTPException) as e:
                    logger.warning("[cleanup] couldn't delete #vote-for-us channel (guild %s): %s",
                                   guild.id, e)
                    errors += 1

            await self._clear_voting_panel_db(row["guild_id"])

        # ── 2. Bump channels ────────────────────────────────────────────────
        try:
            bump_rows = await db.bump_list_configured_guilds(clone_id)
        except Exception:
            logger.exception("[cleanup] failed to query bump_guild_config")
            bump_rows = []

        for row in bump_rows:
            guild = self.bot.get_guild(row["guild_id"])
            if guild is None:
                await self._clear_bump_config_db(row["guild_id"], clone_id)
                continue

            channel_id = row.get("bump_channel_id")
            channel = guild.get_channel(channel_id) if channel_id else None

            # Same rule: only delete if the channel still has the name the
            # bot used when it auto-created it. Admin-renamed channels stay.
            if channel and channel.name == BUMP_CHANNEL_NAME:
                try:
                    await channel.delete(reason="Bump feature removed — auto-cleanup")
                    bump_deleted_channels += 1
                    logger.info("[cleanup] deleted #%s (guild %s)", channel.name, guild.id)
                except discord.NotFound:
                    pass
                except (discord.Forbidden, discord.HTTPException) as e:
                    logger.warning("[cleanup] couldn't delete #bump channel (guild %s): %s",
                                   guild.id, e)
                    errors += 1

            await self._clear_bump_config_db(row["guild_id"], clone_id)

        logger.info(
            "[cleanup] done — vote msgs deleted: %d, vote channels deleted: %d, "
            "bump channels deleted: %d, errors: %d",
            vote_deleted_msgs, vote_deleted_channels, bump_deleted_channels, errors,
        )

        # Self-unload — one-shot, never runs again.
        try:
            await self.bot.unload_extension("discord_bot.cogs.vote_bump_cleanup")
        except Exception:
            logger.warning("[cleanup] self-unload failed (harmless)")

    # ── DB helpers ──────────────────────────────────────────────────────────

    async def _clear_voting_panel_db(self, guild_id: int):
        """Null out voting_channel_id and voting_message_id for this guild."""
        try:
            from database import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE server_listings
                    SET voting_channel_id = NULL,
                        voting_message_id = NULL,
                        voting_panel_pending = FALSE,
                        updated_at = NOW()
                    WHERE guild_id = $1
                    """,
                    guild_id,
                )
        except Exception:
            logger.exception("[cleanup] failed to clear voting panel DB row for guild %s", guild_id)

    async def _clear_bump_config_db(self, guild_id: int, clone_id):
        """Wipe bump_channel_id for this guild (reuses existing db method)."""
        try:
            await db.bump_clear_guild_config(guild_id, clone_id)
        except Exception:
            logger.exception("[cleanup] failed to clear bump config DB row for guild %s", guild_id)


async def setup(bot: commands.Bot):
    await bot.add_cog(VoteBumpCleanupCog(bot))
