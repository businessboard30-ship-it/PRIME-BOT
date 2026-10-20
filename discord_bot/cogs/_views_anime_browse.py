# path: discord_bot/cogs/_views_anime_browse.py

"""
Anime feed D3: Search and Categories on the posted card. Anyone can use them; everything is private (ephemeral). Every item is a
DynamicItem whose custom_id holds all its state (the category key, or the normalised search text and the page number), so a
browser opened before a restart still pages correctly: it simply re-reads the cache or asks AniList again. Search has a
per-person cooldown, the data module shares one request limiter with the posting worker, and results are cached.
"""

from __future__ import annotations

import logging
import re
import time
from typing import List, Optional

import discord

from modules import anime_feed as af
from modules import anime_feed_browse as br
from discord_bot.cogs import _views_anime_actions as aa
from discord_bot.cogs._views_anime_feed import ACCENT, OOPS, TEXT_LIMIT, _FollowButton, _MoreButton

logger = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()
STATE_TEXT = {
    "busy": "⏳ Lots of people are searching right now. Try again in a minute.",
    "down": "😿 I can't reach AniList right now. Try again in a few minutes.",
    "short": f"🔎 Type at least {br.MIN_QUERY} letters or numbers.",
    "unknown": "🚫 That category isn't available.",
}


def entry_text(e: br.Entry, index: int, total: int, heading: str) -> str:
    meta = " · ".join(x for x in (af.ORIGIN_LABELS.get(e.origin, ""), e.fmt, e.status, f"{e.episodes} episodes" if e.episodes else "", ", ".join(e.genres)) if x)
    lines = [f"## {e.title}"]
    if meta:
        lines.append(f"-# {meta}")
    if e.next_episode and e.next_at:
        lines.append(f"**Episode {e.next_episode}** airs <t:{e.next_at}:F> (<t:{e.next_at}:R>)")
    if e.synopsis:
        lines += ["", e.synopsis]
    lines += ["", f"-# {heading} · {index + 1} of {total} · Source: {af.SOURCE_NAME}"]
    return "\n".join(lines)[:TEXT_LIMIT]


class _PageButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^anime_pg:([sc]):(\d{1,2}):(.{1,60})$"):
    """Prev / Next in a browser. kind 's' = search results (key is the normalised search text), 'c' = a category (key is its key)."""

    def __init__(self, kind: str, page: int, key: str, label: str, emoji: str, disabled: bool = False):
        self.kind, self.page, self.key = kind, max(0, int(page)), key         # Prev on page 1 is disabled but still needs a valid id
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.secondary, disabled=disabled,
                                           custom_id=f"anime_pg:{kind}:{self.page}:{key}"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(match.group(1), int(match.group(2)), match.group(3), item.label or "", None)

    async def callback(self, interaction: discord.Interaction):
        wait = af.cooldown_left(interaction.user.id, "browse", br.BROWSE_COOLDOWN / 2)
        if wait > 0:
            await interaction.response.send_message(f"⏳ One moment, try again in {int(wait) + 1}s.", ephemeral=True)
            return
        try:
            await interaction.response.defer()                                   # edit the same private message
            entries, state = await (br.search(self.key) if self.kind == "s" else br.category(self.key))
            await interaction.edit_original_response(view=browse_view(entries, self.page, self.kind, self.key, state), allowed_mentions=NO_PINGS)
        except Exception:
            logger.exception("[anime-feed] page failed")
            await aa._answer_error(interaction)


class _SearchButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^anime_search$"):
    """Search: opens a one-field modal. Results are private. Anyone can use it."""

    def __init__(self):
        super().__init__(discord.ui.Button(label="Search", style=discord.ButtonStyle.secondary, emoji="🔎", custom_id="anime_search"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.send_modal(SearchModal())                 # opening the modal is free; the cooldown applies on submit
        except Exception:
            logger.exception("[anime-feed] search modal failed")
            await aa._answer_error(interaction)


class SearchModal(discord.ui.Modal, title="Search anime"):
    # A modal is shown once and answered once (Discord keeps no state for it), so it needs no persistence.
    query = discord.ui.TextInput(label="Title", placeholder="For example: Frieren, Soul Land, Naruto", min_length=br.MIN_QUERY, max_length=br.MAX_QUERY, required=True)

    async def on_submit(self, interaction: discord.Interaction):
        wait = af.cooldown_left(interaction.user.id, "search", br.SEARCH_COOLDOWN)
        if wait > 0:
            await interaction.response.send_message(f"⏳ You can search again in {int(wait) + 1}s.", ephemeral=True)
            return
        q = br.normalise_query(self.query.value)
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)     # AniList may be slow: answer inside Discord's 3 seconds first
            entries, state = await br.search(q)
            await interaction.followup.send(view=browse_view(entries, 0, "s", q, state), ephemeral=True, allowed_mentions=NO_PINGS)
        except Exception:
            logger.exception("[anime-feed] search failed")
            await aa._answer_error(interaction)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.exception("[anime-feed] search modal error", exc_info=error)
        await aa._answer_error(interaction)


class _CategoriesButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^anime_cats$"):
    """Categories: opens a private picker (Love, Sci-Fi, Donghua, ...). Anyone can use it."""

    def __init__(self):
        super().__init__(discord.ui.Button(label="Categories", style=discord.ButtonStyle.secondary, emoji="🗂️", custom_id="anime_cats"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.send_message(view=picker_view(), ephemeral=True, allowed_mentions=NO_PINGS)
        except Exception:
            logger.exception("[anime-feed] categories failed")
            await aa._answer_error(interaction)


class _CatSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"^anime_catsel$"):
    def __init__(self, current: str = ""):
        opts = [discord.SelectOption(label=c.label, value=c.key, emoji=c.emoji, default=(c.key == current)) for c in br.CATEGORIES]
        super().__init__(discord.ui.Select(placeholder="Pick a category", options=opts, min_values=1, max_values=1, custom_id="anime_catsel"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        key = (interaction.data.get("values") or [""])[0]
        if br.get_category(key) is None:                                          # never trust the value: only keys from the list
            await interaction.response.send_message(STATE_TEXT["unknown"], ephemeral=True)
            return
        wait = af.cooldown_left(interaction.user.id, "browse", br.BROWSE_COOLDOWN)
        if wait > 0:
            await interaction.response.send_message(f"⏳ One moment, try again in {int(wait) + 1}s.", ephemeral=True)
            return
        try:
            await interaction.response.defer()
            entries, state = await br.category(key)
            await interaction.edit_original_response(view=browse_view(entries, 0, "c", key, state), allowed_mentions=NO_PINGS)
        except Exception:
            logger.exception("[anime-feed] category failed")
            await aa._answer_error(interaction)


def picker_view() -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    box = discord.ui.Container(accent_colour=ACCENT)
    box.add_item(discord.ui.TextDisplay("## 🗂️ Categories\nPick one to browse what's airing. Adult titles are never shown.\n-# Source: AniList"))
    box.add_item(discord.ui.ActionRow(_CatSelect()))
    view.add_item(box)
    return view


def browse_view(entries: List[br.Entry], page: int, kind: str, key: str, state: str = "ok") -> discord.ui.LayoutView:
    """One big card at a time with Prev/Next. Never raises: a bad page number is clamped, and an empty or failed result says so plainly."""
    view = discord.ui.LayoutView(timeout=None)
    cat = br.get_category(key) if kind == "c" else None
    heading = f"{cat.emoji} {cat.label}" if cat else f"Search: {key}"
    box = discord.ui.Container(accent_colour=ACCENT)
    if not entries:
        msg = STATE_TEXT.get(state) or (f"🔎 No anime found for “{key}”. Try another spelling or fewer words." if kind == "s"
                                        else f"{heading}: nothing to show right now. Try another category.")
        box.add_item(discord.ui.TextDisplay(msg[:TEXT_LIMIT]))
        if kind == "c":
            box.add_item(discord.ui.ActionRow(_CatSelect(key)))
        view.add_item(box)
        return view
    page = max(0, min(int(page), len(entries) - 1))
    e = entries[page]
    if e.color:
        box = discord.ui.Container(accent_colour=discord.Color(e.color))
    if e.image:
        gallery = discord.ui.MediaGallery()
        gallery.add_item(media=e.image, description=e.title[:200])
        box.add_item(gallery)
    box.add_item(discord.ui.TextDisplay(entry_text(e, page, len(entries), heading)))
    box.add_item(discord.ui.ActionRow(_MoreButton(e.anime_id), _FollowButton(e.anime_id), aa._AlertButton(e.anime_id),
                                      discord.ui.Button(label="Details on AniList", style=discord.ButtonStyle.link, url=e.url)))
    counter = discord.ui.Button(label=f"{page + 1}/{len(entries)}", style=discord.ButtonStyle.secondary, disabled=True)
    box.add_item(discord.ui.ActionRow(_PageButton(kind, page - 1, key, "Prev", "◀️", disabled=page <= 0), counter,
                                      _PageButton(kind, page + 1, key, "Next", "▶️", disabled=page >= len(entries) - 1)))
    if kind == "c":
        box.add_item(discord.ui.ActionRow(_CatSelect(key)))
    view.add_item(box)
    return view


BROWSE_ITEMS = (_SearchButton, _CategoriesButton, _CatSelect, _PageButton)
