# path: discord_bot/cogs/counting.py

"""
Counting game.

NO slash command: it is set up from the join DM's "More features" screen (Counting) and everything runs from one
persistent Components V2 panel. Members just count in the chosen channel: one number each, never the same person
twice in a row. ✅ on a right number, ❌ and a fresh start from 1 on a wrong one.

Free: the game, best score, stats and a top 5. 💎 Premium (applied only while the server has Premium, settings are
kept if it lapses): maths counting, saves, milestone shout-outs and a top 15.

Cost: nothing is loaded until a server turns counting on (one cheap query at startup, none if the table doesn't exist).
Each message does one dict lookup; the database is touched only for messages that are actually a count attempt.
Rules live in modules/counting.py, storage in modules/counting_store.py.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, Optional

import discord
from discord.ext import commands

from discord_bot.cogs.birthdays import _Fixed, _admin, _clone, _is_premium, _premium_gate
from modules import counting as cnt
from modules import counting_store as store

logger = logging.getLogger(__name__)

PREMIUM_TTL = 120.0             # seconds a Premium lookup is reused by the message listener
_LIVE = ("current", "high_score", "last_user_id", "saves", "total_correct", "total_ruined")


def _cog(interaction: discord.Interaction):
    return interaction.client.get_cog("CountingCog")


def _can_play(channel, guild: discord.Guild) -> bool:
    me = guild.me
    if not channel or not me:
        return False
    p = channel.permissions_for(me)
    return bool(p.view_channel and p.send_messages and p.add_reactions and p.read_message_history)


def _on_badge(cfg: dict, ch) -> bool:
    return bool(cfg.get("enabled") and ch)


def _flag(on: bool) -> str:
    return "✅ on" if on else "off"


def _config_text(guild: discord.Guild, cfg: dict, premium: bool) -> str:
    ch = guild.get_channel(cfg["channel_id"]) if cfg.get("channel_id") else None
    cur = int(cfg.get("current") or 0)
    lines = [
        "## 🔢 Counting setup",
        "Members count up together in one channel, one number each, never the same person twice in a row. "
        "A wrong number starts the run again from 1.",
        "",
        f"**Status:** {'✅ On' if _on_badge(cfg, ch) else '⏸️ Off (pick a channel, then turn on)'}",
        f"**Channel:** {ch.mention if ch else 'not set'}",
        f"**Current:** {cur} · **Best:** {int(cfg.get('high_score') or 0)}",
        "",
    ]
    if premium:
        ms = int(cfg.get("milestone_every") or 0)
        lines += ["**💎 Premium perks (active)**",
                  f"🧮 Maths counting: {_flag(bool(cfg.get('math_on')))}",
                  f"🛟 Saves: {_flag(bool(cfg.get('saves_on')))}",
                  f"🎉 Milestone shout-outs: {f'every {ms}' if ms else 'off'}"]
    else:
        lines += ["**💎 Premium perks**", cnt.PREMIUM_PERKS + "\nTap a 💎 button to unlock them."]
    return "\n".join(lines)


def build_setup_view(guild: discord.Guild, cfg: dict, premium: bool, note: Optional[str] = None) -> discord.ui.LayoutView:
    ch = guild.get_channel(cfg["channel_id"]) if cfg.get("channel_id") else None
    view = discord.ui.LayoutView(timeout=None)
    box = discord.ui.Container(accent_colour=discord.Color.green() if _on_badge(cfg, ch) else discord.Color.dark_grey())
    box.add_item(discord.ui.TextDisplay(_config_text(guild, cfg, premium) + (f"\n-# {note}" if note else "")))
    box.add_item(discord.ui.Separator())
    box.add_item(discord.ui.ActionRow(PickChannelSelect()))
    box.add_item(discord.ui.ActionRow(ToggleButton(bool(cfg.get("enabled"))), StatsButton(), ResetButton()))
    box.add_item(discord.ui.ActionRow(MathButton(premium), SavesButton(premium), MilestoneButton(premium)))
    view.add_item(box)
    return view


def _started_view() -> discord.ui.LayoutView:
    """Posted in the counting channel when it is turned on."""
    view = discord.ui.LayoutView(timeout=None)
    box = discord.ui.Container(accent_colour=discord.Color.green())
    box.add_item(discord.ui.TextDisplay(
        "## 🔢 Counting is on!\nStart with **1**, then take turns: one number each, never twice in a row. "
        "Wrong number? We start again from 1."))
    box.add_item(discord.ui.ActionRow(StatsButton()))
    view.add_item(box)
    return view


async def _state(interaction: discord.Interaction):
    cfg = await store.get_config(interaction.guild.id, _clone(interaction))
    return cfg, await _is_premium(interaction.guild.id, _clone(interaction))


async def _save(interaction: discord.Interaction, **fields):
    """Persist settings, refresh the listener's cache, then re-render the panel from the fresh row."""
    guild, clone_id = interaction.guild, _clone(interaction)
    cfg = await store.set_config(guild.id, clone_id, **fields)
    cog = _cog(interaction)
    if cog is not None:
        cfg = cog.apply_config(cfg)
    premium = await _is_premium(guild.id, clone_id)
    await interaction.response.edit_message(view=build_setup_view(guild, cfg, premium))


# ── modal ───────────────────────────────────────────────────────────────

class _MilestoneModal(discord.ui.Modal, title="💎 Milestone shout-outs"):
    every = discord.ui.TextInput(label=f"Celebrate every N counts ({cnt.MILESTONE_MIN}-{cnt.MILESTONE_MAX}, 0 = off)",
                                 max_length=5, placeholder="100")

    def __init__(self, cfg: dict):
        super().__init__()
        self.every.default = str(int(cfg.get("milestone_every") or 0))

    async def on_submit(self, interaction: discord.Interaction):
        if not await _admin(interaction):
            return
        n = cnt.valid_milestone(self.every.value)
        if n is None:
            return await interaction.response.send_message(
                f"Use 0 to turn it off, or a whole number from {cnt.MILESTONE_MIN} to {cnt.MILESTONE_MAX}.", ephemeral=True)
        await _save(interaction, milestone_every=n)


# ── persistent items (fixed custom_ids, no state of their own) ──────────

class PickChannelSelect(_Fixed, discord.ui.DynamicItem[discord.ui.ChannelSelect], template=r"^count:channel$"):
    def __init__(self):
        super().__init__(discord.ui.ChannelSelect(
            custom_id="count:channel", channel_types=[discord.ChannelType.text], placeholder="Channel to count in"))

    async def callback(self, interaction: discord.Interaction):
        if not await _admin(interaction):
            return
        channel = interaction.guild.get_channel(self.item.values[0].id)
        if not _can_play(channel, interaction.guild):
            return await interaction.response.send_message(
                f"I need **View Channel, Send Messages, Add Reactions** and **Read Message History** in "
                f"{channel.mention if channel else 'that channel'}. Fix its permissions or pick another one.", ephemeral=True)
        await _save(interaction, channel_id=channel.id)


class ToggleButton(_Fixed, discord.ui.DynamicItem[discord.ui.Button], template=r"^count:toggle$"):
    def __init__(self, enabled: bool = False):
        super().__init__(discord.ui.Button(
            label="Turn off" if enabled else "Turn on",
            style=discord.ButtonStyle.danger if enabled else discord.ButtonStyle.success, custom_id="count:toggle"))

    async def callback(self, interaction: discord.Interaction):
        if not await _admin(interaction):
            return
        cfg, _ = await _state(interaction)             # the live value, not whatever the label said
        if not cfg.get("enabled") and not cfg.get("channel_id"):
            return await interaction.response.send_message("Pick a channel first.", ephemeral=True)
        turning_on = not cfg.get("enabled")
        if turning_on:
            ch = interaction.guild.get_channel(cfg["channel_id"])
            if not _can_play(ch, interaction.guild):
                return await interaction.response.send_message(
                    "I can't play in that channel any more. Pick a channel I have **Send Messages, Add Reactions** and "
                    "**Read Message History** in.", ephemeral=True)
        await _save(interaction, enabled=turning_on)
        if turning_on:
            try:
                await ch.send(view=_started_view(), allowed_mentions=discord.AllowedMentions.none())
            except (discord.Forbidden, discord.HTTPException):
                logger.info("[counting] couldn't post the start message in guild %s", interaction.guild.id)


class StatsButton(_Fixed, discord.ui.DynamicItem[discord.ui.Button], template=r"^count:stats$"):
    """Open to everyone (read-only): current run, best, and the top counters."""

    def __init__(self):
        super().__init__(discord.ui.Button(label="Stats", emoji="📊", style=discord.ButtonStyle.secondary,
                                           custom_id="count:stats"))

    async def callback(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return await interaction.response.send_message("Use this inside a server.", ephemeral=True)
        cog = _cog(interaction)
        now = time.monotonic()
        cd = cog._stats_cooldown if cog is not None else {}
        if now < cd.get(interaction.user.id, 0):
            return await interaction.response.send_message("Slow down, try again in a few seconds.", ephemeral=True)
        if len(cd) > 500:
            cd.clear()
        cd[interaction.user.id] = now + 5
        await interaction.response.defer(ephemeral=True)
        cfg, premium = await _state(interaction)
        rows = await store.top_users(interaction.guild.id, _clone(interaction), cnt.PREMIUM_TOP if premium else cnt.FREE_TOP)
        view = discord.ui.LayoutView(timeout=None)
        box = discord.ui.Container(accent_colour=discord.Color.blurple())
        box.add_item(discord.ui.TextDisplay(cnt.stats_text(cfg, rows, premium)))
        view.add_item(box)
        await interaction.followup.send(view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


class ResetButton(_Fixed, discord.ui.DynamicItem[discord.ui.Button], template=r"^count:reset$"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="Reset count", emoji="🔄", style=discord.ButtonStyle.secondary,
                                           custom_id="count:reset"))

    async def callback(self, interaction: discord.Interaction):
        if not await _admin(interaction):
            return
        await store.reset_run(interaction.guild.id, _clone(interaction))
        cfg = await store.get_config(interaction.guild.id, _clone(interaction))
        cog = _cog(interaction)
        if cog is not None:
            cfg = cog.apply_config(cfg)
        premium = await _is_premium(interaction.guild.id, _clone(interaction))
        await interaction.response.edit_message(
            view=build_setup_view(interaction.guild, cfg, premium, note="Count reset to 0. Best score and stats are kept."))


class _PremiumToggle:
    """Mixin (not a DynamicItem itself: every DynamicItem subclass needs its own template). Flips one Premium setting."""
    field = ""

    async def callback(self, interaction: discord.Interaction):
        if not await _admin(interaction):
            return
        if not await _premium_gate(interaction, interaction.guild, _clone(interaction)):
            return
        cfg, _ = await _state(interaction)
        await _save(interaction, **{self.field: not cfg.get(self.field)})


class MathButton(_Fixed, _PremiumToggle, discord.ui.DynamicItem[discord.ui.Button], template=r"^count:math$"):
    field = "math_on"

    def __init__(self, premium: bool = False):
        super().__init__(discord.ui.Button(
            label="Maths" + ("" if premium else " 💎"), emoji="🧮", style=discord.ButtonStyle.primary, custom_id="count:math"))


class SavesButton(_Fixed, _PremiumToggle, discord.ui.DynamicItem[discord.ui.Button], template=r"^count:saves$"):
    field = "saves_on"

    def __init__(self, premium: bool = False):
        super().__init__(discord.ui.Button(
            label="Saves" + ("" if premium else " 💎"), emoji="🛟", style=discord.ButtonStyle.primary, custom_id="count:saves"))


class MilestoneButton(_Fixed, discord.ui.DynamicItem[discord.ui.Button], template=r"^count:milestone$"):
    def __init__(self, premium: bool = False):
        super().__init__(discord.ui.Button(
            label="Milestones" + ("" if premium else " 💎"), emoji="🎉", style=discord.ButtonStyle.primary,
            custom_id="count:milestone"))

    async def callback(self, interaction: discord.Interaction):
        if not await _admin(interaction):
            return
        if not await _premium_gate(interaction, interaction.guild, _clone(interaction)):
            return
        cfg, _ = await _state(interaction)
        await interaction.response.send_modal(_MilestoneModal(cfg))


DYNAMIC_ITEMS = (PickChannelSelect, ToggleButton, StatsButton, ResetButton, MathButton, SavesButton, MilestoneButton)


# ── the cog ─────────────────────────────────────────────────────────────

class CountingCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.clone_id: Optional[int] = getattr(bot, "clone_id", None)
        self._cfg: Dict[int, dict] = {}                 # counting channel id -> live config (settings + the run)
        self._locks: Dict[int, asyncio.Lock] = {}
        self._premium_until: Dict[int, tuple] = {}      # guild id -> (monotonic expiry, is_premium)
        self._stats_cooldown: dict = {}

    async def cog_load(self):
        asyncio.create_task(self._startup_load())

    async def _startup_load(self):
        try:
            await self.bot.wait_until_ready()
            for cfg in await store.enabled_configs(self.clone_id):
                if cfg.get("channel_id"):
                    self._cfg[cfg["channel_id"]] = cfg
            if self._cfg:
                logger.info("[counting] %d counting channel(s) loaded", len(self._cfg))
        except Exception:
            logger.exception("[counting] startup load failed; counting stays off until a server re-saves its setup")

    def apply_config(self, cfg: dict) -> dict:
        """Take a saved config into the listener's cache. The live run already in memory wins over the row (it is
        written after every count, so the row can only be older). Returns the config the panel should show."""
        gid = cfg["guild_id"]
        live = None
        for ch_id, old in list(self._cfg.items()):
            if old["guild_id"] == gid:
                live = old
                self._cfg.pop(ch_id, None)
        if live is not None:
            cfg = {**cfg, **{k: live[k] for k in _LIVE if k in live}}
        if cfg.get("enabled") and cfg.get("channel_id"):
            self._cfg[cfg["channel_id"]] = cfg
        return cfg

    async def _premium(self, guild_id: int) -> bool:
        hit = self._premium_until.get(guild_id)
        now = time.monotonic()
        if hit and now < hit[0]:
            return hit[1]
        ok = await _is_premium(guild_id, self.clone_id)
        if len(self._premium_until) > 1000:
            self._premium_until.clear()
        self._premium_until[guild_id] = (now + PREMIUM_TTL, ok)
        return ok

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not self._cfg or message.guild is None or message.author.bot:
            return
        cfg = self._cfg.get(message.channel.id)
        if cfg is None or cfg["guild_id"] != message.guild.id:
            return
        try:
            premium = await self._premium(message.guild.id) if (
                cfg.get("math_on") or cfg.get("saves_on") or cfg.get("milestone_every")) else False
            number = cnt.parse_number(message.content, allow_math=bool(premium and cfg.get("math_on")))
            if number is None:
                return                                   # chat is never punished
            lock = self._locks.setdefault(message.channel.id, asyncio.Lock())
            async with lock:
                cfg = self._cfg.get(message.channel.id)
                if cfg is None:
                    return
                before = int(cfg.get("current") or 0)
                new, out = cnt.step(cfg, message.author.id, number, saves_on=bool(premium and cfg.get("saves_on")),
                                    milestone_every=int(cfg.get("milestone_every") or 0) if premium else 0)
                if out.kind == "hint":
                    cfg_after = cfg
                else:
                    cfg.update(new)
                    cfg_after = cfg
                    try:
                        await store.record(cfg["guild_id"], self.clone_id, new, message.author.id, correct=out.kind == "ok")
                    except Exception:
                        logger.exception("[counting] couldn't save the run in guild %s", cfg["guild_id"])
            await self._respond(message, cfg_after, out, before)
        except Exception:
            logger.exception("[counting] failed handling a message in guild %s", message.guild.id)

    async def _respond(self, message: discord.Message, cfg: dict, out: cnt.Outcome, before: int) -> None:
        name = discord.utils.escape_markdown(discord.utils.escape_mentions(message.author.display_name))
        none = discord.AllowedMentions.none()
        try:
            if out.kind == "ok":
                await message.add_reaction("🎉" if out.milestone else "✅")
                if out.milestone:
                    view = discord.ui.LayoutView(timeout=None)
                    box = discord.ui.Container(accent_colour=discord.Color.gold())
                    box.add_item(discord.ui.TextDisplay(
                        f"### 🎉 {out.number} reached!\nNice teamwork. **{name}** hit the milestone. "
                        f"Best so far: **{int(cfg.get('high_score') or 0)}**."))
                    view.add_item(box)
                    await message.channel.send(view=view, allowed_mentions=none)
                if out.earned_save:
                    await message.channel.send(f"🛟 A save was earned: **{out.saves_left}/{cnt.SAVES_MAX}** saves.",
                                               allowed_mentions=none)
                return
            await message.add_reaction("❌")
            text = cnt.outcome_text(name, out, before, int(cfg.get("high_score") or 0))
            if text:
                await message.channel.send(text, allowed_mentions=none)
        except (discord.Forbidden, discord.HTTPException):
            logger.info("[counting] couldn't react or post in guild %s", message.guild.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(CountingCog(bot))
