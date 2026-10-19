# path: discord_bot/cogs/_views_cat_setup.py

"""
Cat setup wizard: opened from the Cat button on the join DM by someone with Manage Server.

It is posted in a SERVER channel (a DM can't show a server's channel list, same reason the other wizards are posted
in the server). Only the person who opened it can use it. The Premium limits are explained right in the wizard:
a free server picks 1 spawn channel and gets 120 to 140 cats a day, a Premium server up to 3 channels and 250 to 300
a day. Picking more channels than the plan allows keeps the first ones and says so.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Optional

import discord

from modules import cat_announce
from modules import cat_setup as cs

logger = logging.getLogger(__name__)

_send_now_at: dict = {}


def pick_channels(requested_ids, premium: bool):
    """(kept ids, note). Free servers keep the first channel only; Premium keeps up to 3."""
    tier = cs.tier_for(premium)
    kept = cs.clamp_channels(requested_ids, tier)
    if len(set(int(i) for i in requested_ids)) > len(kept):
        note = (f"Free servers get {cs.FREE.max_channels} spawn channel, so I kept <#{kept[0]}>. "
                f"⭐ Premium gets up to {cs.PREMIUM.max_channels}.") if not premium else \
               f"Up to {tier.max_channels} channels, so I kept the first {len(kept)}."
        return kept, note
    return kept, ""


def _range_text(tier, pace: int) -> str:
    p = cs.clamp_pace(pace)
    lo, hi = cs.pace_target(tier.daily_min, p), cs.pace_target(tier.daily_max, p)
    return f"{lo} to {hi}" if lo != hi else str(lo)


ANNOUNCE_LABELS = {"all": "all", "milestones": "milestones only", "off": "off"}
ANNOUNCE_HELP = {"all": "adoptions, rank-ups, sales and milestones",
                 "milestones": "big moments (rare badges, 7/30-day streaks, all daily quests done) and cards players choose to Share",
                 "off": "nothing: cat actions and Share buttons stay private"}


def pace_label(tier, pace: int) -> str:
    return "Full amount" if pace == 100 else f"{pace}% of the full amount"


def _hour_ts(hour: int) -> int:
    """Unix time of the next top-of-hour `hour` (UTC), so Discord can show it in each viewer's own time zone."""
    now = datetime.now(timezone.utc)
    return int(now.replace(hour=hour % 24, minute=0, second=0, microsecond=0).timestamp())


def hours_text(start: int, end: int) -> str:
    if cs.window_hours(start, end) == 24:
        return "all day"
    return (f"<t:{_hour_ts(start)}:t> to <t:{_hour_ts(end)}:t> your time "
            f"({start:02d}:00 to {end % 24:02d}:00 UTC)")


PINK = discord.Color.from_rgb(255, 182, 193)
FOOTER = "Only sharp, high-resolution cats are posted."


def render_text(cfg: cs.CatConfig, premium: bool, notice: str = "", problems: str = "") -> str:
    """The wizard's text (it used to be an embed description). Kept as a plain string so tests can assert wording."""
    tier = cs.tier_for(premium)
    chans = " ".join(f"<#{c}>" for c in cfg.channel_ids) or "none yet, pick below"
    lines = ["## 🐱 Cat setup"]
    if notice:
        lines += [f"⚠️ {notice}", ""]
    if problems:
        lines += [problems, ""]
    lines += [
        f"**Status:** {'🟢 ON' if cfg.enabled else '🔴 OFF'}",
        f"**Spawn channel{'s' if tier.max_channels > 1 else ''}:** {chans}",
        f"**Cats a day:** about {_range_text(tier, cfg.pace_percent)}, spread across {'the day' if cs.window_hours(cfg.active_start, cfg.active_end) == 24 else 'your spawn hours'}",
        f"**Spawn hours:** {hours_text(cfg.active_start, cfg.active_end)}",
        f"**Public posts:** {ANNOUNCE_HELP[cs.clamp_announce(cfg.announce_mode)]}",
        "",
        ("⭐ **Premium server:** up to 3 spawn channels, 250 to 300 cats a day (shared between your channels)." if premium else
         "**Free server:** 1 spawn channel, 120 to 140 cats a day.\n"
         "⭐ **Premium:** up to 3 channels and 250 to 300 cats a day. Upgrade with `/premium` in your server."),
    ]
    return "\n".join(lines)


def _final_view(text: str) -> discord.ui.LayoutView:
    """The wizard with no controls (after Close or a timeout). A V2 message can't be left empty, so it keeps its text."""
    view = discord.ui.LayoutView(timeout=None)
    box = discord.ui.Container(accent_colour=PINK)
    box.add_item(discord.ui.TextDisplay(text))
    box.add_item(discord.ui.TextDisplay(f"-# {FOOTER}"))
    view.add_item(box)
    return view


class CatSetupView(discord.ui.LayoutView):
    def __init__(self, bot, guild: discord.Guild, clone_id, owner_id: int, cfg: cs.CatConfig, premium: bool):
        super().__init__(timeout=900)
        self.bot, self.guild, self.clone_id, self.owner_id = bot, guild, clone_id, owner_id
        self.cfg, self.premium = cfg, premium
        self.notice = ""
        self.message: Optional[discord.Message] = None

        self.channels = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text], placeholder="Pick the channel where cats appear",
            min_values=1, max_values=cs.PREMIUM.max_channels)
        self.channels.callback = self._on_channels

        self.pace = discord.ui.Select(placeholder="Want fewer cats? Pick how many", options=self._pace_options())
        self.pace.callback = self._on_pace

        self.start_hour = discord.ui.Select(placeholder="Spawn timing: cats start at (UTC)", options=self._hour_options(range(0, 24), self.cfg.active_start))
        self.start_hour.callback = self._on_start
        self.end_hour = discord.ui.Select(placeholder="Spawn timing: cats stop at (UTC)", options=self._hour_options(range(1, 25), self.cfg.active_end))
        self.end_hour.callback = self._on_end

        self.toggle = discord.ui.Button(label="Turn on", style=discord.ButtonStyle.success)
        self.toggle.callback = self._on_toggle
        self.send_now = discord.ui.Button(label="Send a cat now", emoji="🐱", style=discord.ButtonStyle.primary)
        self.send_now.callback = self._on_send_now
        self.check = discord.ui.Button(label="Check permissions", emoji="🔍", style=discord.ButtonStyle.secondary)
        self.check.callback = self._on_check
        self.announce = discord.ui.Button(style=discord.ButtonStyle.secondary)
        self.announce.callback = self._on_announce
        close = discord.ui.Button(label="Close", style=discord.ButtonStyle.secondary)
        close.callback = self._on_close
        self._sync()

        self.text = discord.ui.TextDisplay(self._text())
        box = discord.ui.Container(accent_colour=PINK)
        box.add_item(self.text)
        box.add_item(discord.ui.Separator())
        for item in (self.channels, self.pace, self.start_hour, self.end_hour):
            box.add_item(discord.ui.ActionRow(item))
        box.add_item(discord.ui.ActionRow(self.toggle, self.send_now, self.check, self.announce, close))
        box.add_item(discord.ui.TextDisplay(f"-# {FOOTER}"))
        self.add_item(box)

    def _text(self, with_problems: bool = True) -> str:
        return render_text(self.cfg, self.premium, self.notice, self._problems() if with_problems else "")

    def _refresh(self) -> None:
        self._sync()
        self.text.content = self._text()

    def _pace_options(self):
        tier = cs.tier_for(self.premium)
        return [discord.SelectOption(label=f"{pace_label(tier, p)} (about {_range_text(tier, p)} a day)", value=str(p),
                                     default=(p == cs.clamp_pace(self.cfg.pace_percent))) for p in cs.PACES]

    @staticmethod
    def _hour_options(hours, current):
        return [discord.SelectOption(label=(f"{h:02d}:00 UTC" if h < 24 else "24:00 UTC (midnight)"), value=str(h),
                                     default=(h == current)) for h in hours]

    def _problems(self) -> str:
        """Live check of the spawn channels (permissions can change after the server picked them). Only shown while
        the game is ON, because that is when 'on but not working' matters; never raises."""
        if not self.cfg.enabled or not self.cfg.channel_ids:
            return ""
        try:
            from modules import cat_perms
            return cat_perms.panel_text(cat_perms.diagnose(self.guild, self.cfg.channel_ids, discord.TextChannel))
        except Exception:
            logger.debug("[cat] permission check failed", exc_info=True)
            return ""

    def _sync(self) -> None:
        for opt in self.start_hour.options:
            opt.default = int(opt.value) == self.cfg.active_start
        for opt in self.end_hour.options:
            opt.default = int(opt.value) == self.cfg.active_end
        for opt in self.pace.options:
            opt.default = int(opt.value) == cs.clamp_pace(self.cfg.pace_percent)
        self.toggle.label = "Turn off" if self.cfg.enabled else "Turn on"
        self.toggle.style = discord.ButtonStyle.danger if self.cfg.enabled else discord.ButtonStyle.success
        self.toggle.disabled = not self.cfg.channel_ids
        self.send_now.disabled = not self.cfg.channel_ids
        self.check.disabled = not self.cfg.channel_ids
        self.announce.label = f"Public posts: {ANNOUNCE_LABELS[cs.clamp_announce(self.cfg.announce_mode)]}"
        self.announce.emoji = "📣"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only the person who opened this can change it.", ephemeral=True)
            return False
        from discord_bot.cogs._views_shared import user_can_manage_guild
        if not await user_can_manage_guild(self.guild, interaction.user.id):
            await interaction.response.send_message("You need the **Manage Server** permission.", ephemeral=True)
            return False
        return True

    async def _apply(self, interaction: discord.Interaction, **changes) -> None:
        new = replace(self.cfg, **changes)
        new = cs.after_change(self.cfg, new)
        try:
            await cs.save_config(new, updated_by=interaction.user.id)
        except Exception:
            logger.exception("[cat] couldn't save the setup for guild %s", self.guild.id)
            self.notice = "I couldn't save that (database problem). Nothing changed."
        else:
            self.cfg = new
        self._refresh()
        await interaction.response.edit_message(view=self)
        self.notice = ""

    async def _on_check(self, interaction: discord.Interaction):
        from modules import cat_perms
        found = cat_perms.diagnose(self.guild, self.cfg.channel_ids, discord.TextChannel)
        await interaction.response.send_message(
            cat_perms.panel_text(found).replace("The Cat game is ON but I can't post", "I can't post") if found else
            "✅ I can post in your spawn channel" + ("s" if len(self.cfg.channel_ids) > 1 else "") + ".", ephemeral=True)

    async def _on_channels(self, interaction: discord.Interaction):
        usable, bad = [], []
        for ch in self.channels.values:
            real = self.guild.get_channel(ch.id)
            perms = real.permissions_for(self.guild.me) if real is not None else None
            if perms and perms.view_channel and perms.send_messages and perms.embed_links:
                usable.append(ch.id)
            else:
                bad.append(ch.id)
        kept, note = pick_channels(usable, self.premium)
        msgs = []
        if bad:
            msgs.append("I can't post (Send Messages + Embed Links) in " + ", ".join(f"<#{i}>" for i in bad) + ".")
        if note:
            msgs.append(note)
        self.notice = " ".join(msgs)
        if not kept:
            self._sync()
            self.text.content = render_text(self.cfg, self.premium, self.notice)
            await interaction.response.edit_message(view=self)
            self.notice = ""
            return
        await self._apply(interaction, channel_ids=kept)

    async def _on_pace(self, interaction: discord.Interaction):
        await self._apply(interaction, pace_percent=cs.clamp_pace(self.pace.values[0]))

    async def _on_start(self, interaction: discord.Interaction):
        await self._apply(interaction, active_start=cs.clamp_hour(self.start_hour.values[0], 0) % 24)

    async def _on_end(self, interaction: discord.Interaction):
        await self._apply(interaction, active_end=cs.clamp_hour(self.end_hour.values[0], 24))

    async def _on_announce(self, interaction: discord.Interaction):
        modes = cs.ANNOUNCE_MODES
        nxt = modes[(modes.index(cs.clamp_announce(self.cfg.announce_mode)) + 1) % len(modes)]
        await self._apply(interaction, announce_mode=nxt)
        if self.cfg.announce_mode == nxt:
            cat_announce.remember_mode(self.guild.id, self.clone_id, nxt)

    async def _on_toggle(self, interaction: discord.Interaction):
        await self._apply(interaction, enabled=not self.cfg.enabled)

    async def _on_send_now(self, interaction: discord.Interaction):
        left = SEND_NOW_COOLDOWN - (time.monotonic() - _send_now_at.get(self.guild.id, -1e9))
        if left > 0:
            await interaction.response.send_message(f"😺 Easy there, another cat in {left:.0f}s.", ephemeral=True)
            return
        _send_now_at[self.guild.id] = time.monotonic()
        await interaction.response.defer(ephemeral=True)
        from discord_bot.cogs.cat import post_cat
        ok = await post_cat(self.bot, self.guild, self.cfg.channel_ids[0])
        await interaction.followup.send(
            f"🐱 Sent one to <#{self.cfg.channel_ids[0]}>." if ok else
            "😿 I couldn't post a cat there (check I can Send Messages + Embed Links, or try again in a moment).",
            ephemeral=True)

    async def _on_close(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(view=_final_view(render_text(self.cfg, self.premium)))

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(view=_final_view(render_text(self.cfg, self.premium)))
            except discord.HTTPException:
                pass


SEND_NOW_COOLDOWN = 20.0


async def open_setup(bot, guild: discord.Guild, clone_id, owner_id: int, channel) -> discord.Message:
    """Posts the wizard in `channel` for `owner_id`. Raises discord.HTTPException if it can't post."""
    cfg = await cs.get_config(guild.id, clone_id)
    premium = await cs.is_premium(guild.id, clone_id)
    view = CatSetupView(bot, guild, clone_id, owner_id, cfg, premium)
    msg = await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
    view.message = msg
    return msg
