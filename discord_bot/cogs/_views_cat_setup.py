# path: discord_bot/cogs/_views_cat_setup.py

"""
Cat setup wizard: opened from the Cat button on the join DM by someone with Manage Server.

It is posted in a SERVER channel (a DM can't show a server's channel list, same reason the other wizards are posted
in the server). Only the person who opened it can use it. The Premium limits are explained right in the wizard:
a free server picks 1 spawn channel and 6 / 12 / 24 hour intervals, a Premium server up to 3 channels and as often
as every 15 minutes. Picking a Premium-only option on a free server changes nothing and says so.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from typing import Optional

import discord

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


def pick_interval(minutes: int, premium: bool, current: int):
    """(minutes to use, note). A Premium-only speed on a free server keeps the current setting."""
    tier = cs.tier_for(premium)
    if minutes in tier.intervals:
        return minutes, ""
    return cs.clamp_interval(current, tier), (
        f"Every {cs.interval_label(minutes)} is a ⭐ Premium speed. Free servers can choose every "
        + ", ".join(cs.interval_label(m) for m in cs.FREE.intervals) + ". Premium: `/premium` in your server.")


def render_embed(cfg: cs.CatConfig, premium: bool, notice: str = "") -> discord.Embed:
    tier = cs.tier_for(premium)
    chans = " ".join(f"<#{c}>" for c in cfg.channel_ids) or "none yet, pick below"
    lines = []
    if notice:
        lines += [f"⚠️ {notice}", ""]
    lines += [
        f"**Status:** {'🟢 ON' if cfg.enabled else '🔴 OFF'}",
        f"**Spawn channel{'s' if tier.max_channels > 1 else ''}:** {chans}",
        f"**A cat appears:** every {cs.interval_label(cs.clamp_interval(cfg.interval_minutes, tier))}",
        "",
        ("⭐ **Premium server:** up to 3 spawn channels, a cat as often as every 15 minutes." if premium else
         "**Free server:** 1 spawn channel, a cat every 6, 12 or 24 hours.\n"
         "⭐ **Premium:** up to 3 channels and as often as every 15 minutes. Upgrade with `/premium` in your server."),
    ]
    e = discord.Embed(title="🐱 Cat setup", description="\n".join(lines), color=discord.Color.from_rgb(255, 182, 193))
    e.set_footer(text="Only sharp, high-resolution cats are posted.")
    return e


class CatSetupView(discord.ui.View):
    def __init__(self, bot, guild: discord.Guild, clone_id, owner_id: int, cfg: cs.CatConfig, premium: bool):
        super().__init__(timeout=900)
        self.bot, self.guild, self.clone_id, self.owner_id = bot, guild, clone_id, owner_id
        self.cfg, self.premium = cfg, premium
        self.notice = ""
        self.message: Optional[discord.Message] = None

        self.channels = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text], placeholder="Pick the channel where cats appear",
            min_values=1, max_values=cs.PREMIUM.max_channels, row=0)
        self.channels.callback = self._on_channels
        self.add_item(self.channels)

        self.interval = discord.ui.Select(placeholder="How often should a cat appear?", row=1, options=self._options())
        self.interval.callback = self._on_interval
        self.add_item(self.interval)

        self.toggle = discord.ui.Button(label="Turn on", style=discord.ButtonStyle.success, row=2)
        self.toggle.callback = self._on_toggle
        self.add_item(self.toggle)
        self.send_now = discord.ui.Button(label="Send a cat now", emoji="🐱", style=discord.ButtonStyle.primary, row=2)
        self.send_now.callback = self._on_send_now
        self.add_item(self.send_now)
        close = discord.ui.Button(label="Close", style=discord.ButtonStyle.secondary, row=2)
        close.callback = self._on_close
        self.add_item(close)
        self._sync()

    def _options(self):
        return [discord.SelectOption(
                    label=f"Every {cs.interval_label(m)}", value=str(m),
                    description=None if (self.premium or m in cs.FREE.intervals) else "⭐ Premium only",
                    default=(m == self.cfg.interval_minutes)) for m in cs.ALL_INTERVALS]

    def _sync(self) -> None:
        for opt in self.interval.options:
            opt.default = int(opt.value) == cs.clamp_interval(self.cfg.interval_minutes, cs.tier_for(self.premium))
        self.toggle.label = "Turn off" if self.cfg.enabled else "Turn on"
        self.toggle.style = discord.ButtonStyle.danger if self.cfg.enabled else discord.ButtonStyle.success
        self.toggle.disabled = not self.cfg.channel_ids
        self.send_now.disabled = not self.cfg.channel_ids

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
        self._sync()
        await interaction.response.edit_message(embed=render_embed(self.cfg, self.premium, self.notice), view=self)
        self.notice = ""

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
            await interaction.response.edit_message(embed=render_embed(self.cfg, self.premium, self.notice), view=self)
            self.notice = ""
            return
        await self._apply(interaction, channel_ids=kept)

    async def _on_interval(self, interaction: discord.Interaction):
        minutes, note = pick_interval(int(self.interval.values[0]), self.premium, self.cfg.interval_minutes)
        self.notice = note
        await self._apply(interaction, interval_minutes=minutes)

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
        await interaction.response.edit_message(embed=render_embed(self.cfg, self.premium), view=None)

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


SEND_NOW_COOLDOWN = 20.0


async def open_setup(bot, guild: discord.Guild, clone_id, owner_id: int, channel) -> discord.Message:
    """Posts the wizard in `channel` for `owner_id`. Raises discord.HTTPException if it can't post."""
    cfg = await cs.get_config(guild.id, clone_id)
    premium = await cs.is_premium(guild.id, clone_id)
    view = CatSetupView(bot, guild, clone_id, owner_id, cfg, premium)
    msg = await channel.send(embed=render_embed(cfg, premium), view=view,
                             allowed_mentions=discord.AllowedMentions.none())
    view.message = msg
    return msg
