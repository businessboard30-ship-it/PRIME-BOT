"""
Archive automation — background loops that keep the Bots Archive
self-maintaining instead of requiring an owner to run commands by hand.

Everything here reads/writes through modules/archive_adapter.py and calls
back into ArchiveCog's card-rendering helpers (_post_card/_refresh_card/
_delete_card) so there's exactly one place that knows how to draw a card.

Loops, and what each automates:
  1. review_expiry_loop   — auto-denies pending/flagged listings that sat
                             untouched past REVIEW_EXPIRY_DAYS.
  2. trending_repost_loop — keeps a live trending post in each guild's
                             featured_channel_id instead of it only being
                             available on-demand via /archive trending.
  3. dead_bot_sweep_loop  — re-checks approved listings against Discord's
                             RPC endpoint; delists anything that no longer
                             resolves (bot deleted/removed).
  4. boost_expiry_loop    — warns buyers ~24h before a boost lapses, and
                             re-renders any card whose boost just expired
                             so it drops out of boosted styling promptly.
  5 & 9. risk + category recheck (same batch, same loop) — periodically
                             re-scores approved listings and, if the AI
                             classifier disagrees with the chosen category,
                             DMs the owner a suggestion. Never auto-changes
                             anything on its own — flags only.
  6. dispute_reminder_loop— re-pings admins about disputes that have sat
                             open for too long without a decision.
  7. webhook_retry_loop   — drains archive_webhook_failures with backoff,
                             so a submitter's flaky endpoint doesn't just
                             silently miss events forever.
  10. duplicate_cleanup_loop — scans each configured listing channel for
                             more than one card claiming the same listing
                             (a real bug we hit during testing) and deletes
                             the stale extras, keeping only the one the DB
                             has on record.

All loops are deliberately conservative: every DB write is idempotent-ish
and every Discord call is wrapped so one bad guild/channel/listing can't
kill the whole loop iteration.
"""

import logging
import re

import discord
from discord.ext import commands, tasks

from config import DISCORD_CLONE_ADMIN_IDS
from modules import archive_adapter as arc

logger = logging.getLogger(__name__)

REVIEW_EXPIRY_DAYS = 7
DEAD_BOT_RESCAN_HOURS = 48
RISK_RESCAN_HOURS = 24 * 14  # every ~2 weeks per listing, batched daily
DISPUTE_REMINDER_HOURS = 48
BOOST_WARNING_HOURS = 24

LISTING_FOOTER_RE = re.compile(r"Listing #(\d+)")


class ArchiveAutomationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await arc.ensure_ready()
        self.review_expiry_loop.start()
        self.trending_repost_loop.start()
        self.dead_bot_sweep_loop.start()
        self.boost_expiry_loop.start()
        self.risk_and_category_recheck_loop.start()
        self.dispute_reminder_loop.start()
        self.webhook_retry_loop.start()
        self.duplicate_cleanup_loop.start()

    async def cog_unload(self):
        for loop in (
            self.review_expiry_loop, self.trending_repost_loop, self.dead_bot_sweep_loop,
            self.boost_expiry_loop, self.risk_and_category_recheck_loop,
            self.dispute_reminder_loop, self.webhook_retry_loop, self.duplicate_cleanup_loop,
        ):
            loop.cancel()

    @property
    def _archive_cog(self):
        # Card rendering stays owned by ArchiveCog — we just call into it,
        # rather than duplicating _post_card/_refresh_card/_delete_card here.
        return self.bot.get_cog("ArchiveCog")

    async def _dm(self, user_id: int, content: str):
        try:
            user = await self.bot.fetch_user(user_id)
            await user.send(content)
        except discord.HTTPException:
            pass

    # ── 1. Review expiry ────────────────────────────────────────────────

    @tasks.loop(hours=6)
    async def review_expiry_loop(self):
        try:
            denied = await arc.expire_stale_reviews(REVIEW_EXPIRY_DAYS)
            for listing in denied:
                logger.info(f"[v0] archive automation: auto-denied stale review, listing={listing['id']}")
                await self._dm(
                    listing["submitter_id"],
                    f"❌ Your bot **{listing['bot_name']}** was auto-denied — it sat in review for over "
                    f"{REVIEW_EXPIRY_DAYS} days with no decision. Resubmit or contact the archive owner.",
                )
        except Exception:
            logger.exception("[v0] archive automation: review_expiry_loop failed")

    @review_expiry_loop.before_loop
    async def _before_review_expiry(self):
        await self.bot.wait_until_ready()

    # ── 2. Trending re-post ─────────────────────────────────────────────

    @tasks.loop(hours=1)
    async def trending_repost_loop(self):
        try:
            guild_ids = await arc.all_guild_ids_with_archive_enabled()
            for guild_id in guild_ids:
                await self._repost_trending(guild_id)
        except Exception:
            logger.exception("[v0] archive automation: trending_repost_loop failed")

    async def _repost_trending(self, guild_id: int):
        config = await arc.is_enabled(guild_id)
        if not config or not config.get("featured_channel_id"):
            return
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
        channel = guild.get_channel(config["featured_channel_id"])
        if not channel:
            return
        rows = await arc.trending(guild_id)
        if not rows:
            return
        lines = [f"**{i+1}.** {r['bot_name']} — {r['score']:.1f} pts" for i, r in enumerate(rows)]
        embed = discord.Embed(title="🌟 Trending Bots", description="\n".join(lines), color=discord.Color.gold())
        embed.set_footer(text="Auto-updates hourly")
        pin_ref = await arc.get_trending_pin(guild_id)
        if pin_ref and pin_ref.get("message_id"):
            try:
                msg = await channel.fetch_message(pin_ref["message_id"])
                await msg.edit(embed=embed)
                return
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass  # fall through and post a fresh one
        try:
            msg = await channel.send(embed=embed)
            await arc.set_trending_pin(guild_id, channel.id, msg.id)
        except discord.HTTPException:
            pass

    @trending_repost_loop.before_loop
    async def _before_trending(self):
        await self.bot.wait_until_ready()

    # ── 3. Dead-bot sweep ────────────────────────────────────────────────

    @tasks.loop(hours=6)
    async def dead_bot_sweep_loop(self):
        try:
            listings = await arc.approved_listings_for_rescan(DEAD_BOT_RESCAN_HOURS)
            for listing in listings:
                app_data = await arc.fetch_application(str(listing["application_id"]))
                await arc.mark_rescanned(listing["id"])
                if app_data is None:
                    await arc.set_status(listing["id"], "denied", "Auto-delisted: application no longer resolves on Discord.")
                    if self._archive_cog:
                        await self._archive_cog._delete_card(listing["id"])
                    await arc.send_webhook(listing["id"], "auto_delisted")
                    logger.info(f"[v0] archive automation: delisted dead bot listing={listing['id']}")
                    await self._dm(
                        listing["submitter_id"],
                        f"❌ **{listing['bot_name']}** was auto-delisted from the archive — its Discord "
                        f"application no longer resolves (deleted or removed). Resubmit if this is a mistake.",
                    )
        except Exception:
            logger.exception("[v0] archive automation: dead_bot_sweep_loop failed")

    @dead_bot_sweep_loop.before_loop
    async def _before_dead_bot(self):
        await self.bot.wait_until_ready()

    # ── 4. Boost expiry ──────────────────────────────────────────────────

    @tasks.loop(hours=1)
    async def boost_expiry_loop(self):
        try:
            expiring = await arc.boosts_expiring_soon(BOOST_WARNING_HOURS)
            for boost in expiring:
                await arc.mark_boost_warned(boost["id"])
                await self._dm(
                    boost["buyer_id"],
                    f"⏳ Your **{boost['tier']}** boost on **{boost['bot_name']}** expires within "
                    f"{BOOST_WARNING_HOURS}h. Run `/archive boost` again to keep it featured.",
                )
            just_expired = await arc.just_expired_boosted_listing_ids()
            for listing_id in just_expired:
                if self._archive_cog:
                    votes = await arc.vote_count(listing_id)
                    await self._archive_cog._refresh_card(listing_id, votes)
        except Exception:
            logger.exception("[v0] archive automation: boost_expiry_loop failed")

    @boost_expiry_loop.before_loop
    async def _before_boost(self):
        await self.bot.wait_until_ready()

    # ── 5 & 9. Risk + category recheck ──────────────────────────────────

    @tasks.loop(hours=24)
    async def risk_and_category_recheck_loop(self):
        try:
            listings = await arc.approved_listings_for_rescan(RISK_RESCAN_HOURS, limit=25)
            for listing in listings:
                app_data = await arc.fetch_application(str(listing["application_id"]))
                if not app_data:
                    continue  # dead_bot_sweep_loop handles delisting this case
                new_risk = await arc.score_submission(app_data, listing.get("description") or "", listing["submitter_id"])
                await arc.mark_rescored(listing["id"], new_risk)
                if new_risk >= arc.RISK_DENY_ABOVE and listing["risk_score"] < arc.RISK_DENY_ABOVE:
                    # Risk got materially worse since approval — flag for a human, don't auto-deny
                    # something that was already live without a person looking at it.
                    for admin_id in DISCORD_CLONE_ADMIN_IDS:
                        await self._dm(
                            admin_id,
                            f"⚠️ Listing #{listing['id']} (**{listing['bot_name']}**) re-scored risk "
                            f"{listing['risk_score']} → {new_risk}. Review with `/archive pending` "
                            f"or `/archive resolve listing_id:{listing['id']} approve:False`.",
                        )
                suggestion = await arc.suggest_category(listing.get("description") or "", listing["category"])
                if suggestion:
                    await self._dm(
                        listing["submitter_id"],
                        f"💡 **{listing['bot_name']}** is listed under **{listing['category']}**, but its "
                        f"description reads more like **{suggestion}**. No action taken — just flagging it "
                        f"in case you'd like to update it.",
                    )
        except Exception:
            logger.exception("[v0] archive automation: risk_and_category_recheck_loop failed")

    @risk_and_category_recheck_loop.before_loop
    async def _before_risk(self):
        await self.bot.wait_until_ready()

    # ── 6. Dispute reminders ─────────────────────────────────────────────

    @tasks.loop(hours=12)
    async def dispute_reminder_loop(self):
        try:
            stale = await arc.stale_open_disputes(DISPUTE_REMINDER_HOURS)
            for dispute in stale:
                await arc.mark_dispute_reminded(dispute["id"])
                for admin_id in DISCORD_CLONE_ADMIN_IDS:
                    await self._dm(
                        admin_id,
                        f"⚖️ Reminder — dispute on listing #{dispute['listing_id']} (**{dispute['bot_name']}**) "
                        f"has been open {DISPUTE_REMINDER_HOURS}h+ with no decision:\n> {dispute['message']}\n"
                        f"Use `/archive resolve listing_id:{dispute['listing_id']} approve:True/False`.",
                    )
        except Exception:
            logger.exception("[v0] archive automation: dispute_reminder_loop failed")

    @dispute_reminder_loop.before_loop
    async def _before_dispute(self):
        await self.bot.wait_until_ready()

    # ── 7. Webhook retry queue ───────────────────────────────────────────

    @tasks.loop(minutes=5)
    async def webhook_retry_loop(self):
        try:
            due = await arc.due_webhook_retries()
            for failure in due:
                listing = await arc.get_listing(failure["listing_id"])
                if not listing or not listing.get("webhook_url"):
                    await arc.clear_webhook_failure(failure["id"])
                    continue
                ok = await arc._post_webhook(listing["webhook_url"], listing.get("webhook_secret") or "", failure["payload"])
                if ok:
                    await arc.clear_webhook_failure(failure["id"])
                else:
                    backoff = min(5 * (2 ** failure["attempts"]), 240)  # cap at 4h
                    await arc.bump_webhook_retry(failure["id"], backoff)
        except Exception:
            logger.exception("[v0] archive automation: webhook_retry_loop failed")

    @webhook_retry_loop.before_loop
    async def _before_webhook_retry(self):
        await self.bot.wait_until_ready()

    # ── 10. Duplicate/orphan card cleanup ────────────────────────────────

    @tasks.loop(hours=6)
    async def duplicate_cleanup_loop(self):
        try:
            guild_ids = await arc.all_guild_ids_with_archive_enabled()
            for guild_id in guild_ids:
                await self._cleanup_guild_duplicates(guild_id)
        except Exception:
            logger.exception("[v0] archive automation: duplicate_cleanup_loop failed")

    async def _cleanup_guild_duplicates(self, guild_id: int):
        config = await arc.is_enabled(guild_id)
        guild = self.bot.get_guild(guild_id)
        if not config or not guild:
            return
        for channel_id in config["listing_channel_ids"]:
            channel = guild.get_channel(channel_id)
            if not channel:
                continue
            by_listing = {}
            try:
                async for msg in channel.history(limit=200):
                    if msg.author.id != self.bot.user.id or not msg.embeds:
                        continue
                    footer = msg.embeds[0].footer.text or ""
                    match = LISTING_FOOTER_RE.search(footer)
                    if not match:
                        continue
                    by_listing.setdefault(int(match.group(1)), []).append(msg)
            except discord.HTTPException:
                continue
            for listing_id, messages in by_listing.items():
                if len(messages) < 2:
                    continue
                listing = await arc.get_listing(listing_id)
                keep_id = listing.get("message_id") if listing else None
                for msg in messages:
                    if msg.id == keep_id:
                        continue
                    try:
                        await msg.delete()
                        logger.info(f"[v0] archive automation: deleted duplicate card msg={msg.id} listing={listing_id}")
                    except discord.HTTPException:
                        pass

    @duplicate_cleanup_loop.before_loop
    async def _before_cleanup(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(ArchiveAutomationCog(bot))
