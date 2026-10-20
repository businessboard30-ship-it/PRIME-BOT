# path: discord_bot/cogs/_views_anime_feed.py

"""
Anime release feed, slice A: the shared Components V2 anime card, the live PREVIEW browser, and the setup wizard opened from
the Anime button on the join DM's More screen (no slash command). The wizard picks the channel, lets the admin browse
previews of what will be posted, sends one test post, and turns the feed on or off. Automatic posting comes in the next slice.
One card builder serves previews, test posts and (later) real posts, so every server sees the same design.
"""

from __future__ import annotations

import logging
import re
import time
from typing import List, Optional

import discord

from modules import anime_alerts as aa
from modules import anime_feed as af

logger = logging.getLogger(__name__)

ACCENT = discord.Color.from_rgb(88, 101, 242)
OOPS = "😿 Something went wrong. Try again in a moment."
TEXT_LIMIT = 3800


# ── the card (shared by previews, test posts and later real posts) ───────

def card_text(p: af.Preview, *, tag: Optional[str] = None) -> str:
    head = f"## {p.title}"
    meta = " · ".join(x for x in (af.origin_label(p), p.fmt, ", ".join(p.genres)) if x)
    verb = "aired" if p.airing_at <= int(time.time()) else "airs"
    lines = [head, f"**{af.episode_label(p)}** {verb} <t:{p.airing_at}:F> (<t:{p.airing_at}:R>)"]
    if meta:
        lines.append(f"-# {meta}")
    if p.synopsis:
        lines += ["", p.synopsis]
    lines += ["", f"-# Source: {af.SOURCE_NAME} · times shown in your own timezone" + (f" · {tag}" if tag else "")]
    return "\n".join(lines)[:TEXT_LIMIT]


class _FollowButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^anime_follow:(\d+)$"):
    """Follow / Unfollow toggle on every anime card. Followers are mentioned when a new episode of that anime is posted in a
    server where they are a member. Free plan: up to FREE_FOLLOW_LIMIT anime per person."""

    def __init__(self, anime_id: int):
        self.anime_id = int(anime_id)
        super().__init__(discord.ui.Button(label="Follow", style=discord.ButtonStyle.secondary, emoji="🔔",
                                           custom_id=f"anime_follow:{self.anime_id}"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        title = _title_from_message(interaction) or f"Anime {self.anime_id}"
        try:
            state, n = await af.toggle_follow(interaction.user.id, self.anime_id, title)
        except Exception:
            logger.exception("[anime-feed] follow toggle failed")
            await interaction.response.send_message(OOPS, ephemeral=True)
            return
        text = {"followed": f"🔔 Following **{title}** ({n}/{af.FREE_FOLLOW_LIMIT}). I'll mention you when a new episode is posted.",
                "unfollowed": f"🔕 Unfollowed **{title}** ({n}/{af.FREE_FOLLOW_LIMIT} left in your list).",
                "limit": f"🚫 You already follow {n} anime, the free limit. Unfollow one first by tapping its Follow button again."}[state]
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


def _title_from_message(interaction: discord.Interaction) -> Optional[str]:
    """The title is the '## Title' heading of the card the button sits on (nothing extra is stored on the message)."""
    try:
        for c in _walk(interaction.message.components):
            m = re.match(r"^## (.+)", getattr(c, "content", "") or "")
            if m:
                return m.group(1).strip()[:120]
    except Exception:
        pass
    return None


def _walk(components):
    for c in components or []:
        yield c
        for attr in ("children", "components"):
            kids = getattr(c, attr, None)
            if kids:
                yield from _walk(kids)
        acc = getattr(c, "accessory", None)
        if acc is not None:
            yield acc


class _MoreButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^anime_more:(\d+)$"):
    """Read more: the full synopsis and details of this anime, shown only to the person who tapped (nothing is posted in the
    channel). Anyone can use it. Everything it needs is in the custom_id (the AniList id), so it works on any old card after a
    restart."""

    COOLDOWN = 3.0

    def __init__(self, anime_id: int):
        self.anime_id = int(anime_id)
        super().__init__(discord.ui.Button(label="Read more", style=discord.ButtonStyle.primary, emoji="📖",
                                           custom_id=f"anime_more:{self.anime_id}"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        wait = af.cooldown_left(interaction.user.id, "more", self.COOLDOWN)
        if wait > 0:
            await interaction.response.send_message(f"⏳ One moment, try again in {int(wait) + 1}s.", ephemeral=True)
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)      # answer inside Discord's 3 seconds first
            detail, ok = await af.get_detail(self.anime_id)
            await interaction.followup.send(view=detail_view(detail, ok, self.anime_id), ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            logger.exception("[anime-feed] read more failed")
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(OOPS, ephemeral=True)
                else:
                    await interaction.response.send_message(OOPS, ephemeral=True)
            except discord.HTTPException:
                pass


class _AlertButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^anime_alert:(\d+)$"):
    """Alert me: opens a private panel to pick how you're told about this anime (mention in the server / DM / mute). Anyone can use
    it. Everything needed is in the custom_id (the AniList id), so it works on any old card after a restart."""

    COOLDOWN = 2.0

    def __init__(self, anime_id: int):
        self.anime_id = int(anime_id)
        super().__init__(discord.ui.Button(label="Alert me", style=discord.ButtonStyle.secondary, emoji="⏰",
                                           custom_id=f"anime_alert:{self.anime_id}"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        wait = af.cooldown_left(interaction.user.id, "alert", self.COOLDOWN)
        if wait > 0:
            await interaction.response.send_message(f"⏳ One moment, try again in {int(wait) + 1}s.", ephemeral=True)
            return
        title = _title_from_message(interaction) or f"Anime {self.anime_id}"
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            guild_id = interaction.guild.id if interaction.guild else None
            panel = await AlertPanel.open(interaction.user.id, self.anime_id, title, guild_id)
            await interaction.followup.send(view=panel, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            logger.exception("[anime-feed] alert panel failed")
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(OOPS, ephemeral=True)
                else:
                    await interaction.response.send_message(OOPS, ephemeral=True)
            except discord.HTTPException:
                pass


def alert_text(title: str, mode: Optional[str], prefs: aa.Prefs, plus: bool, note: Optional[str] = None) -> str:
    lines = [note, ""] if note else []
    lines += [f"## ⏰ Alerts for {title}"[:200], f"**Now:** {aa.mode_label(mode)}", "",
              "Pick how I tell you when a new episode comes out. Choosing one also follows the anime.",
              "-# 🔔 mention = tagged in the server post · 📩 DM = a private message, only if you choose it · 🔕 mute = followed, no alert"]
    if plus:
        q = f"{prefs.quiet_start:02d}:00 to {prefs.quiet_end:02d}:00" if prefs.quiet_start is not None and prefs.quiet_end is not None else "off"
        tz = next((lbl for lbl, m in aa.TZ_CHOICES if m == prefs.tz_minutes), f"UTC{prefs.tz_minutes:+d} min")
        lines += ["", f"**Quiet hours:** {q} ({tz}). DM alerts that fall inside quiet hours are skipped."]
    return "\n".join(lines)[:3990]


class AlertPanel(discord.ui.LayoutView):
    """Private, short-lived panel (re-open it from the card's Alert me button any time; nothing here needs to survive a restart)."""

    def __init__(self, user_id: int, anime_id: int, title: str, guild_id: Optional[int], mode: Optional[str], prefs: aa.Prefs, plus: bool):
        super().__init__(timeout=300)
        self.user_id, self.anime_id, self.title, self.guild_id = user_id, anime_id, title, guild_id
        self.mode, self.prefs, self.plus = mode, prefs, plus
        self.note: Optional[str] = None
        self._build()

    @classmethod
    async def open(cls, user_id: int, anime_id: int, title: str, guild_id: Optional[int]) -> "AlertPanel":
        mode = await aa.get_mode(user_id, anime_id)
        prefs = await aa.get_prefs(user_id)
        plus = await af.has_anime_plus(user_id, guild_id)
        return cls(user_id, anime_id, title, guild_id, mode, prefs, plus)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("⏰ This panel is someone else's. Tap Alert me on the card for your own.", ephemeral=True)
            return False
        return True

    def _build(self) -> None:
        self.clear_items()
        modes = discord.ui.Select(placeholder="How should I alert you?", min_values=1, max_values=1, options=[
            discord.SelectOption(label="Mention me in the server", value=aa.MENTION, emoji="🔔", default=self.mode == aa.MENTION,
                                 description="Tagged in the post when the episode comes out"),
            discord.SelectOption(label="DM me" + ("" if self.plus else " (Anime+)"), value=aa.DM, emoji="📩", default=self.mode == aa.DM,
                                 description="A private message, even if the server feed is off"),
            discord.SelectOption(label="Mute", value=aa.MUTE, emoji="🔕", default=self.mode == aa.MUTE,
                                 description="Keep following, no alerts")])
        modes.callback = self._on_mode
        rows = [discord.ui.ActionRow(modes)]
        if self.plus:
            quiet = discord.ui.Select(placeholder="Quiet hours for DM alerts", min_values=1, max_values=1, options=[
                discord.SelectOption(label="No quiet hours", value="off", emoji="🔊", default=self.prefs.quiet_start is None)] + [
                discord.SelectOption(label=lbl, value=f"{a}-{b}", emoji="🌙", default=(self.prefs.quiet_start, self.prefs.quiet_end) == (a, b))
                for lbl, a, b in aa.QUIET_CHOICES])
            quiet.callback = self._on_quiet
            tz = discord.ui.Select(placeholder="Your timezone (for quiet hours)", min_values=1, max_values=1, options=[
                discord.SelectOption(label=lbl, value=str(m), default=m == self.prefs.tz_minutes) for lbl, m in aa.TZ_CHOICES])
            tz.callback = self._on_tz
            rows += [discord.ui.ActionRow(quiet), discord.ui.ActionRow(tz)]
        self.add_item(_info_box(alert_text(self.title, self.mode, self.prefs, self.plus, self.note), rows))

    async def _push(self, interaction: discord.Interaction) -> None:
        self._build()
        await interaction.response.edit_message(view=self)

    async def _on_mode(self, interaction: discord.Interaction):
        mode = (interaction.data.get("values") or [""])[0]
        try:
            if mode == aa.DM and not await af.has_anime_plus(self.user_id, self.guild_id):
                self.note = "🔒 DM alerts are part of Anime+. Mention and mute are free."
            else:
                state, n = await aa.set_mode(self.user_id, self.anime_id, self.title, mode, af.FREE_FOLLOW_LIMIT)
                if state == "limit":
                    self.note = f"🚫 You already follow {n} anime, the free limit. Unfollow one first."
                else:
                    self.mode = mode
                    self.note = {aa.MENTION: "✅ I'll mention you in the server post.", aa.DM: "✅ I'll DM you when it airs. Make sure your DMs are open.",
                                 aa.MUTE: "✅ Muted. You still follow it."}[mode]
        except Exception:
            logger.exception("[anime-feed] couldn't save the alert mode")
            self.note = OOPS
        await self._push(interaction)

    async def _save_prefs(self, interaction: discord.Interaction, prefs: aa.Prefs, ok_note: str):
        try:
            await aa.save_prefs(self.user_id, prefs)
            self.prefs, self.note = prefs, ok_note
        except Exception:
            logger.exception("[anime-feed] couldn't save alert settings")
            self.note = OOPS
        await self._push(interaction)

    async def _on_quiet(self, interaction: discord.Interaction):
        v = (interaction.data.get("values") or ["off"])[0]
        if v == "off":
            start = end = None
        else:
            a, b = v.split("-")
            start, end = int(a), int(b)
        await self._save_prefs(interaction, aa.Prefs(self.prefs.tz_minutes, start, end), "✅ Quiet hours saved.")

    async def _on_tz(self, interaction: discord.Interaction):
        try:
            m = int((interaction.data.get("values") or ["0"])[0])
        except ValueError:
            m = 0
        if m not in {x for _, x in aa.TZ_CHOICES}:
            m = 0
        await self._save_prefs(interaction, aa.Prefs(m, self.prefs.quiet_start, self.prefs.quiet_end), "✅ Timezone saved.")


def detail_text(d: af.Detail) -> str:
    meta = " · ".join(x for x in (ORIGIN_NAME.get(d.origin, ""), d.fmt, d.status, f"{d.episodes} episodes" if d.episodes else "") if x)
    lines = [f"## {d.title}"]
    if d.native and d.native != d.title:
        lines.append(f"-# {d.native}")
    if meta:
        lines.append(meta)
    if d.genres:
        lines.append(", ".join(d.genres))
    if d.next_episode and d.next_at:
        lines.append(f"**Episode {d.next_episode}** airs <t:{d.next_at}:F> (<t:{d.next_at}:R>)")
    lines += ["", d.synopsis or "_No synopsis available._", "", f"-# Source: {af.SOURCE_NAME}"]
    return "\n".join(lines)[:3990]


ORIGIN_NAME = af.ORIGIN_LABELS


def detail_view(d: Optional[af.Detail], ok: bool, anime_id: int) -> discord.ui.LayoutView:
    """The private Read more panel. Never raises; says plainly when AniList is unreachable or the title is unavailable."""
    view = discord.ui.LayoutView(timeout=None)
    box = discord.ui.Container(accent_colour=ACCENT)
    if d is None:
        box.add_item(discord.ui.TextDisplay("😿 I can't reach AniList right now. Try again in a few minutes." if not ok
                                            else "🚫 There are no details to show for this title."))
        box.add_item(discord.ui.ActionRow(discord.ui.Button(label="Open on AniList", style=discord.ButtonStyle.link,
                                                            url=f"https://anilist.co/anime/{int(anime_id)}")))
    else:
        box.add_item(discord.ui.TextDisplay(detail_text(d)))
        box.add_item(discord.ui.ActionRow(discord.ui.Button(label="Details on AniList", style=discord.ButtonStyle.link, url=d.url),
                                          _FollowButton(d.anime_id)))
    view.add_item(box)
    return view


DYNAMIC_ITEMS = (_FollowButton, _MoreButton, _AlertButton)


def build_card(p: af.Preview, *, tag: Optional[str] = None, extra_rows=(), ping: str = "") -> discord.ui.Container:
    box = discord.ui.Container(accent_colour=discord.Color(p.color) if p.color else ACCENT)
    if ping:
        box.add_item(discord.ui.TextDisplay(ping[:500]))
    if p.image:                         # ONE big full-width picture (banner, else the big cover); a missing image never blocks a post
        gallery = discord.ui.MediaGallery()
        gallery.add_item(media=p.image, description=p.title[:200])
        box.add_item(gallery)
    box.add_item(discord.ui.TextDisplay(card_text(p, tag=tag)))
    box.add_item(discord.ui.ActionRow(_MoreButton(p.anime_id), _FollowButton(p.anime_id), _AlertButton(p.anime_id),
                                      discord.ui.Button(label="Details on AniList", style=discord.ButtonStyle.link, url=p.url)))
    for row in extra_rows:
        box.add_item(row)
    return box


def post_view(p: af.Preview, *, tag: Optional[str] = None, ping: str = "") -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(build_card(p, tag=tag, ping=ping))
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
              f"**Ping role:** {f'<@&{cfg.ping_role_id}>' if cfg.ping_role_id else 'none (optional)'}",
              f"**Feed:** {'ON' if cfg.enabled and cfg.channel_id else 'OFF'}",
              "-# People who tap Follow on an anime get mentioned when its next episode is posted. New episodes are posted when they air. Only episodes that air after you turn the feed on are posted, never a backlog."]
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
        role = discord.ui.RoleSelect(placeholder="Optional: a role to ping on every new episode", min_values=0, max_values=1)
        role.callback = self._on_role
        ready = bool(self.cfg.channel_id)
        rows = [discord.ui.ActionRow(sel), discord.ui.ActionRow(role), discord.ui.ActionRow(
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

    async def _on_role(self, interaction: discord.Interaction):
        values = interaction.data.get("values") or []
        role = self.guild.get_role(int(values[0])) if values else None
        if values and (role is None or role.is_default() or role.managed):
            self.note = "🚫 Pick a normal role (not @everyone and not a bot's role), or clear the choice."
        elif role is not None and not role.mentionable and not self.guild.me.guild_permissions.mention_everyone:
            self.note = f"🚫 {role.mention} can't be pinged. Make it mentionable, or give me **Mention Everyone**."
        else:
            self.cfg.ping_role_id = role.id if role else None
            try:
                await af.save_config(self.cfg, interaction.user.id)
                self.note = f"✅ I'll ping {role.mention} on new episodes." if role else "Ping role cleared."
            except Exception:
                logger.exception("[anime-feed] couldn't save the ping role")
                self.note = OOPS
        await self._push(interaction)

    async def _on_preview(self, interaction: discord.Interaction):
        await interaction.response.defer()
        items, ok = await af.airing_soon()
        items = af.for_preview(items, int(time.time()))
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
