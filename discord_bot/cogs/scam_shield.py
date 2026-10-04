# path: discord_bot/cogs/scam_shield.py

"""
Scam Shield listener: deletes known scam messages in EVERY server the bot is in and flags each catch.

On by default everywhere; the owner can switch it off globally from /admin -> Scam Shield.
Rules and matching live in modules/scam_shield.py (memory-cached, no database call per message).

Flow per message:
  1. skip our own / DMs / system messages and trusted staff (Manage Messages, Manage Server, Admin);
  2. match the text (content + embed text + attachment file names);
  3. only if no text match AND image rules exist: hash small image attachments and compare;
  4. on a match: delete it, record the hit, and post a flag in the server's log channel if it has one.
Webhook messages are checked too (raid bots love them); there is no "trusted" shortcut for them.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord
from discord.ext import commands, tasks

from database import db
from modules import scam_shield as ss

logger = logging.getLogger(__name__)

_IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")
_download_slots = asyncio.Semaphore(3)


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

    def cog_unload(self):
        self._reload.cancel()

    async def cog_load(self):
        await ss.load(force=True)

    @tasks.loop(minutes=5)
    async def _reload(self):
        await ss.load(force=True)

    @_reload.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    # ── listeners ────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        await self._inspect(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.content != after.content:           # scammers sometimes edit the bait in afterwards
            await self._inspect(after)

    async def _inspect(self, message: discord.Message) -> None:
        if message.guild is None or message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return
        if self.bot.user and message.author.id == self.bot.user.id:
            return
        if ss.stale():
            await ss.load()
        if not ss.is_enabled():
            return
        try:
            if _is_trusted(message):
                return
            hit = ss.match_text(_text_of(message))
            if hit is None and ss.has_image_rules():
                hit = await self._match_images(message)
            if hit is None:
                return
            await self._act(message, *hit)
        except Exception:
            logger.exception("[scam-shield] failed while checking a message")

    async def _match_images(self, message: discord.Message):
        for att in message.attachments[:4]:
            is_img = (att.content_type or "").startswith("image/") or att.filename.lower().endswith(_IMG_EXT)
            if not is_img or att.size > ss.IMAGE_MAX_BYTES:
                continue
            try:
                async with _download_slots:
                    data = await asyncio.wait_for(att.read(), timeout=6)
            except Exception:
                continue
            hit = await asyncio.get_running_loop().run_in_executor(None, ss.match_image, data)
            if hit:
                return hit
        return None

    # ── what we do when something matches ────────────────────────────────
    async def _act(self, message: discord.Message, kind: str, matched: str, rule_id: Optional[int]) -> None:
        deleted = False
        try:
            await message.delete()
            deleted = True
        except discord.HTTPException as e:
            logger.warning("[scam-shield] couldn't delete in guild %s (%s)", message.guild.id, e)
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
