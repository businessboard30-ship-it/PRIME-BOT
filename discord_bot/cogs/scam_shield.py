# path: discord_bot/cogs/scam_shield.py

"""
Scam Shield listener: deletes known scam messages in EVERY server the bot is in and flags each catch.

On by default everywhere; the owner can switch it off globally from /admin -> Scam Shield, and each server can
switch it off for itself or allow specific domains from /serversetup -> Moderation -> Join gate & Scam Shield.
Rules and matching live in modules/scam_shield.py (memory-cached, no database call per message).

Flow per message:
  1. skip our own / DMs / system messages and trusted staff (Manage Messages, Manage Server, Admin);
  2. match the text (content + embed text + attachment file names);
  3. only if no text match AND image rules exist: hash small image attachments and compare;
  3b. still nothing: ask Gemini (modules/scam_vision.py) about image attachments. Needs GEMINI_API_KEY; skipped
      when over its budget; any error means "not a scam";
  4. on a match: save a copy (text + attachments) to the Scam Shield evidence channel (/admin scamchannel; falls back to\n     the image-hosting channel) if one is set, delete it, record\n     the hit, and post a flag in the server's log channel if it has one.
Webhook messages are checked too (raid bots love them); there is no "trusted" shortcut for them.
"""

from __future__ import annotations

import asyncio
import io
import logging
from datetime import timedelta
from typing import List, Optional, Tuple

import discord
from discord.ext import commands, tasks

from database import db
from discord_bot.cogs._admin_mount import mount_admin_command
from modules import scam_shield as ss
from modules import scam_vision as sv

logger = logging.getLogger(__name__)

_IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")
_download_slots = asyncio.Semaphore(3)
EVIDENCE_MAX_FILES = 4                      # attachments kept per caught message
EVIDENCE_MAX_BYTES = 8 * 1024 * 1024        # same cap as welcome backgrounds / ad images


def _is_trusted(message: discord.Message) -> bool:
    a = message.author
    if not isinstance(a, discord.Member):
        return False                      # webhooks / users that left: no free pass
    p = a.guild_permissions
    return bool(p.administrator or p.manage_guild or p.manage_messages or a.id == message.guild.owner_id)


def _text_of(message: discord.Message) -> str:
    parts = [message.content or ""]
    for e in message.embeds:
        parts += [e.title or "", e.description or "", e.url or "", e.author.name if e.author else ""]
        parts += [f"{f.name} {f.value}" for f in e.fields]
    parts += [a.filename for a in message.attachments]
    return "\n".join(p for p in parts if p)


class ScamShieldCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._reload.start()
        self._backfill.start()

    def cog_unload(self):
        self._reload.cancel()
        self._backfill.cancel()

    async def cog_load(self):
        mount_admin_command(
            self.bot, self.scamchannel, name="scamchannel",
            description="[Bot owner] Use THIS channel to keep copies of caught scam images (Scam Shield)",
        )
        await ss.load(force=True)

    # Mounted as /admin scamchannel (see cog_load). Same rules as /admin hostingchannel, but only for Scam Shield.
    async def scamchannel(self, interaction: discord.Interaction):
        from config import DISCORD_OWNER_BROADCAST_IDS
        if interaction.user.id not in DISCORD_OWNER_BROADCAST_IDS:
            await interaction.response.send_message("This command is restricted to bot owners.", ephemeral=True)
            return
        if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Run this in a server text channel.", ephemeral=True)
            return
        perms = interaction.channel.permissions_for(interaction.guild.me)
        if not (perms.send_messages and perms.attach_files and perms.read_message_history):
            await interaction.response.send_message(
                "I need Send Messages, Attach Files, and Read Message History in this channel.", ephemeral=True)
            return
        try:
            await ss.set_evidence_channel(interaction.channel.id)
        except Exception:
            logger.exception("[scam-shield] couldn't save the evidence channel")
            await interaction.response.send_message("Couldn't save that (database problem). Nothing changed.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"✅ Caught scam messages (text + images) will now be saved in #{interaction.channel.name}. "
            "Keep it private. The image-hosting channel is no longer used for this.", ephemeral=True)

    @tasks.loop(minutes=5)
    async def _reload(self):
        await ss.load(force=True)

    @_reload.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    # ── one-time look back at recent history ─────────────────────────────
    @tasks.loop(count=1)
    async def _backfill(self):
        try:
            await self._sweep_history()
        except Exception:
            logger.exception("[scam-shield] history sweep failed; it will try again on the next start")

    @_backfill.before_loop
    async def _before_backfill(self):
        await self.bot.wait_until_ready()

    async def _sweep_history(self) -> None:
        """Runs once per bot after the dwinble rules exist: looks through the last few days of every readable
        channel and removes copies of the scam that were posted before the bot knew about it. Goes through the
        same _inspect as live messages (trusted staff skipped, server settings respected, evidence archived,
        hit logged). The 'done' marker is written only when the sweep reaches the end, so a restart halfway
        through just starts it again."""
        clone_id = getattr(self.bot, "clone_id", None)
        if await ss.backfill_done(clone_id):
            return
        await ss.load(force=True)                 # makes sure the new rules are in memory (and seeded)
        if not ss.is_enabled() or not await ss.seed_done(ss.SEED_DWINBLE_KEY):
            return                                # switched off, or new rules not stored yet: try next start
        cutoff = discord.utils.utcnow() - timedelta(days=ss.BACKFILL_DAYS)
        scanned = caught = channels = guilds = 0
        for guild in list(self.bot.guilds):
            if self.bot.is_closed():
                return                            # shutting down: no marker, so it resumes next start
            gs = await ss.guild_settings(guild.id, clone_id)
            me = guild.me
            if not gs["enabled"] or me is None:
                continue
            guilds += 1
            for ch in guild.text_channels:
                perms = ch.permissions_for(me)
                if not (perms.view_channel and perms.read_message_history):
                    continue
                channels += 1
                try:
                    async for msg in ch.history(limit=ss.BACKFILL_PER_CHANNEL, after=cutoff):
                        scanned += 1
                        if await self._inspect(msg):
                            caught += 1
                except discord.HTTPException as e:
                    logger.debug("[scam-shield] sweep couldn't read #%s in %s (%s)", ch.id, guild.id, e)
                await asyncio.sleep(ss.BACKFILL_CHANNEL_PAUSE)
        await ss.mark_backfill_done(clone_id)
        logger.info("[scam-shield] history sweep done: %s messages in %s channels across %s servers, %s caught",
                    scanned, channels, guilds, caught)
        await self._sweep_summary(scanned, caught, channels, guilds)

    async def _sweep_summary(self, scanned: int, caught: int, channels: int, guilds: int) -> None:
        """Best effort: tell the owner what the sweep found, in the evidence channel if one is set."""
        try:
            host = await self._evidence_channel()
            if host is None:
                return
            await host.send(
                f"\U0001F6E1\ufe0f **Scam Shield history sweep finished** | looked at {scanned} recent messages in "
                f"{channels} channels across {guilds} servers | **{caught} scam message(s) removed**",
                allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            logger.debug("[scam-shield] couldn't post the sweep summary", exc_info=True)

    # ── listeners ────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        await self._inspect(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.content != after.content:           # scammers sometimes edit the bait in afterwards
            await self._inspect(after)

    async def _inspect(self, message: discord.Message) -> bool:
        if message.guild is None or message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return False
        if self.bot.user and message.author.id == self.bot.user.id:
            return False
        if ss.stale():
            await ss.load()
        if not ss.is_enabled():
            return False
        try:
            if _is_trusted(message):
                return False
            text = _text_of(message)
            hit = ss.match_text(text)
            if hit is None:
                hit = await self._match_pictures(message)
            if hit is None:
                return False
            # Only now (a real match) look at this server's own settings: it can switch the shield
            # off for itself or allow specific domains. Cached, so this is not a per-message query.
            gs = await ss.guild_settings(message.guild.id, getattr(self.bot, "clone_id", None))
            if not gs["enabled"]:
                return False
            if gs["allowed_domains"] and hit[0] == "domain":
                hit = ss.match_text(text, gs["allowed_domains"])
                if hit is None:
                    hit = await self._match_pictures(message)
                if hit is None:
                    return False
            await self._act(message, *hit)
            return True
        except Exception:
            logger.exception("[scam-shield] failed while checking a message")
            return False

    async def _match_pictures(self, message: discord.Message):
        """Hash rules first (free), then Gemini (budgeted). Each attachment is downloaded at most once."""
        if not message.attachments:
            return None
        cache: dict = {}
        hit = None
        if ss.has_image_rules():
            hit = await self._match_images(message, cache)
        if hit is None and sv.available():
            hit = await self._match_vision(message, cache)
        return hit

    async def _match_images(self, message: discord.Message, cache: Optional[dict] = None):
        for att in message.attachments[:4]:
            is_img = (att.content_type or "").startswith("image/") or att.filename.lower().endswith(_IMG_EXT)
            if not is_img or att.size > ss.IMAGE_MAX_BYTES:
                continue
            try:
                async with _download_slots:
                    data = await asyncio.wait_for(att.read(), timeout=6)
            except Exception:
                continue
            if cache is not None:
                cache[att.id] = data
            hit = await asyncio.get_running_loop().run_in_executor(None, ss.match_image, data)
            if hit:
                return hit
        return None

    async def _match_vision(self, message: discord.Message, cache: dict):
        """Ask Gemini about up to 3 image attachments. Animated GIFs and tiny images are skipped."""
        for att in message.attachments[:3]:
            ctype = (att.content_type or "").split(";")[0].lower()
            name = att.filename.lower()
            is_img = ctype.startswith("image/") or name.endswith(_IMG_EXT)
            if not is_img or ctype == "image/gif" or name.endswith(".gif"):
                continue
            w = att.width if isinstance(att.width, int) else None
            h = att.height if isinstance(att.height, int) else None
            if not sv.worth_checking(att.size, w, h):
                continue
            data = cache.get(att.id)
            if data is None:
                try:
                    async with _download_slots:
                        data = await asyncio.wait_for(att.read(), timeout=8)
                except Exception:
                    continue
                cache[att.id] = data
            hit = await sv.is_scam_image(data, message.guild.id, message.author.id,
                                         getattr(self.bot, "clone_id", None))
            if hit:
                return hit
        return None

    async def _evidence_channel(self) -> Optional[discord.TextChannel]:
        """Scam Shield's own evidence channel (/admin scamchannel); falls back to the image-hosting channel."""
        cid = ss.evidence_channel_id()
        if cid:
            ch = self.bot.get_channel(cid)
            if ch is None:
                try:
                    ch = await self.bot.fetch_channel(cid)
                except discord.HTTPException:
                    ch = None
            if isinstance(ch, discord.TextChannel):
                return ch
        from discord_bot.ad_images import _host_channel
        return await _host_channel(self.bot)

    # ── what we do when something matches ────────────────────────────────
    async def _evidence(self, message: discord.Message) -> Tuple[Optional[discord.TextChannel], List[Tuple[str, bytes]]]:
        """The evidence channel (/admin scamchannel, else the image-hosting channel from /admin hostingchannel) plus the
        message's attachments as bytes. Must run BEFORE the delete: Discord CDN links die with the message.
        (None, []) when no hosting channel is set or the bot can't reach it: nothing is downloaded then."""
        try:
            host = await self._evidence_channel()
        except Exception:
            return None, []
        if host is None:
            return None, []
        got: List[Tuple[str, bytes]] = []
        for att in message.attachments[:EVIDENCE_MAX_FILES]:
            if att.size > EVIDENCE_MAX_BYTES:
                continue
            try:
                async with _download_slots:
                    data = await asyncio.wait_for(att.read(), timeout=6)
                got.append((att.filename, data))
            except Exception:
                continue
        return host, got

    async def _archive(self, host, files, message, kind, matched, rule_id, deleted) -> None:
        """Drop the caught scam into the hosting channel (text + the files) so you keep a copy of everything
        Scam Shield removed, e.g. to add new rules from. Best effort: never blocks or breaks the delete."""
        try:
            body = (message.content or "")[:1200] or "(image / embed only)"
            head = (f"\U0001F6E1\ufe0f **Scam Shield catch** | {kind}: `{discord.utils.escape_markdown(str(matched))[:80]}`"
                    f"{f' (rule {rule_id})' if rule_id else ''} | server `{message.guild.id}` | user `{message.author.id}` "
                    f"| {'deleted' if deleted else 'NOT deleted'}\n>>> {discord.utils.escape_mentions(body)}")
            await host.send(content=head[:1990],
                            files=[discord.File(io.BytesIO(d), filename=f"scam_{i}_{n}"[:100]) for i, (n, d) in enumerate(files)],
                            allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            logger.debug("[scam-shield] couldn't archive the catch", exc_info=True)

    async def _act(self, message: discord.Message, kind: str, matched: str, rule_id: Optional[int]) -> None:
        host, files = await self._evidence(message)
        deleted = False
        try:
            await message.delete()
            deleted = True
        except discord.HTTPException as e:
            logger.warning("[scam-shield] couldn't delete in guild %s (%s)", message.guild.id, e)
        if host is not None:
            await self._archive(host, files, message, kind, matched, rule_id, deleted)
        if ss.throttled(message.guild.id, message.author.id):
            return                                    # already flagged this user a moment ago
        clone_id = getattr(self.bot, "clone_id", None)
        snippet = discord.utils.escape_mentions((message.content or "")[:300]) or "(image / embed only)"
        await ss.log_hit(message.guild.id, message.channel.id, message.author.id, kind, matched, rule_id,
                         snippet, deleted, clone_id)
        await self._flag(message, kind, matched, deleted, snippet, clone_id)

    async def _flag(self, message, kind, matched, deleted, snippet, clone_id) -> None:
        try:
            cfg = await db.get_automod_config(message.guild.id, clone_id=clone_id)
            ch_id = cfg.get("log_channel_id")
            ch = message.guild.get_channel(int(ch_id)) if ch_id else None
            if ch is None:
                return
            embed = discord.Embed(title="🛡️ Scam Shield caught a scam message",
                                  description=snippet[:1000], color=discord.Color.red())
            embed.add_field(name="User", value=f"{message.author} (`{message.author.id}`)")
            embed.add_field(name="Channel", value=message.channel.mention)
            embed.add_field(name="Matched", value=f"{kind}: {discord.utils.escape_markdown(matched)}"[:200])
            embed.add_field(name="Message", value="🗑️ deleted" if deleted else "⚠️ couldn't delete (missing permission)")
            await ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            logger.debug("[scam-shield] couldn't post the flag", exc_info=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ScamShieldCog(bot))
