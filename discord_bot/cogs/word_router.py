# path: discord_bot/cogs/word_router.py

"""
One-word router: "@Bot raid", "!raid" or "raid" in a DM -> a button-only Components V2 panel. Pressing a button opens that
command's own panel with the press as its interaction, so every feature works with no per-feature code.

Safety: permissions are re-checked on every press (modules/word_router.allowed + the command's own checks); only the person who
typed the word can press; AI-only commands (harness, anti-nuke setup) are not on the slash tree so they never appear; no
mentions are ever sent. This cog has no slash commands.
"""

import logging
import time
from typing import Optional

import discord
from discord.ext import commands

from modules import word_router as wr

logger = logging.getLogger(__name__)
NO_PINGS = discord.AllowedMentions.none()
COOLDOWN = 3.0
TIMEOUT = 180


async def _note(i: discord.Interaction, text: str):
    try:
        if i.response.is_done():
            await i.followup.send(text, ephemeral=True, allowed_mentions=NO_PINGS)
        else:
            await i.response.send_message(text, ephemeral=True, allowed_mentions=NO_PINGS)
    except discord.HTTPException:
        logger.exception("[word_router] could not send a notice")


class RouterView(discord.ui.LayoutView):
    def __init__(self, bot, owner_id: int, word: str, user_perms: Optional[int], in_guild: bool):
        super().__init__(timeout=TIMEOUT)
        self.bot, self.owner_id, self.word, self.in_guild = bot, owner_id, word, in_guild
        self.message: Optional[discord.Message] = None
        self.by_name = {c.qualified_name: c for c in self._commands()}
        self.entries = wr.entries_from(self.by_name.values())
        self.render(word, user_perms)

    def _commands(self):
        return [c for c in self.bot.tree.walk_commands() if not isinstance(c, discord.app_commands.Group)]

    def render(self, word: str, user_perms: Optional[int]):
        self.clear_items()
        found = wr.rank(word, self.entries, user_perms, self.in_guild)
        box = discord.ui.Container(accent_colour=discord.Color.blurple())
        if found:
            for i in range(0, len(found), 5):
                row = discord.ui.ActionRow()
                for e in found[i:i + 5]:
                    b = discord.ui.Button(label=e.qname[:80], style=discord.ButtonStyle.secondary)
                    b.callback = self._opener(e.qname)
                    row.add_item(b)
                box.add_item(row)
        else:
            names = wr.top_level_names(self.entries, user_perms, self.in_guild)
            if not names:
                box.add_item(discord.ui.TextDisplay("—"))
            else:
                sel = discord.ui.Select(placeholder="Pick one", options=[discord.SelectOption(label=n[:100], value=n[:100]) for n in names])
                sel.callback = self._picked(sel)
                box.add_item(discord.ui.ActionRow(sel))
        self.add_item(box)

    def _picked(self, sel):
        async def cb(i: discord.Interaction):
            self.render(sel.values[0].lower(), getattr(i.permissions, "value", None) if i.guild else None)
            await i.response.edit_message(view=self)
        return cb

    def _opener(self, qname: str):
        async def cb(i: discord.Interaction):
            await self._open(i, qname)
        return cb

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if i.user.id != self.owner_id:
            await _note(i, "Not yours.")
            return False
        return True

    async def _open(self, i: discord.Interaction, qname: str):
        cmd = self.by_name.get(qname)
        entry = next((e for e in self.entries if e.qname == qname), None)
        perms = getattr(i.permissions, "value", None) if i.guild else None
        if cmd is None or entry is None or not wr.allowed(entry, perms, bool(i.guild)):
            return await _note(i, "Not available here.")
        try:
            if not await cmd._check_can_run(i):
                return await _note(i, "Not available to you.")
        except discord.app_commands.AppCommandError:
            return await _note(i, "Not available to you.")
        try:
            binding = getattr(cmd, "binding", None)
            await (cmd.callback(binding, i) if binding is not None else cmd.callback(i))
        except Exception:
            logger.exception("[word_router] /%s failed", qname)
            await _note(i, "Something went wrong.")

    async def on_timeout(self):
        if self.message is not None:
            try:
                await self.message.delete()
            except discord.HTTPException:
                pass


class WordRouterCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._last = {}

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        try:
            if message.author.bot:
                return
            in_dm = message.guild is None
            word = wr.parse_trigger(message.content, getattr(self.bot.user, "id", None), in_dm,
                                    lambda w: self.bot.get_command(w) is not None)
            if word is None:
                return
            now = time.monotonic()
            if now - self._last.get(message.author.id, 0.0) < COOLDOWN:
                return
            self._last[message.author.id] = now
            if len(self._last) > 5000:
                self._last = {k: v for k, v in self._last.items() if now - v < COOLDOWN}
            perms = None if in_dm else message.channel.permissions_for(message.author).value
            view = RouterView(self.bot, message.author.id, word, perms, not in_dm)
            view.message = await message.reply(view=view, mention_author=False, allowed_mentions=NO_PINGS)
        except Exception:
            logger.exception("[word_router] on_message failed")


async def setup(bot: commands.Bot):
    await bot.add_cog(WordRouterCog(bot))
