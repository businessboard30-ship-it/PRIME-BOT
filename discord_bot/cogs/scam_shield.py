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
  3b. still nothing: ask Gemini (modules/scam_vision.py) about image attachments. Needs GEMINI_API_KEY (or OPENAI_API_KEY as the backup); skipped
      when over its budget; any error means "not a scam";
  4. on a match: save a copy (text + attachments) to the Scam Shield evidence channel (/admin scamchannel; falls back to\n     the image-hosting channel) if one is set, delete it, record\n     the hit, and post a flag in the server's log channel if it has one.
Webhook messages are checked too (raid bots love them); there is no "trusted" shortcut for them.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from datetime import timedelta
from types import SimpleNamespace
from typing import List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database import db
from discord_bot.cogs._admin_mount import mount_admin_command
from modules import scam_check as sc
from modules import scam_reputation as rep
from modules import scam_shield as ss
from modules import scam_media as sm
from modules import scam_ocr as sco
from modules import scam_qr as sqr
from modules import scam_risk as risk
from modules import scam_shield_actions as sa
from modules import scam_vision as sv
from discord_bot.cogs import _views_scamshield_wizard as wiz
from discord_bot.cogs import _views_scam_submit as submit

logger = logging.getLogger(__name__)

def _support_invite() -> str:
    """Read lazily: importing it at module load would make this cog depend on config having the name."""
    try:
        import config
        return getattr(config, "DISCORD_SUPPORT_SERVER_INVITE", "") or "our support server (see /help)"
    except Exception:
        return "our support server (see /help)"


_check_cooldown = sc.Cooldown()
_reported: dict = {}                         # (guild_id, message_id) -> monotonic time: one staff report per message
REPORT_DEDUPE_SECONDS = 3600

_warned: dict = {}                            # (guild_id, user_id) -> monotonic time until which we stay quiet

_IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")
_download_slots = asyncio.Semaphore(3)
NOTICE_SECONDS = 60                         # how long the in-channel "scam caught" notice (with its Submit button) stays
JOIN_FALLBACK_SECONDS = 300                 # a "flagged member joined" heads-up with no mod-log channel stays this long
JOIN_WARN_TTL = 6 * 3600                    # stay quiet about the same member in the same server this long (rejoin spam)
FALLBACK_SECONDS = 60                       # how long the flag stays when there is no usable mod-log channel (long enough for a mod to read it)
EVIDENCE_MAX_FILES = 4                      # attachments kept per caught message
EVIDENCE_MAX_BYTES = 8 * 1024 * 1024        # same cap as welcome backgrounds / ad images


_STORAGE_TTL = 60
_storage_cache: Tuple[float, frozenset] = (0.0, frozenset())


async def _storage_channel_ids() -> frozenset:
    """Channels PRIME bots store pictures/logs in (hosting + cat channel). Cached a minute. Never inspected:
    every bot and clone posts there, and their cat logs/pictures must not be deleted as 'scam caught'."""
    global _storage_cache
    ts, ids = _storage_cache
    if time.monotonic() - ts < _STORAGE_TTL:
        return ids
    try:
        from modules import image_host
        ids = frozenset(int(cid) for cid, _src in await image_host.cat_candidates())
    except Exception:
        logger.debug("[scam-shield] couldn't read the storage channel ids", exc_info=True)
    _storage_cache = (time.monotonic(), ids)
    return ids


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
        self._sweep_lock = asyncio.Lock()      # the startup sweep and a forced one never run at the same time
        self._reload.start()
        self._backfill.start()
        self._force_poll.start()

    def cog_unload(self):
        self._reload.cancel()
        self._backfill.cancel()
        self._force_poll.cancel()

    async def cog_load(self):
        mount_admin_command(
            self.bot, self.scamchannel, name="scamchannel",
            description="[Bot owner] Use THIS channel to keep copies of caught scam images (Scam Shield)",
        )
        self.bot.add_view(submit.ReviewView())      # Approve / Deny on submissions keep working after a restart
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

    # ── owner "force scan" from the admin panel ──────────────────────────
    # The panel button writes a request token to the database (ss.request_force_scan). Every bot, the main one and
    # each clone (separate processes), polls for it here and re-runs the sweep over its own servers right away.
    @tasks.loop(seconds=15)
    async def _force_poll(self):
        clone_id = getattr(self.bot, "clone_id", None)
        token = await ss.pending_force_scan(clone_id)
        if token is None:
            return
        try:
            await self._sweep_history(force=True)
        except Exception:
            logger.exception("[scam-shield] forced scan failed")
        finally:
            await ss.mark_force_scan_done(token, clone_id)    # even on failure: one tap must not loop forever

    @_force_poll.before_loop
    async def _before_force_poll(self):
        await self.bot.wait_until_ready()

    async def _sweep_history(self, *, force: bool = False) -> None:
        """Looks through the last few days of every readable channel and removes copies of the scam that were posted
        before the bot knew about it. Goes through the same _inspect as live messages (trusted staff skipped, server
        settings respected, evidence archived, hit logged).

        Normal run (startup): happens once per bot, after the dwinble rules exist; the 'done' marker is written only
        when the sweep reaches the end, so a restart halfway through just starts it again.
        force=True (owner's Force scan button): ignores that marker and runs again now."""
        clone_id = getattr(self.bot, "clone_id", None)
        if not force and await ss.backfill_done(clone_id):
            return
        async with self._sweep_lock:
            await ss.load(force=True)                 # makes sure the new rules are in memory (and seeded)
            if not ss.is_enabled():
                return                                # switched off: nothing to scan with
            if not force and not await ss.seed_done(ss.SEED_DWINBLE_KEY):
                return                                # new rules not stored yet: try next start
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
            if not force:
                await ss.mark_backfill_done(clone_id)
            logger.info("[scam-shield] %s sweep done: %s messages in %s channels across %s servers, %s caught",
                        "forced" if force else "history", scanned, channels, guilds, caught)
            await self._sweep_summary(scanned, caught, channels, guilds, forced=force)

    async def _sweep_summary(self, scanned: int, caught: int, channels: int, guilds: int, forced: bool = False) -> None:
        """Best effort: tell the owner what the sweep found, in the evidence channel if one is set."""
        try:
            host = await self._evidence_channel()
            if host is None:
                return
            who = f" ({self.bot.user})" if forced and self.bot.user else ""
            what = "forced scan" if forced else "history sweep"
            await host.send(
                f"\U0001F6E1\ufe0f **Scam Shield {what} finished**{who} | looked at {scanned} recent messages in "
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
        # Scammers sometimes edit the bait in afterwards. A link preview (embed) also arrives as an edit with the
        # same text, and so does a changed attachment list: those get looked at too.
        if (before.content != after.content
                or len(getattr(after, "embeds", None) or ()) > len(getattr(before, "embeds", None) or ())
                or len(getattr(after, "attachments", None) or ()) != len(getattr(before, "attachments", None) or ())):
            await self._inspect(after)

    # ── a member caught in another server joins this one ─────────────────
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        try:
            await self._warn_staff_about_join(member)
        except Exception:
            logger.debug("[scam-shield] join check failed", exc_info=True)

    async def _warn_staff_about_join(self, member: discord.Member) -> bool:
        """Scam Shield's catch log is shared by the main bot and every clone, so a member caught in one server is known
        in all of them. When they join another server, tell that server's staff (mod-log, else a self-deleting message
        in another channel). Informational only: nothing is done to the member, and no other server is named."""
        guild = member.guild
        if member.bot or guild is None or not ss.is_enabled():
            return False
        clone_id = getattr(self.bot, "clone_id", None)
        if not (await ss.guild_settings(guild.id, clone_id))["enabled"]:
            return False                                   # this server switched Scam Shield off: respect it
        key, now = (guild.id, member.id), time.monotonic()
        if _warned.get(key, 0) > now:
            return False
        found = await rep.lookup(member.id, guild.id)
        if found is None:
            return False
        if len(_warned) > 5000:
            _warned.clear()
        _warned[key] = now + JOIN_WARN_TTL
        # One staff warning per join, not one per bot in the server (same reason as _claim_message).
        joined = int(member.joined_at.timestamp()) if getattr(member, "joined_at", None) else int(time.time() // JOIN_WARN_TTL)
        if not await ss.claim(f"join:{guild.id}:{member.id}:{joined}", guild.id, clone_id):
            return False
        who = discord.utils.escape_markdown(str(member))[:40]
        since = f" Most recent: {discord.utils.format_dt(found['last_at'], 'R')}." if found["last_at"] else ""
        body = (f"**{who}** (`{member.id}`) just joined. Scam Shield caught scam messages from this account in "
                f"**{found['servers']}** other server{'s' if found['servers'] != 1 else ''} "
                f"({found['catches']} catch{'es' if found['catches'] != 1 else ''}).{since}\n"
                "The account may be run by a scammer or may have been hacked, so keep an eye on it. "
                "Nothing has been done to the member.")
        embed = discord.Embed(title="⚠️ Scam Shield: member flagged in other servers", description=body, color=discord.Color.orange())
        if found["kinds"]:
            embed.add_field(name="What was caught", value=", ".join(found["kinds"])[:300], inline=False)
        embed.add_field(name="Evidence",
                        value=("We keep a record of every catch and have evidence supporting this flag. "
                               f"To validate it, contact support: {_support_invite()}"), inline=False)
        mods = dict(allowed_mentions=discord.AllowedMentions.none())
        cfg = await db.get_automod_config(guild.id, clone_id=clone_id)
        ch_id = cfg.get("log_channel_id")
        ch = guild.get_channel(int(ch_id)) if ch_id else None
        if ch is not None and self._can_post(ch, guild):
            await ch.send(embed=embed, **mods)
            return True
        target = self._fallback_channel(guild)
        if target is None:
            return False
        embed.description += ("\n\nThis server has no mod-log channel I can post in, so I'm flagging it here. "
                              "Set one with `/modlog` to keep a permanent record.\n"
                              f"-# This message will be deleted in a short time ({JOIN_FALLBACK_SECONDS // 60} minutes).")
        await target.send(embed=embed, delete_after=JOIN_FALLBACK_SECONDS, **mods)
        return True

    # ── manual check: reply to a message, @mention the bot, write "scam shield" ─────────────
    @commands.Cog.listener("on_message")
    async def on_check_request(self, message: discord.Message):
        try:
            await self._check_request(message)
        except Exception:
            logger.exception("[scam-shield] manual check failed")

    async def _target_of(self, message: discord.Message) -> Optional[discord.Message]:
        ref = message.reference
        if ref is None or not ref.message_id:
            return None
        found = ref.resolved if isinstance(ref.resolved, discord.Message) else None
        if found is None:
            try:
                found = await message.channel.fetch_message(ref.message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None
        return found

    async def _answer(self, message: discord.Message, text: str, view=None, color=None) -> None:
        embed = discord.Embed(title="🛡️ Scam Shield check", description=text + f"\n-# This message will be deleted in {sc.ANSWER_SECONDS} seconds.",
                              color=color or discord.Color.blurple())
        try:
            await message.reply(embed=embed, view=view, mention_author=False, delete_after=sc.ANSWER_SECONDS,
                                allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            logger.debug("[scam-shield] couldn't post the check answer", exc_info=True)

    async def _check_request(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot or self.bot.user is None:
            return
        if self.bot.user not in message.mentions or message.reference is None:
            return
        if not sc.is_check_request(message.content):
            return
        if not self._can_post(message.channel, message.guild):
            return
        target = await self._target_of(message)
        if target is None:
            await self._answer(message, sc.GONE_TEXT)
            return
        if target.author.id == self.bot.user.id:
            return                                   # a reply to the bot's own message is normal AI chat
        wait = _check_cooldown.wait_seconds(message.guild.id, message.author.id)
        if wait > 0:
            await self._answer(message, sc.wait_text(wait))
            return
        if ss.stale():
            await ss.load()
        clone_id = getattr(self.bot, "clone_id", None)
        gs = await ss.guild_settings(message.guild.id, clone_id)
        if not ss.is_enabled() or not gs["enabled"]:
            await self._answer(message, sc.OFF_TEXT)
            return
        member = target.author if isinstance(target.author, discord.Member) else message.guild.get_member(target.author.id)
        if member is not None and _is_trusted(SimpleNamespace(author=member, guild=message.guild)):
            await self._answer(message, sc.STAFF_TEXT)
            return
        text = _text_of(target)
        has_pictures = any((a.content_type or "").startswith("image/") or a.filename.lower().endswith(_IMG_EXT)
                           for a in target.attachments)
        if not text.strip() and not target.attachments:
            await self._answer(message, sc.NOTHING_TEXT)
            return
        hit = ss.match_text(text, gs["allowed_domains"])
        if hit is None:
            hit = await self._match_pictures(target)
        if hit is not None:
            await self._remove_checked(message, target, hit, clone_id)
            return
        pictures_unchecked = False
        if has_pictures:
            try:
                cap, _prem = await sv.guild_cap(message.guild.id, clone_id)
                pictures_unchecked = not sv.available() or sv.guild_used_today(message.guild.id, clone_id) >= cap
            except Exception:
                pictures_unchecked = True
        await self._answer(message, sc.clean_text(pictures_unchecked), color=discord.Color.green())

    async def _remove_checked(self, message: discord.Message, target: discord.Message, hit, clone_id) -> None:
        """A manual check matched (known scam text/link/picture, or the AI scan said scam): delete the scam if we can.
        If we can't, say exactly why and keep the "Tell server staff" button."""
        kind, matched, rule_id = hit
        guild = message.guild
        reason = None
        try:
            can_delete = target.channel.permissions_for(guild.me).manage_messages
        except Exception:
            can_delete = True                        # can't tell: try, and report the real error
        if not can_delete:
            reason = sc.NO_PERMS_TEXT.format(channel=getattr(target.channel, "mention", "that channel"))
        elif not await ss.claim(f"msg:{target.id}", guild.id, clone_id):
            await self._answer(message, sc.OTHER_BOT_TEXT, color=discord.Color.red())
            return
        deleted = False
        if reason is None:
            host, files = await self._evidence(target)       # must run BEFORE the delete: CDN links die with the message
            try:
                await target.delete()
                deleted = True
            except discord.NotFound:
                deleted = True                       # already gone (another bot or the author got there first)
            except discord.Forbidden:
                reason = sc.FORBIDDEN_TEXT
            except discord.HTTPException as e:
                logger.warning("[scam-shield] manual check couldn't delete in guild %s (%s)", guild.id, e)
                reason = sc.ERROR_TEXT.format(code=getattr(e, "status", "?"))
            if host is not None:
                await self._archive(host, files, target, kind, matched, rule_id, deleted)
        try:
            snippet = discord.utils.escape_mentions((target.content or "")[:300]) or "(image / embed only)"
            await ss.log_hit(guild.id, target.channel.id, target.author.id, kind, matched, rule_id,
                             snippet, deleted, clone_id)
        except Exception:
            logger.debug("[scam-shield] couldn't record the manual-check hit", exc_info=True)
        if deleted:
            await self._answer(message, sc.deleted_text(kind), color=discord.Color.red())
            return
        view = _ReportView(self, message.author.id, target, kind, clone_id)
        await self._answer(message, sc.not_deleted_text(kind, reason), view=view, color=discord.Color.red())

    async def _report_to_staff(self, reporter: discord.abc.User, target: discord.Message, kind: str, clone_id) -> bool:
        """Tell the server's staff (mod-log channel first, else a self-deleting flag). Never deletes anything."""
        key = (target.guild.id, target.id)
        now = time.monotonic()
        if len(_reported) > 2000:
            for k in [k for k, v in _reported.items() if now - v > REPORT_DEDUPE_SECONDS]:
                _reported.pop(k, None)
        if now - _reported.get(key, -1e9) < REPORT_DEDUPE_SECONDS:
            return True                              # already reported: same answer for the member, no second flag
        _reported[key] = now
        guild = target.guild
        who = discord.utils.escape_markdown(str(target.author))[:40]
        cfg = await db.get_automod_config(guild.id, clone_id=clone_id)
        ch_id = cfg.get("log_channel_id")
        ch = guild.get_channel(int(ch_id)) if ch_id else None
        if ch is not None and self._can_post(ch, guild):
            snippet = discord.utils.escape_mentions((target.content or "")[:300]) or "(image / embed only)"
            embed = discord.Embed(title="🛡️ Member asked Scam Shield to check a message", description=snippet[:1000],
                                  color=discord.Color.red())
            embed.add_field(name="Author", value=f"{target.author} (`{target.author.id}`)")
            embed.add_field(name="Reported by", value=f"{reporter} (`{reporter.id}`)")
            embed.add_field(name="Result", value=sc.kind_word(kind))
            embed.add_field(name="Message", value=f"[Jump to it]({target.jump_url})", inline=False)
            await ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            return True
        fb = self._fallback_channel(guild, target.channel)
        if fb is None:
            return False
        embed = discord.Embed(
            title="🛡️ Scam Shield: a member reported a message",
            description=(f"A message from **{who}** was checked and looks like a scam ({sc.kind_word(kind)}). "
                         f"[Jump to it]({target.jump_url}). Staff: this server has no mod-log channel I can post in, "
                         f"so I'm flagging it here (set one with `/modlog`).\n"
                         f"-# This message will be deleted in {FALLBACK_SECONDS} seconds."), color=discord.Color.red())
        await fb.send(embed=embed, delete_after=FALLBACK_SECONDS, allowed_mentions=discord.AllowedMentions.none())
        return True

    async def _inspect(self, message: discord.Message) -> bool:
        if message.guild is None or message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return False
        if self.bot.user and message.author.id == self.bot.user.id:
            return False
        if getattr(message.author, "bot", False) is True and getattr(message, "webhook_id", None) is None:
            # Never inspect another bot's messages (the main bot and every clone can share a server). Their Scam
            # Shield notices and staff flags quote the scam they caught, so inspecting them made the bots delete
            # each other's alerts in a loop. Webhooks stay inspected (scammers use them).
            return False
        if getattr(message.channel, "id", None) in await _storage_channel_ids():
            return False                      # the bots' own picture/log storage channel: never a scam, never touched
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
                hit = await self._strict_hit(message, text)       # premium, opt-in per server; rare path
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
            if not await self._claim_message(message):
                return False                     # another bot in this server is already handling it
            await self._act(message, *hit, action=gs.get("punish_action") or "delete")
            return True
        except Exception:
            logger.exception("[scam-shield] failed while checking a message")
            return False

    async def _claim_message(self, message: discord.Message) -> bool:
        """Several bots can share a server (the main bot, clones, or two copies of one bot), and each sees this
        message. Exactly one should delete / archive / log / warn. A bot that can't delete here waits a moment,
        so one that can is chosen first; if nobody can, the first one still flags it ("couldn't delete")."""
        if not self._can_delete(message):
            await asyncio.sleep(ss.CLAIM_GRACE_SECONDS)
        return await ss.claim(f"msg:{message.id}", message.guild.id, getattr(self.bot, "clone_id", None))

    @staticmethod
    def _can_delete(message: discord.Message) -> bool:
        try:
            return bool(message.channel.permissions_for(message.guild.me).manage_messages)
        except Exception:
            return True                          # can't tell: don't hold it back

    async def _strict_hit(self, message: discord.Message, text: str):
        """Strict mode (premium, per server). Costs nothing for servers that never switched it on: the first
        check is an in-memory set lookup. Premium is only looked up after a strict check actually matched, and
        if premium lapsed the saved choice is kept but paused."""
        clone_id = getattr(self.bot, "clone_id", None)
        if not ss.strict_on(message.guild.id, clone_id):
            return None
        hit = ss.match_strict(text)
        if hit is None:
            return None
        gs = await ss.guild_settings(message.guild.id, clone_id)
        if gs["allowed_domains"]:
            hit = ss.match_strict(text, gs["allowed_domains"])
            if hit is None:
                return None
        from modules import antiraid_pro as pro
        if not await pro.is_premium(message.guild.id, clone_id):
            return None
        return hit

    # ── /scamshield ──────────────────────────────────────────────────────
    @app_commands.command(name="scamshield",
                          description="Scam Shield: status, allowed domains, link checker and premium extras")
    @app_commands.guild_only()
    async def scamshield_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = await wiz._auth(interaction, interaction.guild_id)
        if guild is None:
            return
        await wiz.open_wizard(interaction, guild, getattr(self.bot, "clone_id", None))

    async def _match_pictures(self, message: discord.Message):
        """Hash rules first (free), then Gemini (budgeted). Each attachment is downloaded at most once."""
        if not message.attachments and not getattr(message, "embeds", None):
            return None
        cache: dict = {}
        hit = None
        if ss.has_image_rules():
            hit = await self._match_images(message, cache)
        if hit is None and sqr.available():
            hit = await self._match_qr(message, cache)           # free + fast: link hidden in a QR code
        if hit is None and sco.available():
            hit = await self._match_ocr(message, cache)          # free: read the words in the picture first
        if hit is None:
            hit = await self._match_extra(message, cache)        # free: animated GIF/WebP frames + link-preview images
        if hit is None and sv.available() and message.attachments:
            hit = await self._match_vision(message, cache)       # budgeted AI check last
        return hit

    async def _match_extra(self, message: discord.Message, cache: dict):
        """Free checks (hash rules, QR codes, OCR text) on pictures the attachment stages skip: 2-3 frames of an
        animated GIF / WebP, and the images inside link-preview embeds (Discord's proxied copies only)."""
        items = []                                                # (data, is_attachment, ocr_ok)
        embed_pics = sm.embed_urls(message) if sm.enabled() else []
        has_anim = any(
            (a.content_type or "").split(";")[0].lower() in ("image/gif", "image/webp")
            or a.filename.lower().endswith((".gif", ".webp")) for a in message.attachments[:3])
        if not has_anim and not embed_pics:
            return None                                           # nothing here for this stage: costs nothing
        # Expensive: only for new / suspicious senders, at most once per 10 s per user and 30 a minute overall.
        if not (risk.risky(message) and risk.allow(getattr(message.author, "id", 0))):
            return None
        for att in message.attachments[:3]:
            ctype = (att.content_type or "").split(";")[0].lower()
            name = att.filename.lower()
            if not (ctype in ("image/gif", "image/webp") or name.endswith((".gif", ".webp"))):
                continue
            if att.size > ss.IMAGE_MAX_BYTES:
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
            items.append((data, True, True))
        for url, ocr_ok in embed_pics:
            if sm.seen_clean(url):
                continue
            data = cache.get("emb:" + url)
            if data is None:
                async with _download_slots:
                    data = await sm.fetch(url)
                if data is None:
                    continue
                cache["emb:" + url] = data
            items.append((data, False, ocr_ok))
        if not items:
            return None
        loop = asyncio.get_running_loop()
        gs = await ss.guild_settings(message.guild.id, getattr(self.bot, "clone_id", None))
        allowed = gs["allowed_domains"]
        for data, is_att, ocr_ok in items[:4]:
            frs = await loop.run_in_executor(None, sm.frames, data)
            if is_att and len(frs) <= 1:
                continue                                          # a still WebP: the normal stages already checked it
            for i, fr in enumerate(frs):
                hit = ss.match_image(fr) if ss.has_image_rules() else None
                if hit is None and sqr.available():
                    hit = await sqr.scan(fr, allowed)
                if hit is None and ocr_ok and sco.available() and i < 2:
                    hit = await sco.scan(fr, allowed, deep=False)
                if hit:
                    if hit[0] != "image" and hit[1].startswith(("word:", "domain:")):
                        await ss.learn_image(fr, f"{hit[0]} {hit[1]}"[:150])
                    return hit
        for url, _ in embed_pics:
            if ("emb:" + url) in cache:
                sm.mark_clean(url)                                # downloaded and clean: never fetch it again
        return None

    async def _match_qr(self, message: discord.Message, cache: dict):
        """Decode QR codes in up to 2 image attachments and run the link through the scam rules."""
        for att in message.attachments[:2]:
            ctype = (att.content_type or "").split(";")[0].lower()
            name = att.filename.lower()
            is_img = ctype.startswith("image/") or name.endswith(_IMG_EXT)
            if not is_img or ctype == "image/gif" or name.endswith(".gif") or att.size > ss.IMAGE_MAX_BYTES:
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
            gs = await ss.guild_settings(message.guild.id, getattr(self.bot, "clone_id", None))
            hit = await sqr.scan(data, gs["allowed_domains"])
            if hit:
                if hit[1].startswith(("word:", "domain:")):
                    await ss.learn_image(data, f"QR {hit[1]}"[:150])
                return hit
        return None

    async def _match_ocr(self, message: discord.Message, cache: dict):
        """Read the text out of up to 2 image attachments and run it through the normal text rules (words, scam
        domains, giveaway/promo-code bait). Catches a caption-less scam picture. Animated GIFs and tiny images skip."""
        for att in message.attachments[:2]:
            ctype = (att.content_type or "").split(";")[0].lower()
            name = att.filename.lower()
            is_img = ctype.startswith("image/") or name.endswith(_IMG_EXT)
            if not is_img or ctype == "image/gif" or name.endswith(".gif") or att.size > ss.IMAGE_MAX_BYTES:
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
            gs = await ss.guild_settings(message.guild.id, getattr(self.bot, "clone_id", None))
            hit = await sco.scan(data, gs["allowed_domains"], deep=risk.risky(message))
            if hit:
                if hit[1].startswith(("word:", "domain:")):      # a curated rule matched: safe to remember the picture
                    await ss.learn_image(data, f"OCR {hit[1]}"[:150])
                return hit
        return None

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
                await ss.learn_image(data, "AI vision")
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

    async def _act(self, message: discord.Message, kind: str, matched: str, rule_id: Optional[int],
                   action: str = "delete") -> None:
        host, files = await self._evidence(message)
        deleted = False
        try:
            await message.delete()
            deleted = True
        except discord.HTTPException as e:
            logger.warning("[scam-shield] couldn't delete in guild %s (%s)", message.guild.id, e)
        if host is not None:
            await self._archive(host, files, message, kind, matched, rule_id, deleted)
        clone_id = getattr(self.bot, "clone_id", None)
        action_note = ""
        if action != "delete":
            # The server chose what happens to the sender (join DM / /scamshield > Scam Shield settings). Done before the
            # throttle below so a repeat offender is still actioned, and never raises (see modules/scam_shield_actions).
            _done, action_note = await sa.punish(message.guild, clone_id, message.author, action, f"{kind}: {matched}"[:200])
        if ss.throttled(message.guild.id, message.author.id):
            return                                    # already flagged this user a moment ago
        snippet = discord.utils.escape_mentions((message.content or "")[:300]) or "(image / embed only)"
        await ss.log_hit(message.guild.id, message.channel.id, message.author.id, kind, matched, rule_id,
                         snippet, deleted, clone_id)
        fallback = await self._flag(message, kind, matched, deleted, snippet, clone_id, action_note)
        if fallback is not message.channel:           # the fallback flag already warns the channel it landed in
            await self._notice(message, deleted)

    async def _notice(self, message: discord.Message, deleted: bool) -> None:
        """Short public heads-up in the channel where the scam was posted, so members aren't left wondering.
        It names nobody with a ping, repeats nothing from the scam, and removes itself after NOTICE_SECONDS.
        Best effort: a missing permission just means no notice."""
        try:
            who = discord.utils.escape_markdown(str(message.author))[:40]
            embed = discord.Embed(
                title="🛡️ Scam Shield",
                description=(f"A scam message from **{who}** was caught here"
                             f"{' and removed' if deleted else ''}. Don't click links or scan codes from it, "
                             "and never share your login or send crypto to claim a prize.\n"
                             f"-# This notice will be deleted in a short time ({NOTICE_SECONDS} seconds). "
                             "Saw another scam? Press the button to send it to us."),
                color=discord.Color.red())
            await message.channel.send(embed=embed, delete_after=NOTICE_SECONDS, view=submit.SubmitScamView(NOTICE_SECONDS),
                                       allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            logger.debug("[scam-shield] couldn't post the in-channel notice", exc_info=True)

    @staticmethod
    def _can_post(ch, guild) -> bool:
        """True when the bot can see the channel, send messages and use embeds there."""
        try:
            me = guild.me
            if ch is None or me is None:
                return False
            p = ch.permissions_for(me)
            return bool(p.view_channel and p.send_messages and p.embed_links)
        except Exception:
            return False

    def _fallback_channel(self, guild, preferred=None):
        """Where to post the flag when the server has no (usable) mod-log channel: the channel the scam was in, then the
        server's system channel, then the first text channel the bot can write in. None when it can't write anywhere."""
        g = guild
        seen = set()
        for ch in [preferred, getattr(g, "system_channel", None),
                   *sorted(getattr(g, "text_channels", []) or [], key=lambda c: c.position)]:
            if ch is None or getattr(ch, "id", None) in seen or not isinstance(ch, (discord.TextChannel, discord.Thread)):
                continue
            seen.add(ch.id)
            if self._can_post(ch, g):
                return ch
        return None

    async def _flag(self, message, kind, matched, deleted, snippet, clone_id, action_note: str = ""):
        """Tell the staff about the catch. Mod-log channel first (full details, permanent). If the server has none, or it
        was deleted, or the bot can't write there, post a short self-deleting flag in another channel instead.
        Returns the fallback channel it posted into, else None (mod-log used, or nowhere to post)."""
        try:
            cfg = await db.get_automod_config(message.guild.id, clone_id=clone_id)
            ch_id = cfg.get("log_channel_id")
            ch = message.guild.get_channel(int(ch_id)) if ch_id else None
            if ch is not None and self._can_post(ch, message.guild):
                embed = discord.Embed(title="🛡️ Scam Shield caught a scam message",
                                      description=snippet[:1000], color=discord.Color.red())
                embed.add_field(name="User", value=f"{message.author} (`{message.author.id}`)")
                embed.add_field(name="Channel", value=message.channel.mention)
                embed.add_field(name="Matched", value=f"{kind}: {discord.utils.escape_markdown(matched)}"[:200])
                embed.add_field(name="Message", value="🗑️ deleted" if deleted else "⚠️ couldn't delete (missing permission)")
                if action_note:
                    embed.add_field(name="Sender", value=action_note[:200], inline=False)
                from discord_bot.cogs._views_join_dm_scamshield import FlagSettingsButton
                flag_view = discord.ui.View(timeout=None)
                flag_view.add_item(FlagSettingsButton(message.guild.id, clone_id))
                await ch.send(embed=embed, view=flag_view, allowed_mentions=discord.AllowedMentions.none())
                return None
            target = self._fallback_channel(message.guild, message.channel)
            if target is None:
                return None
            # This lands in a public channel, so it never repeats the scam text or the rule that matched.
            who = discord.utils.escape_markdown(str(message.author))[:40]
            where = message.channel.mention if target is not message.channel else "this channel"
            embed = discord.Embed(
                title="🛡️ Scam Shield caught a scam message",
                description=(f"A scam message from **{who}** (`{message.author.id}`) was caught in {where}"
                             f"{' and removed' if deleted else ', but I could not remove it (I need Manage Messages)'}.\n"
                             f"Type: `{discord.utils.escape_markdown(str(kind))[:30]}`. "
                             "Don't click links or scan codes from it, and never share your login or send crypto to claim a prize.\n"
                             + (f"{action_note}\n" if action_note else "") + "\n"
                             "This server has no mod-log channel I can post in, so I'm flagging it here. "
                             "Set one with `/modlog` to keep a permanent record.\n"
                             f"-# This message will be deleted in a short time ({FALLBACK_SECONDS} seconds)."),
                color=discord.Color.red())
            await target.send(embed=embed, delete_after=FALLBACK_SECONDS, view=submit.SubmitScamView(FALLBACK_SECONDS), allowed_mentions=discord.AllowedMentions.none())
            return target
        except Exception:
            logger.debug("[scam-shield] couldn't post the flag", exc_info=True)
            return None


class _ReportView(discord.ui.View):
    """One button under a "this looks like a scam" answer. Only the member who asked can press it."""

    def __init__(self, cog: "ScamShieldCog", asker_id: int, target: discord.Message, kind: str, clone_id):
        super().__init__(timeout=sc.ANSWER_SECONDS)
        self.cog, self.asker_id, self.target, self.kind, self.clone_id = cog, asker_id, target, kind, clone_id
        btn = discord.ui.Button(label="Tell server staff", emoji="🚨", style=discord.ButtonStyle.danger)
        btn.callback = self._on_report
        self.add_item(btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.asker_id:
            await interaction.response.send_message("Only the person who asked for this check can use this button.", ephemeral=True)
            return False
        return True

    async def _on_report(self, interaction: discord.Interaction):
        try:
            ok = await self.cog._report_to_staff(interaction.user, self.target, self.kind, self.clone_id)
        except Exception:
            logger.exception("[scam-shield] report to staff failed")
            ok = False
        for item in self.children:
            item.disabled = True
        note = "🚨 Server staff have been told. Thank you." if ok else \
            "I couldn't reach the staff (no channel I can post in). Please message a moderator directly."
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(note, ephemeral=True)
        self.stop()


async def setup(bot: commands.Bot):
    await bot.add_cog(ScamShieldCog(bot))
