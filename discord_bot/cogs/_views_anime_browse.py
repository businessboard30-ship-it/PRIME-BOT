# path: discord_bot/cogs/_views_anime_browse.py

"""
Anime feed, slice D3: the Search and Categories buttons and their private (ephemeral) browsers.

Persistence: every button here is a DynamicItem whose whole state lives in its custom_id (`anime_search`, `anime_cats`,
`anime_catsel`, `anime_nav:<s|c>:<query-or-category>:<sort>:<index>`, `anime_sort:<category>:<sort>`), and each page is re-queried
from the shared cache, so a Next button on a browser opened before a restart still works. Anyone can use these (replies are private).
Imported only by `_views_anime_feed` (which registers DYNAMIC_ITEMS); never import this module first.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional

import discord

from discord_bot.cogs import _views_anime_feed as vf
from modules import anime_browse as ab
from modules import anime_feed as af

logger = logging.getLogger(__name__)

SEARCH_COOLDOWN = 10.0
CATEGORY_COOLDOWN = 3.0
NAV_COOLDOWN = 0.7


# ── pure card text / view ───────────────────────────────────────────────

def entry_text(e: ab.Entry, index: int = 0, total: int = 0) -> str:
    meta = " · ".join(x for x in (af.ORIGIN_LABELS.get(e.origin, ""), e.fmt, e.status, f"{e.episodes} episodes" if e.episodes else "",
                                  ", ".join(e.genres)) if x)
    lines = [f"## {e.title}"]
    if meta:
        lines.append(f"-# {meta}")
    if e.next_episode and e.next_at:
        lines.append(f"**Episode {e.next_episode}** airs <t:{e.next_at}:F> (<t:{e.next_at}:R>)")
    if e.synopsis:
        lines += ["", e.synopsis]
    lines += ["", f"-# Source: {af.SOURCE_NAME}" + (f" · result {index + 1} of {total}" if total else "")]
    return "\n".join(lines)[:vf.TEXT_LIMIT]


def _custom(kind: str, arg: str, sort: int, index: int) -> str:
    return f"anime_nav:{kind}:{arg}:{int(sort)}:{int(index)}"


def browse_view(entries: List[ab.Entry], index: int, kind: str, arg: str, sort: int = ab.POPULAR, heading: str = "") -> discord.ui.LayoutView:
    """One result as a big card: the usual buttons plus Prev/Next (and Sort for categories). Never raises on an empty list."""
    view = discord.ui.LayoutView(timeout=None)
    if not entries:
        view.add_item(vf._info_box((heading + "\n\n" if heading else "") + "😿 Nothing found. Try another word, or pick a category from the card's Categories button."))
        return view
    index = max(0, min(int(index), len(entries) - 1))
    e = entries[index]
    box = discord.ui.Container(accent_colour=discord.Color(e.color) if e.color else vf.ACCENT)
    if heading:
        box.add_item(discord.ui.TextDisplay(heading[:300]))
    if e.image:
        gallery = discord.ui.MediaGallery()
        gallery.add_item(media=e.image, description=e.title[:200])
        box.add_item(gallery)
    box.add_item(discord.ui.TextDisplay(entry_text(e, index, len(entries))))
    box.add_item(discord.ui.ActionRow(vf._MoreButton(e.anime_id), vf._FollowButton(e.anime_id), vf._AlertButton(e.anime_id),
                                      discord.ui.Button(label="Details on AniList", style=discord.ButtonStyle.link, url=e.url)))
    nav = [_NavButton(kind, arg, sort, index - 1, "Prev", "◀️", disabled=index <= 0),
           discord.ui.Button(label=f"{index + 1}/{len(entries)}", style=discord.ButtonStyle.secondary, disabled=True),
           _NavButton(kind, arg, sort, index + 1, "Next", "▶️", disabled=index >= len(entries) - 1)]
    if kind == "c":
        nav.append(_SortButton(arg, ab.POPULAR if sort == ab.TOP else ab.TOP))
    box.add_item(discord.ui.ActionRow(*nav))
    view.add_item(box)
    return view


def _heading_for(kind: str, arg: str, sort: int) -> str:
    if kind == "s":
        return f"🔭 Results for **{ab.decode_query(arg)}**"[:300]
    c = ab.get_category(arg)
    return f"{c.emoji} **{c.label}**" + (" · top picks" if sort == ab.TOP else " · popular") if c else ""


async def _entries_for(kind: str, arg: str, sort: int):
    return await (ab.search(ab.decode_query(arg)) if kind == "s" else ab.category(arg, sort))


def _busy_text() -> str:
    return "😿 I can't reach AniList right now (or lots of people are browsing). Try again in a minute."


# ── nav buttons (state in the custom_id; rebuilt after any restart) ─────

class _NavButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^anime_nav:([sc]):([A-Za-z0-9_\-=]{1,80}):([01]):(\d{1,3})$"):
    def __init__(self, kind: str, arg: str, sort: int, index: int, label: str = "Next", emoji: Optional[str] = None, disabled: bool = False):
        self.kind, self.arg, self.sort, self.index = kind, arg, int(sort), max(0, int(index))
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.primary, disabled=disabled,
                                           custom_id=_custom(kind, arg, self.sort, self.index)))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(match.group(1), match.group(2), int(match.group(3)), int(match.group(4)))

    async def callback(self, interaction: discord.Interaction):
        await _turn_page(interaction, self.kind, self.arg, self.sort, self.index)


class _SortButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^anime_sort:([a-z0-9_]{1,20}):([01])$"):
    """Switch a category between Popular (free) and Top picks (an Anime+ extra). The id holds the sort to switch TO."""

    def __init__(self, key: str, to_sort: int):
        self.key, self.to_sort = key, int(to_sort)
        super().__init__(discord.ui.Button(label="Top picks" if self.to_sort == ab.TOP else "Popular", emoji="🏅" if self.to_sort == ab.TOP else "🔥",
                                           style=discord.ButtonStyle.secondary, custom_id=f"anime_sort:{key}:{self.to_sort}"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(match.group(1), int(match.group(2)))

    async def callback(self, interaction: discord.Interaction):
        if self.to_sort == ab.TOP:
            try:
                allowed = await af.has_anime_plus(interaction.user.id, interaction.guild.id if interaction.guild else None)
            except Exception:
                allowed = False
            if not allowed:
                await interaction.response.send_message("🔒 Top picks is part of Anime+. Popular is free.", ephemeral=True)
                return
        await _turn_page(interaction, "c", self.key, self.to_sort, 0)


async def _turn_page(interaction: discord.Interaction, kind: str, arg: str, sort: int, index: int) -> None:
    wait = af.cooldown_left(interaction.user.id, "nav", NAV_COOLDOWN)
    if wait > 0:
        await interaction.response.send_message("⏳ Easy there, one page at a time.", ephemeral=True)
        return
    try:
        await interaction.response.defer()                                  # 3-second rule first, then edit this same private message
        entries, ok = await _entries_for(kind, arg, sort)
        if not ok:
            await interaction.followup.send(_busy_text(), ephemeral=True)
            return
        await interaction.edit_original_response(view=browse_view(entries, index, kind, arg, sort, _heading_for(kind, arg, sort)),
                                                 allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        logger.exception("[anime-feed] couldn't turn the page")
        try:
            await interaction.followup.send(vf.OOPS, ephemeral=True)
        except discord.HTTPException:
            pass


async def _open_browser(interaction: discord.Interaction, kind: str, arg: str, sort: int) -> None:
    """The interaction must already be deferred (ephemeral). Sends the first page as a new private message."""
    entries, ok = await _entries_for(kind, arg, sort)
    if not ok:
        await interaction.followup.send(_busy_text(), ephemeral=True)
        return
    await interaction.followup.send(view=browse_view(entries, 0, kind, arg, sort, _heading_for(kind, arg, sort)), ephemeral=True,
                                    allowed_mentions=discord.AllowedMentions.none())


# ── Search ──────────────────────────────────────────────────────────────

class SearchModal(discord.ui.Modal, title="Search anime"):
    query = discord.ui.TextInput(label="Title", placeholder="For example: Frieren", min_length=2, max_length=ab.QUERY_MAX_BYTES)

    async def on_submit(self, interaction: discord.Interaction):
        q = ab.normalize_query(str(self.query.value))
        if len(q) < 2:
            await interaction.response.send_message("🔭 Type at least 2 characters.", ephemeral=True)
            return
        wait = af.cooldown_left(interaction.user.id, "search", SEARCH_COOLDOWN)
        if wait > 0:
            await interaction.response.send_message(f"⏳ Searches are limited. Try again in {int(wait) + 1}s.", ephemeral=True)
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await _open_browser(interaction, "s", ab.encode_query(q), ab.POPULAR)
        except Exception:
            logger.exception("[anime-feed] search failed")
            try:
                await interaction.followup.send(vf.OOPS, ephemeral=True)
            except discord.HTTPException:
                pass


class SearchButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^anime_search$"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="Search", style=discord.ButtonStyle.secondary, emoji="🔭", custom_id="anime_search"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SearchModal())               # a modal must be the first response


# ── Categories ──────────────────────────────────────────────────────────

def _category_options() -> List[discord.SelectOption]:
    return [discord.SelectOption(label=c.label, value=c.key, emoji=c.emoji) for c in ab.CATEGORIES][:25]


class CategorySelect(discord.ui.DynamicItem[discord.ui.Select], template=r"^anime_catsel$"):
    def __init__(self):
        super().__init__(discord.ui.Select(placeholder="Pick a category", min_values=1, max_values=1, options=_category_options(), custom_id="anime_catsel"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        key = (interaction.data.get("values") or [""])[0]
        if ab.get_category(key) is None:
            await interaction.response.send_message("🧭 That category isn't available any more. Open Categories again.", ephemeral=True)
            return
        wait = af.cooldown_left(interaction.user.id, "cat", CATEGORY_COOLDOWN)
        if wait > 0:
            await interaction.response.send_message(f"⏳ One moment, try again in {int(wait) + 1}s.", ephemeral=True)
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await _open_browser(interaction, "c", key, ab.POPULAR)
        except Exception:
            logger.exception("[anime-feed] category failed")
            try:
                await interaction.followup.send(vf.OOPS, ephemeral=True)
            except discord.HTTPException:
                pass


class CategoriesButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^anime_cats$"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="Categories", style=discord.ButtonStyle.secondary, emoji="🧭", custom_id="anime_cats"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(vf._info_box("## 🧭 Categories\nPick one to browse what's airing now. Donghua and Cultivation / Xianxia are Chinese animation.",
                                   [discord.ui.ActionRow(CategorySelect())]))
        await interaction.response.send_message(view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


DYNAMIC_ITEMS = (SearchButton, CategoriesButton, CategorySelect, _NavButton, _SortButton)
