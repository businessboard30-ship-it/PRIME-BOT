# path: discord_bot/cogs/movies.py

"""
Movies hub (Premium). Legal sources only: TMDB (info, trailers, where it streams in your country) and the Internet
Archive's public-domain film collection. The bot never searches or downloads from piracy sites.

Setup (Manage Server, server must have Premium) is a private, owner-locked wizard built like the anime spawn wizard: channel,
country, optional role, ⚡ Quick setup, create-channel / create-role buttons, Preview and Close. It times out into a plain summary.
"Post the hub" puts the only persistent message in the server: a hub with Search / Trending / Free classics buttons (and an
opt-in role toggle) that every member can press. It opens from the join DM's "More features" screen. There is no slash command.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord.ext import commands

from discord_bot.auto_create import create_role, create_text_channel, toggle_member_role
from discord_bot.cogs.birthdays import _Fixed, _admin, _clone, _is_premium, _premium_gate
from modules import movie_store as store
from modules import movies as mv

logger = logging.getLogger(__name__)
COLOR = discord.Color.from_rgb(229, 9, 20)


def _can_post(channel, guild: discord.Guild) -> bool:
    me = guild.me
    if not channel or not me:
        return False
    p = channel.permissions_for(me)
    return bool(p.view_channel and p.send_messages and p.embed_links)


# ── embeds ──────────────────────────────────────────────────────────────

def detail_embed(d: dict, region: str) -> discord.Embed:
    title = f"{d['title']} ({d['year']})" if d.get("year") else d["title"]
    e = discord.Embed(title=title[:256], url=d["page"], description=(f"*{d['tagline']}*\n\n" if d.get("tagline") else "") + d["overview"],
                      color=COLOR)
    if d.get("poster"):
        e.set_thumbnail(url=d["poster"])
    facts = [f"⭐ {d['rating']}/10"]
    if d.get("runtime"):
        facts.append(f"⏱️ {d['runtime']} min")
    if d.get("seasons"):
        facts.append(f"📺 {d['seasons']} season(s)")
    e.add_field(name="Rating", value=" · ".join(facts), inline=False)
    if d.get("genres"):
        e.add_field(name="Genres", value=", ".join(d["genres"]), inline=True)
    if d.get("cast"):
        e.add_field(name="Cast", value=", ".join(d["cast"]), inline=True)
    where = []
    for label, key in (("Stream", "stream"), ("Rent", "rent"), ("Buy", "buy")):
        if d.get(key):
            where.append(f"**{label}:** {', '.join(d[key])}")
    e.add_field(name=f"Where to watch legally ({mv.REGIONS.get(region, region)})",
                value="\n".join(where) or "Not listed for your country right now.", inline=False)
    e.set_footer(text=mv.ATTRIBUTION)
    return e


def detail_links(d: dict) -> list:
    links = [discord.ui.Button(label="TMDB page", url=d["page"])]
    if d.get("trailer"):
        links.append(discord.ui.Button(label="Trailer", emoji="▶️", url=d["trailer"]))
    if d.get("watch_link"):
        links.append(discord.ui.Button(label="Where to watch", emoji="📺", url=d["watch_link"]))
    return links


class ResultsView(discord.ui.View):
    """Pick one of the results to see its details. Temporary (ephemeral message), so it needs no persistence."""

    def __init__(self, results: list, region: str):
        super().__init__(timeout=300)
        self.region = region
        self.select = discord.ui.Select(placeholder="Pick one", options=[
            discord.SelectOption(label=f"{r['title']} ({r['year']})"[:100] if r["year"] else r["title"][:100],
                                 value=f"{r['type']}:{r['id']}", description=f"{'Series' if r['type'] == 'tv' else 'Movie'} · ⭐ {r['rating']}")
            for r in results])
        self.select.callback = self._picked
        self.add_item(self.select)

    async def _picked(self, interaction: discord.Interaction):
        kind, _, raw = self.select.values[0].partition(":")
        await interaction.response.defer()
        d = await mv.details(kind, int(raw), self.region)
        if not d:
            return await interaction.followup.send("I couldn't load that title. Try another.", ephemeral=True)
        view = discord.ui.View(timeout=300)
        view.add_item(self.select)
        for b in detail_links(d):
            view.add_item(b)
        await interaction.edit_original_response(embed=detail_embed(d, self.region), view=view)


async def _hub_ready(interaction: discord.Interaction) -> bool:
    """Every hub press: server must have Premium and the TMDB key must be set."""
    if interaction.guild is None:
        await interaction.response.send_message("Use this inside a server.", ephemeral=True)
        return False
    if not await _premium_gate(interaction, interaction.guild, _clone(interaction)):
        return False
    return True


async def _no_key(interaction: discord.Interaction, send):
    try:
        from modules import owner_env
        owner_env.note_missing("TMDB_API_KEY", "the Movies feature")
    except Exception:
        pass
    await send("The movie database isn't connected yet. The bot owner needs to add the TMDB key. Try again later.", ephemeral=True)


class SearchModal(discord.ui.Modal, title="Search movies & series"):
    query = discord.ui.TextInput(label="Title", placeholder="e.g. Black Panther", max_length=80)

    async def on_submit(self, interaction: discord.Interaction):
        if not await _hub_ready(interaction):
            return
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        if not mv.configured():
            return await _no_key(interaction, interaction.followup.send)
        cfg = await store.get_config(interaction.guild.id, _clone(interaction))
        results = await mv.search(str(self.query))
        if not results:
            return await interaction.followup.send(f"Nothing found for **{discord.utils.escape_markdown(str(self.query))}**.", ephemeral=True)
        await interaction.followup.send("Here's what I found:", view=ResultsView(results, mv.valid_region(cfg.get("region"))), ephemeral=True)


class ClassicsModal(discord.ui.Modal, title="Free classic films"):
    query = discord.ui.TextInput(label="Title or word", placeholder="e.g. Dracula", max_length=60)

    async def on_submit(self, interaction: discord.Interaction):
        if not await _hub_ready(interaction):
            return
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        items = await mv.free_classics(str(self.query))
        if not items:
            return await interaction.followup.send("No free public-domain film matched that. Try a different word.", ephemeral=True)
        lines = [f"🏛️ [{i['title']}]({i['url']})" + (f" ({i['year']})" if i["year"] else "") for i in items]
        e = discord.Embed(title="Free classics (public domain)", description="\n".join(lines), color=COLOR)
        e.set_footer(text="From the Internet Archive's public-domain film collection. Watch free and legally.")
        await interaction.followup.send(embed=e, ephemeral=True)


# ── hub (persistent, everyone presses these) ────────────────────────────

class SearchButton(_Fixed, discord.ui.DynamicItem[discord.ui.Button], template=r"^movie:search$"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="Search", emoji="🔎", style=discord.ButtonStyle.primary, custom_id="movie:search"))

    async def callback(self, interaction: discord.Interaction):
        if not await _hub_ready(interaction):
            return
        await interaction.response.send_modal(SearchModal())


class TrendingButton(_Fixed, discord.ui.DynamicItem[discord.ui.Button], template=r"^movie:trending$"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="Trending", emoji="🔥", style=discord.ButtonStyle.secondary, custom_id="movie:trending"))

    async def callback(self, interaction: discord.Interaction):
        if not await _hub_ready(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        if not mv.configured():
            return await _no_key(interaction, interaction.followup.send)
        cfg = await store.get_config(interaction.guild.id, _clone(interaction))
        results = await mv.trending()
        if not results:
            return await interaction.followup.send("I couldn't load what's trending. Try again in a minute.", ephemeral=True)
        await interaction.followup.send("🔥 Trending this week:", view=ResultsView(results, mv.valid_region(cfg.get("region"))), ephemeral=True)


class ClassicsButton(_Fixed, discord.ui.DynamicItem[discord.ui.Button], template=r"^movie:classics$"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="Free classics", emoji="🏛️", style=discord.ButtonStyle.secondary, custom_id="movie:classics"))

    async def callback(self, interaction: discord.Interaction):
        if not await _hub_ready(interaction):
            return
        await interaction.response.send_modal(ClassicsModal())


class RoleToggleButton(_Fixed, discord.ui.DynamicItem[discord.ui.Button], template=r"^movie:rolebtn$"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="Movie Fans role", emoji="🔔", style=discord.ButtonStyle.secondary, custom_id="movie:rolebtn"))

    async def callback(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return
        cfg = await store.get_config(interaction.guild.id, _clone(interaction))
        role = interaction.guild.get_role(cfg["role_id"]) if cfg.get("role_id") else None
        if role is None:
            return await interaction.response.send_message("That role no longer exists. An admin can set it up again.", ephemeral=True)
        has, err = await toggle_member_role(interaction.user, role)
        msg = err or (f"✅ You now have {role.mention}." if has else f"You no longer have {role.mention}.")
        await interaction.response.send_message(msg, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


def build_hub_view(show_role: bool) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    box = discord.ui.Container(accent_colour=COLOR)
    box.add_item(discord.ui.TextDisplay(
        "## 🎬 Movies & Series\nLook up any movie or series: plot, cast, rating, trailer and **where it streams legally** in "
        "your country. Or find free public-domain classics.\n-# " + mv.ATTRIBUTION))
    box.add_item(discord.ui.Separator())
    items = [SearchButton(), TrendingButton(), ClassicsButton()] + ([RoleToggleButton()] if show_role else [])
    box.add_item(discord.ui.ActionRow(*items))
    view.add_item(box)
    return view


# ── setup wizard (Manage Server + Premium), same model as the anime spawn wizard ──────────

AUTO_CHANNEL = "movies"
AUTO_ROLE = "Movie Fans"
OOPS = "😿 Something went wrong saving that. Try again."


def _config_text(guild: discord.Guild, cfg: dict, note: Optional[str] = None) -> str:
    ch = guild.get_channel(cfg["channel_id"]) if cfg.get("channel_id") else None
    role = guild.get_role(cfg["role_id"]) if cfg.get("role_id") else None
    parts = [note, ""] if note else []
    parts += ["## 🎬 Movies 💎",
              "A Movies hub for your members: plot, cast, trailers, **where it streams legally** in your country, and free public-domain "
              "classics. Pick a channel (or let me create one) and I post the hub there. **Quick setup** does it in one tap. "
              "Until you set this up, I do nothing in your server.", "",
              f"**Hub channel:** {ch.mention if ch else 'not picked yet'}",
              f"**Country (where to watch):** {mv.REGIONS[mv.valid_region(cfg.get('region'))]}",
              f"**Opt-in role:** {role.mention if role else 'none (optional)'}",
              "-# Movies is a Premium feature. Members can take the role with the 🔔 button on the hub."]
    return "\n".join(parts)


def _btn(label, cb, style=discord.ButtonStyle.secondary, emoji=None, disabled=False) -> discord.ui.Button:
    b = discord.ui.Button(label=label, style=style, emoji=emoji, disabled=disabled)
    b.callback = cb
    return b


class MovieSetupView(discord.ui.LayoutView):
    """Private, owner-locked wizard. Times out into a plain summary; the only thing left in the server is the hub itself."""

    def __init__(self, guild: discord.Guild, clone_id, owner_id: int, cfg: dict):
        super().__init__(timeout=900)
        self.guild, self.clone_id, self.owner_id, self.cfg = guild, clone_id, owner_id, cfg
        self.note: Optional[str] = None
        self.message: Optional[discord.Message] = None
        self._build()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            try:
                await interaction.response.send_message("🎬 This setup belongs to the person who opened it.", ephemeral=True)
            except discord.HTTPException:
                pass
            return False
        return True

    def _build(self) -> None:
        self.clear_items()
        ready = bool(self.cfg.get("channel_id"))
        sel = discord.ui.ChannelSelect(channel_types=[discord.ChannelType.text], placeholder="Pick the Movies channel")
        sel.callback = self._on_channel
        region = discord.ui.Select(placeholder="Country for 'where to watch'", options=[
            discord.SelectOption(label=name, value=code, default=(code == mv.valid_region(self.cfg.get("region"))))
            for code, name in mv.REGIONS.items()])
        region.callback = self._on_region
        role = discord.ui.RoleSelect(placeholder="Optional: a role members can opt in to", min_values=0, max_values=1)
        role.callback = self._on_role
        rows = [discord.ui.ActionRow(sel), discord.ui.ActionRow(region), discord.ui.ActionRow(role),
                discord.ui.ActionRow(
                    _btn("Quick setup", self._on_quick, discord.ButtonStyle.success, "⚡"),
                    _btn("Create channel", self._on_create_channel, emoji="➕"),
                    _btn("Create role", self._on_create_role, emoji="🎭")),
                discord.ui.ActionRow(
                    _btn("Preview", self._on_preview, discord.ButtonStyle.primary, "👀"),
                    _btn("Post the hub", self._on_post, emoji="📌", disabled=not ready),
                    _btn("Close", self._on_close))]
        box = discord.ui.Container(accent_colour=COLOR)
        box.add_item(discord.ui.TextDisplay(_config_text(self.guild, self.cfg, self.note)[:3900]))
        for r in rows:
            box.add_item(r)
        self.add_item(box)

    async def _push(self, interaction: discord.Interaction) -> None:
        self._build()
        if interaction.response.is_done():
            await interaction.edit_original_response(view=self)
        else:
            await interaction.response.edit_message(view=self)

    async def _save(self, **fields) -> bool:
        try:
            self.cfg = await store.set_config(self.guild.id, self.clone_id, **fields)
            return True
        except Exception:
            logger.exception("[movies] couldn't save settings")
            self.note = OOPS
            return False

    async def _on_channel(self, interaction: discord.Interaction):
        await interaction.response.defer()
        ch = self.guild.get_channel(int(interaction.data["values"][0]))
        if not _can_post(ch, self.guild):
            self.note = f"🚫 I need **View Channel, Send Messages** and **Embed Links** in {ch.mention if ch else 'that channel'}."
        elif await self._save(channel_id=ch.id):
            self.note = f"✅ Movies channel: {ch.mention}."
        await self._push(interaction)

    async def _on_region(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if await self._save(region=mv.valid_region(interaction.data["values"][0])):
            self.note = f"✅ Where-to-watch country: {mv.REGIONS[self.cfg['region']]}."
        await self._push(interaction)

    async def _on_role(self, interaction: discord.Interaction):
        await interaction.response.defer()
        vals = interaction.data.get("values") or []
        role = self.guild.get_role(int(vals[0])) if vals else None
        if await self._save(role_id=role.id if role else None):
            self.note = f"✅ Members can opt in to {role.mention}." if role else "Role cleared."
        await self._push(interaction)

    async def _make_channel(self) -> Optional[discord.TextChannel]:
        ch, err = await create_text_channel(self.guild, AUTO_CHANNEL, "Look up movies and series, trailers and where to watch legally.")
        if err:
            self.note = "🚫 " + err
        return ch

    async def _on_create_channel(self, interaction: discord.Interaction):
        await interaction.response.defer()
        ch = await self._make_channel()
        if ch is not None and await self._save(channel_id=ch.id):
            self.note = f"✅ Channel ready: {ch.mention}."
        await self._push(interaction)

    async def _on_create_role(self, interaction: discord.Interaction):
        await interaction.response.defer()
        role, err = await create_role(self.guild, AUTO_ROLE, discord.Color.red())
        if err:
            self.note = "🚫 " + err
        elif await self._save(role_id=role.id):
            self.note = f"✅ Role ready: {role.mention}. Members opt in from the hub."
        await self._push(interaction)

    async def _post_hub(self, ch: discord.TextChannel) -> bool:
        role_ok = bool(self.cfg.get("role_id") and self.guild.get_role(self.cfg["role_id"]))
        try:
            await ch.send(view=build_hub_view(role_ok), allowed_mentions=discord.AllowedMentions.none())
            return True
        except (discord.Forbidden, discord.HTTPException):
            self.note = f"🚫 I couldn't post in {ch.mention}. Give me **Send Messages** and **Embed Links** there."
            return False

    async def _on_quick(self, interaction: discord.Interaction):
        """Channel (kept if picked, otherwise created) + the hub posted, in one tap. Premium."""
        if not await _premium_gate(interaction, self.guild, self.clone_id):
            return
        await interaction.response.defer()
        ch = self.guild.get_channel(self.cfg["channel_id"]) if self.cfg.get("channel_id") else None
        if not _can_post(ch, self.guild):
            ch = await self._make_channel()
        if ch is not None and await self._save(channel_id=ch.id) and await self._post_hub(ch):
            self.note = f"⚡ All set. The Movies hub is live in {ch.mention}. Change anything below any time."
        await self._push(interaction)

    async def _on_post(self, interaction: discord.Interaction):
        if not await _premium_gate(interaction, self.guild, self.clone_id):
            return
        await interaction.response.defer()
        ch = self.guild.get_channel(self.cfg["channel_id"]) if self.cfg.get("channel_id") else None
        if ch is None:
            self.note = "🚫 That channel is gone. Pick another."
        elif await self._post_hub(ch):
            self.note = f"📌 The Movies hub is live in {ch.mention}."
        await self._push(interaction)

    async def _on_preview(self, interaction: discord.Interaction):
        """Shows what a member sees: this week's top trending title, in your country."""
        await interaction.response.defer()
        if not mv.configured():
            self.note = "🔑 The movie database isn't connected yet. The bot owner needs to add the TMDB key."
            return await self._push(interaction)
        top = await mv.trending()
        d = await mv.details(top[0]["type"], top[0]["id"], mv.valid_region(self.cfg.get("region"))) if top else None
        if d:
            view = discord.ui.View(timeout=300)
            for b in detail_links(d):
                view.add_item(b)
            await interaction.followup.send(embed=detail_embed(d, mv.valid_region(self.cfg.get("region"))), view=view, ephemeral=True)
        else:
            self.note = "😿 I can't reach the movie database right now. Try again in a minute."
            await self._push(interaction)

    async def _on_close(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(view=_final_view(_config_text(self.guild, self.cfg)))

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(view=_final_view(_config_text(self.guild, self.cfg)))
            except discord.HTTPException:
                pass


def _final_view(text: str) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    box = discord.ui.Container(accent_colour=COLOR)
    box.add_item(discord.ui.TextDisplay(text[:3900]))
    view.add_item(box)
    return view


async def open_setup(bot, guild: discord.Guild, clone_id, owner_id: int, channel) -> discord.Message:
    """Posts the wizard in `channel` for `owner_id`. Raises discord.HTTPException if it can't post."""
    cfg = await store.get_config(guild.id, clone_id)
    view = MovieSetupView(guild, clone_id, owner_id, cfg)
    msg = await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
    view.message = msg
    return msg


DYNAMIC_ITEMS = (SearchButton, TrendingButton, ClassicsButton, RoleToggleButton)


class MoviesCog(commands.Cog):
    """No commands: the panel opens from More features. Loaded so the persistent items are registered with the bot."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot


async def setup(bot: commands.Bot):
    await bot.add_cog(MoviesCog(bot))
