# path: discord_bot/cogs/_views_anime_feed.py

"""
Anime release feed, slice A: the shared Components V2 anime card, the live PREVIEW browser, and the setup wizard opened from
the Anime button on the join DM's More screen (no slash command). The wizard picks the channel, lets the admin browse
previews of what will be posted, sends one test post, and turns the feed on or off. Automatic posting comes in the next slice.
One card builder serves previews, test posts and (later) real posts, so every server sees the same design.
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional

import discord

from modules import anime_feed as af

logger = logging.getLogger(__name__)

ACCENT = discord.Color.from_rgb(88, 101, 242)
OOPS = "😿 Something went wrong. Try again in a moment."
TEXT_LIMIT = 3800


# ── the card (shared by previews, test posts and later real posts) ───────

def card_text(p: af.Preview, *, tag: Optional[str] = None) -> str:
    head = f"## {p.title}"
    meta = " · ".join(x for x in (p.fmt, ", ".join(p.genres)) if x)
    verb = "aired" if p.airing_at <= int(time.time()) else "airs"
    lines = [head, f"**{af.episode_label(p)}** {verb} <t:{p.airing_at}:F> (<t:{p.airing_at}:R>)"]
    if meta:
        lines.append(f"-# {meta}")
    if p.synopsis:
        lines += ["", p.synopsis]
    lines += ["", f"-# Source: {af.SOURCE_NAME} · times shown in your own timezone" + (f" · {tag}" if tag else "")]
    return "\n".join(lines)[:TEXT_LIMIT]


def build_card(p: af.Preview, *, tag: Optional[str] = None, extra_rows=()) -> discord.ui.Container:
    box = discord.ui.Container(accent_colour=ACCENT)
    text = card_text(p, tag=tag)
    if p.cover:
        section = discord.ui.Section(accessory=discord.ui.Thumbnail(p.cover))
        section.add_item(text)
        box.add_item(section)
    else:
        box.add_item(discord.ui.TextDisplay(text))
    box.add_item(discord.ui.ActionRow(discord.ui.Button(label="Details on AniList", style=discord.ButtonStyle.link, url=p.url)))
    for row in extra_rows:
        box.add_item(row)
    return box


def post_view(p: af.Preview, *, tag: Optional[str] = None) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(build_card(p, tag=tag))
    return view


def _btn(label, cb, style=discord.ButtonStyle.secondary, emoji=None, disabled=False) -> discord.ui.Button:
    b = discord.ui.Button(label=label, style=style, emoji=emoji, disabled=disabled)
    b.callback = cb
    return b


def _info_box(text: str, rows=()) -> discord.ui.Container:
    box = discord.ui.Container(accent_colour=ACCENT)
    box.add_item(discord.ui.TextDisplay(text[:TEXT_LIMIT]))
    for r in rows:
        box.add_item(r)
    return box


def setup_text(cfg: af.FeedConfig, note: Optional[str] = None) -> str:
    parts = [note, ""] if note else []
    parts += ["## 🎌 Anime releases",
              "Pick a channel, preview what I'll post, and switch the feed on. Titles come from AniList with their real airing "
              "times, shown in each reader's own timezone. Adult titles are never shown.", "",
              f"**Channel:** {f'<#{cfg.channel_id}>' if cfg.channel_id else 'not picked yet'}",
              f"**Feed:** {'ON' if cfg.enabled and cfg.channel_id else 'OFF'}",
              "-# New episodes are posted when they air. Only episodes that air after you turn the feed on are posted, never a backlog."]
    return "\n".join(parts)


# ── the wizard ───────────────────────────────────────────────────────────

class AnimeSetupView(discord.ui.LayoutView):
    def __init__(self, guild: discord.Guild, clone_id, owner_id: int, cfg: af.FeedConfig):
        super().__init__(timeout=900)
        self.guild, self.clone_id, self.owner_id, self.cfg = guild, clone_id, owner_id, cfg
        self.note: Optional[str] = None
        self.message: Optional[discord.Message] = None
        self.previews: List[af.Preview] = []
        self.index = 0
        self.screen = "setup"
        self._build()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            try:
                await interaction.response.send_message("🎌 This setup belongs to the person who opened it.", ephemeral=True)
            except discord.HTTPException:
                pass
            return False
        return True

    def _build(self) -> None:
        self.clear_items()
        if self.screen == "preview":
            self._build_preview()
            return
        sel = discord.ui.ChannelSelect(channel_types=[discord.ChannelType.text], placeholder="Pick the anime channel", min_values=1, max_values=1)
        sel.callback = self._on_channel
        ready = bool(self.cfg.channel_id)
        rows = [discord.ui.ActionRow(sel), discord.ui.ActionRow(
            _btn("Preview", self._on_preview, discord.ButtonStyle.primary, "👀"),
            _btn("Post a test", self._on_test, emoji="🧪", disabled=not ready),
            _btn("Turn off" if self.cfg.enabled else "Turn on", self._on_toggle,
                 discord.ButtonStyle.danger if self.cfg.enabled else discord.ButtonStyle.success, disabled=not ready),
            _btn("Close", self._on_close))]
        self.add_item(_info_box(setup_text(self.cfg, self.note), rows))

    def _build_preview(self) -> None:
        if not self.previews:
            self.add_item(_info_box(self.note or "Nothing is scheduled to air in the next 48 hours.",
                                    [discord.ui.ActionRow(_btn("Back", self._on_back, emoji="⬅️"))]))
            return
        p = self.previews[self.index]
        nav = discord.ui.ActionRow(
            _btn("Prev", self._on_prev, emoji="◀️", disabled=self.index <= 0),
            _btn(f"{self.index + 1}/{len(self.previews)}", self._on_back_noop, disabled=True),
            _btn("Next", self._on_next, emoji="▶️", disabled=self.index >= len(self.previews) - 1),
            _btn("Back", self._on_back, emoji="⬅️"))
        self.add_item(build_card(p, tag="Preview", extra_rows=[nav]))

    async def _push(self, interaction: discord.Interaction) -> None:
        self._build()
        if interaction.response.is_done():
            await interaction.edit_original_response(view=self)
        else:
            await interaction.response.edit_message(view=self)

    async def _on_channel(self, interaction: discord.Interaction):
        ch = interaction.data["values"][0]
        channel = self.guild.get_channel(int(ch))
        if channel is None or not channel.permissions_for(self.guild.me).send_messages:
            self.note = "🚫 I can't post in that channel. Give me **Send Messages** and **Embed Links** there, or pick another."
        else:
            self.cfg.channel_id = channel.id
            try:
                await af.save_config(self.cfg, interaction.user.id)
                self.note = f"✅ Channel set to {channel.mention}."
            except Exception:
                logger.exception("[anime-feed] couldn't save the channel")
                self.note = OOPS
        await self._push(interaction)

    async def _on_preview(self, interaction: discord.Interaction):
        await interaction.response.defer()
        items, ok = await af.airing_soon()
        items = af.upcoming(items, int(time.time()))
        self.previews, self.index, self.screen = items, 0, "preview"
        self.note = None if ok else "😿 I can't reach AniList right now, so there's nothing to preview. Try again in a few minutes."
        await self._push(interaction)

    async def _on_prev(self, interaction: discord.Interaction):
        self.index = max(0, self.index - 1)
        await self._push(interaction)

    async def _on_next(self, interaction: discord.Interaction):
        self.index = min(len(self.previews) - 1, self.index + 1)
        await self._push(interaction)

    async def _on_back_noop(self, interaction: discord.Interaction):
        await interaction.response.defer()

    async def _on_back(self, interaction: discord.Interaction):
        self.screen, self.note = "setup", None
        await self._push(interaction)

    async def _on_test(self, interaction: discord.Interaction):
        await interaction.response.defer()
        channel = self.guild.get_channel(self.cfg.channel_id) if self.cfg.channel_id else None
        items, ok = await af.airing_soon()
        items = af.upcoming(items, int(time.time())) or items
        if channel is None:
            self.note = "🚫 That channel is gone. Pick another."
        elif not items:
            self.note = "😿 There's nothing to post right now (AniList has no upcoming episodes, or I can't reach it)."
        else:
            try:
                await channel.send(view=post_view(items[0], tag="Test post"), allowed_mentions=discord.AllowedMentions.none())
                self.note = f"🧪 Test post sent to {channel.mention}."
            except (discord.Forbidden, discord.HTTPException):
                logger.warning("[anime-feed] test post failed in guild %s", self.guild.id, exc_info=True)
                self.note = "🚫 I couldn't post there. Check **Send Messages** and **Embed Links** for me in that channel."
        await self._push(interaction)

    async def _on_toggle(self, interaction: discord.Interaction):
        self.cfg.enabled = not self.cfg.enabled
        try:
            await af.save_config(self.cfg, interaction.user.id)
            self.note = "✅ Anime feed is ON." if self.cfg.enabled else "Anime feed is OFF."
        except Exception:
            logger.exception("[anime-feed] couldn't save the switch")
            self.cfg.enabled = not self.cfg.enabled
            self.note = OOPS
        await self._push(interaction)

    async def _on_close(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(view=_final_view(setup_text(self.cfg)))

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(view=_final_view(setup_text(self.cfg)))
            except discord.HTTPException:
                pass


def _final_view(text: str) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(_info_box(text))
    return view


async def open_setup(bot, guild: discord.Guild, clone_id, owner_id: int, channel) -> discord.Message:
    """Posts the wizard in `channel` for `owner_id`. Raises discord.HTTPException if it can't post."""
    cfg = await af.get_config(guild.id, clone_id)
    view = AnimeSetupView(guild, clone_id, owner_id, cfg)
    msg = await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
    view.message = msg
    return msg
