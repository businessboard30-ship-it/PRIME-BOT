# path: discord_bot/cogs/movies.py

"""
Movies hub (Premium). Legal sources only: TMDB (info, trailers, where it streams in your country) and the Internet
Archive's public-domain film collection. The bot never searches or downloads from piracy sites.

Setup (Manage Server, server must have Premium) is one persistent Components V2 panel: channel, country, optional role,
with buttons to create the channel and the role for you. "Post hub" puts a persistent hub message in the channel with
Search / Trending / Free classics buttons (and an opt-in role toggle) that every member can press.

The panel opens from the join DM's "More features" screen. There is no slash command.
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


# ── setup panel (Manage Server + Premium) ───────────────────────────────

def _config_text(guild: discord.Guild, cfg: dict, premium: bool) -> str:
    ch = guild.get_channel(cfg["channel_id"]) if cfg.get("channel_id") else None
    role = guild.get_role(cfg["role_id"]) if cfg.get("role_id") else None
    return "\n".join([
        "## 🎬 Movies setup 💎",
        "A Movies hub for your members: plot, cast, trailers, where to watch legally, and free public-domain classics. "
        "This is a **Premium** feature." + ("" if premium else " This server doesn't have Premium yet."),
        "",
        f"**Hub channel:** {ch.mention if ch else 'not set'}",
        f"**Country (where to watch):** {mv.REGIONS[mv.valid_region(cfg.get('region'))]}",
        f"**Opt-in role:** {role.mention if role else 'none'}",
    ])


def build_setup_view(guild: discord.Guild, cfg: dict, premium: bool, note: Optional[str] = None) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    box = discord.ui.Container(accent_colour=COLOR)
    box.add_item(discord.ui.TextDisplay(_config_text(guild, cfg, premium) + (f"\n-# {note}" if note else "")))
    box.add_item(discord.ui.Separator())
    box.add_item(discord.ui.ActionRow(MovieChannelSelect()))
    box.add_item(discord.ui.ActionRow(RegionSelect(mv.valid_region(cfg.get("region")))))
    box.add_item(discord.ui.ActionRow(MovieRoleSelect()))
    box.add_item(discord.ui.ActionRow(MakeChannelButton(), MakeRoleButton(), PostHubButton()))
    view.add_item(box)
    return view


async def _setup_ok(interaction: discord.Interaction) -> bool:
    """Manage Server AND Premium: the whole feature is Premium."""
    if not await _admin(interaction):
        return False
    return await _premium_gate(interaction, interaction.guild, _clone(interaction))


async def _save(interaction: discord.Interaction, note: Optional[str] = None, **fields):
    guild, clone_id = interaction.guild, _clone(interaction)
    cfg = await store.set_config(guild.id, clone_id, **fields)
    premium = await _is_premium(guild.id, clone_id)
    view = build_setup_view(guild, cfg, premium, note=note)
    if interaction.response.is_done():
        await interaction.edit_original_response(view=view)
    else:
        await interaction.response.edit_message(view=view)


class MovieChannelSelect(_Fixed, discord.ui.DynamicItem[discord.ui.ChannelSelect], template=r"^movie:channel$"):
    def __init__(self):
        super().__init__(discord.ui.ChannelSelect(custom_id="movie:channel", channel_types=[discord.ChannelType.text],
                                                  placeholder="Channel for the Movies hub"))

    async def callback(self, interaction: discord.Interaction):
        if not await _setup_ok(interaction):
            return
        ch = interaction.guild.get_channel(self.item.values[0].id)
        if not _can_post(ch, interaction.guild):
            return await interaction.response.send_message(
                f"I need **View Channel, Send Messages** and **Embed Links** in {ch.mention if ch else 'that channel'}.", ephemeral=True)
        await _save(interaction, channel_id=ch.id)


class RegionSelect(_Fixed, discord.ui.DynamicItem[discord.ui.Select], template=r"^movie:region$"):
    def __init__(self, current: str = mv.DEFAULT_REGION):
        super().__init__(discord.ui.Select(custom_id="movie:region", placeholder="Country for 'where to watch'", options=[
            discord.SelectOption(label=name, value=code, default=(code == current)) for code, name in mv.REGIONS.items()]))

    async def callback(self, interaction: discord.Interaction):
        if not await _setup_ok(interaction):
            return
        await _save(interaction, region=mv.valid_region(self.item.values[0]))


class MovieRoleSelect(_Fixed, discord.ui.DynamicItem[discord.ui.RoleSelect], template=r"^movie:role$"):
    def __init__(self):
        super().__init__(discord.ui.RoleSelect(custom_id="movie:role", placeholder="Opt-in role for movie fans (optional)",
                                               min_values=0, max_values=1))

    async def callback(self, interaction: discord.Interaction):
        if not await _setup_ok(interaction):
            return
        role = self.item.values[0] if self.item.values else None
        await _save(interaction, role_id=role.id if role else None)


class MakeChannelButton(_Fixed, discord.ui.DynamicItem[discord.ui.Button], template=r"^movie:mkchannel$"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="Create channel", emoji="➕", style=discord.ButtonStyle.secondary,
                                           custom_id="movie:mkchannel"))

    async def callback(self, interaction: discord.Interaction):
        if not await _setup_ok(interaction):
            return
        ch, err = await create_text_channel(interaction.guild, "movies", "Look up movies and series, trailers and where to watch legally.",
                                            near=interaction.channel)
        if err:
            return await interaction.response.send_message(err, ephemeral=True)
        await _save(interaction, note=f"Channel {ch.mention} is ready.", channel_id=ch.id)


class MakeRoleButton(_Fixed, discord.ui.DynamicItem[discord.ui.Button], template=r"^movie:mkrole$"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="Create role", emoji="➕", style=discord.ButtonStyle.secondary,
                                           custom_id="movie:mkrole"))

    async def callback(self, interaction: discord.Interaction):
        if not await _setup_ok(interaction):
            return
        role, err = await create_role(interaction.guild, "Movie Fans", discord.Color.red())
        if err:
            return await interaction.response.send_message(err, ephemeral=True)
        await _save(interaction, note=f"Role {role.mention} is ready. Members can opt in from the hub.", role_id=role.id)


class PostHubButton(_Fixed, discord.ui.DynamicItem[discord.ui.Button], template=r"^movie:posthub$"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="Post the hub", emoji="📌", style=discord.ButtonStyle.success,
                                           custom_id="movie:posthub"))

    async def callback(self, interaction: discord.Interaction):
        if not await _setup_ok(interaction):
            return
        cfg = await store.get_config(interaction.guild.id, _clone(interaction))
        ch = interaction.guild.get_channel(cfg["channel_id"]) if cfg.get("channel_id") else None
        if ch is None:
            return await interaction.response.send_message("Pick or create the hub channel first.", ephemeral=True)
        if not _can_post(ch, interaction.guild):
            return await interaction.response.send_message(f"I can't post in {ch.mention}. Give me **Send Messages** and **Embed Links** there.", ephemeral=True)
        role_ok = bool(cfg.get("role_id") and interaction.guild.get_role(cfg["role_id"]))
        try:
            await ch.send(view=build_hub_view(role_ok), allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException):
            return await interaction.response.send_message("I couldn't post there. Check my permissions in that channel.", ephemeral=True)
        await interaction.response.send_message(f"📌 The Movies hub is live in {ch.mention}.", ephemeral=True)


DYNAMIC_ITEMS = (SearchButton, TrendingButton, ClassicsButton, RoleToggleButton, MovieChannelSelect, RegionSelect,
                 MovieRoleSelect, MakeChannelButton, MakeRoleButton, PostHubButton)


class MoviesCog(commands.Cog):
    """No commands: the panel opens from More features. Loaded so the persistent items are registered with the bot."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot


async def setup(bot: commands.Bot):
    await bot.add_cog(MoviesCog(bot))
