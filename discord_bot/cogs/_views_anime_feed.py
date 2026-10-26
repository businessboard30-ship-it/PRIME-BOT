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

from modules import anime_feed as af
from modules import anime_feed_spawn as spawn

logger = logging.getLogger(__name__)

ACCENT = discord.Color.from_rgb(88, 101, 242)
OOPS = "😿 Something went wrong. Try again in a moment."
TEXT_LIMIT = 3800


# ── the card (shared by previews, test posts and later real posts) ───────

def card_text(p: af.Preview, *, tag: Optional[str] = None) -> str:
    head = f"## {p.title}"
    meta = " · ".join(x for x in (af.origin_label(p), p.fmt, ", ".join(p.genres)) if x)
    if p.airing_at:                     # a real episode post
        verb = "aired" if p.airing_at <= int(time.time()) else "airs"
        lines = [head, f"**{af.episode_label(p)}** {verb} <t:{p.airing_at}:F> (<t:{p.airing_at}:R>)"]
        foot = f"-# Source: {af.SOURCE_NAME} · times shown in your own timezone"
    else:                               # a spawn: no airing line, just what it is
        eps = f"{p.episodes} episodes" if p.episodes else ""
        lines = [f"### 🎌 An anime appeared!", head] + ([f"**{eps}**"] if eps else [])
        foot = f"-# Source: {af.SOURCE_NAME}"
    if meta:
        lines.append(f"-# {meta}")
    if p.synopsis:
        lines += ["", p.synopsis]
    lines += ["", foot + (f" · {tag}" if tag else "")]
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
            clone = getattr(interaction.client, "clone_id", None) or 0
            limit = await _plus_mod().follow_limit(interaction.user.id, interaction.guild.id if interaction.guild else None, clone)
            state, n = await af.toggle_follow(interaction.user.id, self.anime_id, title, limit)
        except Exception:
            logger.exception("[anime-feed] follow toggle failed")
            await interaction.response.send_message(OOPS, ephemeral=True)
            return
        text = {"followed": f"🔔 Following **{title}** ({n}/{limit}). I'll mention you when a new episode is posted.",
                "unfollowed": f"🔕 Unfollowed **{title}** ({n}/{limit} in your list).",
                "limit": f"🚫 You already follow {n} anime, your limit. Unfollow one first by tapping its Follow button again." + (f" Tap **Upgrade** to follow up to {_plus_mod().PLUS_FOLLOW_LIMIT}." if limit < _plus_mod().PLUS_FOLLOW_LIMIT else "")}[state]
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


def _plus_mod():
    from modules import anime_plus
    return anime_plus


def _title_from_message(interaction: discord.Interaction) -> Optional[str]:
    """The title is the '## Title' heading of the card the button sits on (nothing extra is stored on the message)."""
    try:
        for c in _walk(interaction.message.components):
            m = re.match(r"^## (.+)", getattr(c, "content", "") or "")
            if m:
                return m.group(1).strip()[:120]
    except Exception:
        logger.debug("swallowed exception in _title_from_message", exc_info=True)
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


def _actions():
    """The panel buttons live in _views_anime_actions (which imports this module), so they are loaded lazily."""
    from discord_bot.cogs import _views_anime_actions as aa
    return aa


def _browse():
    from discord_bot.cogs import _views_anime_browse as ab
    return ab


def _plus():
    from discord_bot.cogs import _views_anime_plus as ap
    return ap


def _nudge():
    from discord_bot.cogs import _views_anime_nudge
    return _views_anime_nudge


def _all_dynamic_items():
    return (_FollowButton, _MoreButton) + tuple(_nudge().NUDGE_ITEMS) + tuple(_actions().ACTION_ITEMS) + tuple(_browse().BROWSE_ITEMS) + tuple(_plus().PLUS_ITEMS)


# -- old buttons ------------------------------------------------------------------------------
# Every anime button is a persistent DynamicItem that rebuilds from its custom_id alone, so a card keeps working across restarts and
# deploys. This is the safety net for the rare button that no longer matches anything (an id from a retired feature, or one that no
# longer fits its template): instead of Discord's "This interaction failed", the person gets a private pointer to a fresh card.
# Families are the text before the first ':'. When a feature is ever REMOVED, move its family into RETIRED_FAMILIES so its old cards
# still get this message. Only these families are answered, never another module's ids.
LIVE_FAMILIES = frozenset({"anime_follow", "anime_more", "anime_alert", "anime_amode", "anime_atz", "anime_aq", "anime_mute", "anime_pg",
                           "anime_search", "anime_cats", "anime_catsel", "anime_plus", "anime_plus_pay", "anime_plus_verify", "anime_nudge"})
RETIRED_FAMILIES: frozenset = frozenset()
STALE_TEXT = "🔄 That button is from an older version of this card and doesn't work any more. Tap a button on the **newest anime post** instead."


def is_stale_anime_id(custom_id: str) -> bool:
    """True for an id of ours (or a retired family of ours) that no current item will answer."""
    if not isinstance(custom_id, str) or custom_id.split(":", 1)[0] not in (LIVE_FAMILIES | RETIRED_FAMILIES):
        return False
    return not any(cls.__discord_ui_compiled_template__.fullmatch(custom_id) for cls in _all_dynamic_items())


async def stale_button_listener(interaction: discord.Interaction) -> None:
    """`on_interaction` listener. Does nothing for every interaction except a component press whose id is stale."""
    try:
        if interaction.type is not discord.InteractionType.component or interaction.response.is_done():
            return
        if is_stale_anime_id((interaction.data or {}).get("custom_id", "")):
            await interaction.response.send_message(STALE_TEXT, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        logger.debug("[anime-feed] couldn't answer a stale button", exc_info=True)


def __getattr__(name: str):
    """`DYNAMIC_ITEMS` (every persistent anime item, registered at startup) is built on first use, which avoids an import cycle."""
    if name == "DYNAMIC_ITEMS":
        return _all_dynamic_items()
    raise AttributeError(name)


def build_card(p: af.Preview, *, tag: Optional[str] = None, extra_rows=(), ping: str = "", dm: bool = False) -> discord.ui.Container:
    box = discord.ui.Container(accent_colour=discord.Color(p.color) if p.color else ACCENT)
    if ping:
        box.add_item(discord.ui.TextDisplay(ping[:500]))
    if p.image:                         # ONE big full-width picture (banner, else the big cover); a missing image never blocks a post
        gallery = discord.ui.MediaGallery()
        gallery.add_item(media=p.image, description=p.title[:200])
        box.add_item(gallery)
    box.add_item(discord.ui.TextDisplay(card_text(p, tag=tag)))
    aa = _actions()
    link = discord.ui.Button(label="Details on AniList", style=discord.ButtonStyle.link, url=p.url)
    if dm:                              # a DM alert: Mute is the one-tap way out
        box.add_item(discord.ui.ActionRow(_MoreButton(p.anime_id), aa._MuteButton(p.anime_id), aa._AlertButton(p.anime_id), link))
    else:                               # row 1: this anime; row 2: find more
        ab = _browse()
        box.add_item(discord.ui.ActionRow(_MoreButton(p.anime_id), _FollowButton(p.anime_id), aa._AlertButton(p.anime_id), _plus()._UpgradeButton()))
        box.add_item(discord.ui.ActionRow(ab._SearchButton(), ab._CategoriesButton(), link))
    for row in extra_rows:
        box.add_item(row)
    return box


def post_view(p: af.Preview, *, tag: Optional[str] = None, ping: str = "", dm: bool = False) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(build_card(p, tag=tag, ping=ping, dm=dm))
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


AUTO_CHANNEL_NAME = "anime-spawns"
AUTO_ROLE_NAME = "Anime Pings"
KIND_LABELS = {"both": "Anime and donghua", "anime": "Anime only", "donghua": "Donghua only (Chinese animation)"}


def setup_text(cfg: af.FeedConfig, note: Optional[str] = None) -> str:
    parts = [note, ""] if note else []
    parts += ["## 🎌 Anime spawns",
              "Pick a channel (or let me create one), choose how often an anime should spawn there, and turn it on. **Quick setup** does all of it in one tap. Each spawn is a random popular title from AniList with every button on it "
              "(Read more, Follow, Alerts, Search, Categories). Adult titles are never shown. Until you set this up, I do nothing in your server.", "",
              f"**Channel:** {f'<#{cfg.channel_id}>' if cfg.channel_id else 'not picked yet'}",
              f"**Spawns:** {af.interval_label(cfg.spawn_interval_minutes)}",
              f"**Ping role:** {f'<@&{cfg.ping_role_id}>' if cfg.ping_role_id else 'none (optional)'}",
              f"**Posts:** {KIND_LABELS[af.clean_kinds(cfg.content_kinds)]}",
              f"**Spawning:** {'ON' if cfg.enabled and cfg.channel_id else 'OFF (asleep)'}",
              "-# People who tap Follow on an anime get mentioned when it spawns, and can get a DM when its episodes air. The first spawn comes one interval after you turn this on."]
    return "\n".join(parts)


def _interval_option(m: int, current: int) -> discord.SelectOption:
    return discord.SelectOption(label=af.interval_label(m).capitalize(), value=str(m), default=(m == af.clean_interval(current)))


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
        role = discord.ui.RoleSelect(placeholder="Optional: a role to ping on every spawn", min_values=0, max_values=1)
        role.callback = self._on_role
        ready = bool(self.cfg.channel_id)
        kinds = discord.ui.Select(placeholder="What to post", min_values=1, max_values=1, options=[
            discord.SelectOption(label=KIND_LABELS[k], value=k, default=(k == af.clean_kinds(self.cfg.content_kinds))) for k in af.KINDS])
        kinds.callback = self._on_kinds
        pace = discord.ui.Select(placeholder="How often should an anime spawn?", min_values=1, max_values=1,
                                 options=[_interval_option(m, self.cfg.spawn_interval_minutes) for m in af.SPAWN_INTERVALS])
        pace.callback = self._on_interval
        rows = [discord.ui.ActionRow(sel), discord.ui.ActionRow(pace), discord.ui.ActionRow(role), discord.ui.ActionRow(kinds), discord.ui.ActionRow(
            _btn("Quick setup", self._on_quick, discord.ButtonStyle.success, "⚡"),
            _btn("Create channel", self._on_create_channel, emoji="➕"),
            _btn("Create ping role", self._on_create_role, emoji="🎭")), discord.ui.ActionRow(
            _btn("Preview", self._on_preview, discord.ButtonStyle.primary, "👀"),
            _btn("Post a test", self._on_test, emoji="🧪", disabled=not ready),
            _btn("Turn off" if self.cfg.enabled else "Turn on", self._on_toggle,
                 discord.ButtonStyle.danger if self.cfg.enabled else discord.ButtonStyle.success, disabled=not ready),
            _btn("Close", self._on_close))]
        self.add_item(_info_box(setup_text(self.cfg, self.note), rows))

    def _build_preview(self) -> None:
        if not self.previews:
            self.add_item(_info_box(self.note or "Nothing to preview right now.",
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
                self.note = f"✅ I'll ping {role.mention} on every spawn." if role else "Ping role cleared."
            except Exception:
                logger.exception("[anime-feed] couldn't save the ping role")
                self.note = OOPS
        await self._push(interaction)

    async def _on_interval(self, interaction: discord.Interaction):
        values = interaction.data.get("values") or []
        old = self.cfg.spawn_interval_minutes
        self.cfg.spawn_interval_minutes = af.clean_interval(values[0] if values else None)
        try:
            await af.save_config(self.cfg, interaction.user.id)
            self.note = f"✅ An anime will spawn {af.interval_label(self.cfg.spawn_interval_minutes)}."
        except Exception:
            logger.exception("[anime-feed] couldn't save the spawn interval")
            self.cfg.spawn_interval_minutes = old
            self.note = OOPS
        await self._push(interaction)

    async def _on_kinds(self, interaction: discord.Interaction):
        values = interaction.data.get("values") or []
        self.cfg.content_kinds = af.clean_kinds(values[0] if values else None)
        try:
            await af.save_config(self.cfg, interaction.user.id)
            self.note = f"✅ This server's feed now posts: {KIND_LABELS[self.cfg.content_kinds]}."
        except Exception:
            logger.exception("[anime-feed] couldn't save the content kinds")
            self.note = OOPS
        await self._push(interaction)


    # ── auto-create (channel, role) and one-tap quick setup ──
    async def _make_channel(self) -> Optional[discord.TextChannel]:
        """The #anime-spawns channel: reuse one that already exists and I can write in, otherwise create it. Sets self.note on failure."""
        me = self.guild.me
        existing = discord.utils.get(self.guild.text_channels, name=AUTO_CHANNEL_NAME)
        if existing is not None and existing.permissions_for(me).send_messages:
            return existing
        if not me.guild_permissions.manage_channels:
            self.note = "🚫 I need the **Manage Channels** permission to create the channel. Give it to me, or pick a channel yourself."
            return None
        try:
            return await self.guild.create_text_channel(
                AUTO_CHANNEL_NAME, topic="A random anime or donghua spawns here. Tap Follow on one to get pinged for new episodes.",
                overwrites={me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, read_message_history=True)},
                reason="Anime spawns setup (requested by a server manager)")
        except (discord.Forbidden, discord.HTTPException):
            logger.warning("[anime-feed] couldn't create the anime channel in guild %s", self.guild.id, exc_info=True)
            self.note = "🚫 I couldn't create the channel. Check my **Manage Channels** permission, or pick a channel yourself."
            return None

    async def _on_create_channel(self, interaction: discord.Interaction):
        await interaction.response.defer()
        channel = await self._make_channel()
        if channel is not None:
            self.cfg.channel_id = channel.id
            try:
                await af.save_config(self.cfg, interaction.user.id)
                self.note = f"✅ Channel ready: {channel.mention}."
            except Exception:
                logger.exception("[anime-feed] couldn't save the created channel")
                self.note = OOPS
        await self._push(interaction)

    async def _on_create_role(self, interaction: discord.Interaction):
        await interaction.response.defer()
        me = self.guild.me
        role = discord.utils.get(self.guild.roles, name=AUTO_ROLE_NAME)
        if role is None or role.managed or role.is_default():
            if not me.guild_permissions.manage_roles:
                self.note = "🚫 I need the **Manage Roles** permission to create the role. Give it to me, or pick a role yourself."
                await self._push(interaction)
                return
            try:
                role = await self.guild.create_role(name=AUTO_ROLE_NAME, mentionable=True, reason="Anime spawns setup (requested by a server manager)")
            except (discord.Forbidden, discord.HTTPException):
                logger.warning("[anime-feed] couldn't create the ping role in guild %s", self.guild.id, exc_info=True)
                self.note = "🚫 I couldn't create the role. Check my **Manage Roles** permission, or pick a role yourself."
                await self._push(interaction)
                return
        self.cfg.ping_role_id = role.id
        try:
            await af.save_config(self.cfg, interaction.user.id)
            self.note = f"✅ I'll ping {role.mention} on every spawn. Members can take the role from your self-roles or you can give it out."
        except Exception:
            logger.exception("[anime-feed] couldn't save the created role")
            self.note = OOPS
        await self._push(interaction)

    async def _on_quick(self, interaction: discord.Interaction):
        """Channel (kept if already picked, otherwise created) + the interval already shown + switched on, in one tap."""
        await interaction.response.defer()
        channel = self.guild.get_channel(self.cfg.channel_id) if self.cfg.channel_id else None
        if channel is None or not channel.permissions_for(self.guild.me).send_messages:
            channel = await self._make_channel()
        if channel is not None:
            self.cfg.channel_id, self.cfg.enabled = channel.id, True
            try:
                await af.save_config(self.cfg, interaction.user.id)
                self.note = (f"⚡ All set. Anime spawns are ON in {channel.mention}, {af.interval_label(self.cfg.spawn_interval_minutes)}. "
                             "The first one comes after one interval. Change anything below any time.")
            except Exception:
                logger.exception("[anime-feed] quick setup couldn't save")
                self.cfg.enabled = False
                self.note = OOPS
        await self._push(interaction)

    async def _on_preview(self, interaction: discord.Interaction):
        await interaction.response.defer()
        items, ok = await spawn.sample(self.cfg.content_kinds)
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
        if channel is None:
            self.note = "🚫 That channel is gone. Pick another."
        else:
            p, ok = await spawn.pick(self.cfg.content_kinds, [])
            if p is None:
                self.note = "😿 There's nothing to post right now (I can't reach AniList). Try again in a few minutes."
            else:
                try:
                    await channel.send(view=post_view(p, tag="Test spawn"), allowed_mentions=discord.AllowedMentions.none())
                    self.note = f"🧪 Test spawn sent to {channel.mention}."
                except (discord.Forbidden, discord.HTTPException):
                    logger.warning("[anime-feed] test post failed in guild %s", self.guild.id, exc_info=True)
                    self.note = "🚫 I couldn't post there. Check **Send Messages** and **Embed Links** for me in that channel."
        await self._push(interaction)

    async def _on_toggle(self, interaction: discord.Interaction):
        self.cfg.enabled = not self.cfg.enabled
        try:
            await af.save_config(self.cfg, interaction.user.id)
            self.note = (f"✅ Anime spawns are ON. The first one comes {af.interval_label(self.cfg.spawn_interval_minutes).replace('every', 'in', 1) if False else 'after one interval'}.") if self.cfg.enabled else "Anime spawns are OFF. I'm asleep in this server."
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


async def open_setup_ephemeral(interaction: discord.Interaction, guild: discord.Guild, clone_id) -> None:
    """Opens the wizard privately for the person who tapped a button (used by the mod-log reminder)."""
    cfg = await af.get_config(guild.id, clone_id)
    view = AnimeSetupView(guild, clone_id, interaction.user.id, cfg)
    await interaction.response.send_message(view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    try:
        view.message = await interaction.original_response()
    except discord.HTTPException:
        pass
