"""Collection and Dex screens for the catch hub (Phase 3 slice).

Both open as ephemeral messages owned by the player who pressed the hub button.
Every callback defers first (rule B.6), then reads; the view only accepts clicks from
its owner, and favouriting re-validates ownership in SQL.
"""

from __future__ import annotations

import logging

import discord

from discord_bot.cogs._views_catch_creature import open_creature_detail
from discord_bot.cogs._views_catch_profile import open_profile
from modules import catch_emoji
from modules.catch_collection import (
    SORTS, dex_page, dex_summary, format_dex_line, format_owned_line,
    list_owned, load_dex, page_count, set_favorite,
)
from modules.catch_gate import check_player_allowed
from modules.catch_i18n import text
from modules.catch_theme import button_style, state_color

logger = logging.getLogger(__name__)

SORT_LABELS = {"recent": "Newest", "level": "Highest level", "rarity": "Rarest", "favorites": "Favourites first"}


def _collection_embed(rows, total: int, page: int, sort: str) -> discord.Embed:
    pages = page_count(total, 10)
    if not rows:
        return discord.Embed(title=text("collection.title"), description=text("collection.empty"), colour=state_color("info"))
    embed = discord.Embed(
        title=text("collection.title"),
        description="\n".join(format_owned_line(r) for r in rows),
        colour=state_color("info"),
    )
    embed.set_footer(text=text("collection.footer", page=page + 1, pages=pages, total=total, sort=SORT_LABELS[sort]))
    return embed


class CollectionView(discord.ui.View):
    def __init__(self, user_id: int, clone_id: int | None, *, page: int = 0, sort: str = "recent",
                 rows=(), total: int = 0):
        super().__init__(timeout=600)
        self.user_id, self.clone_id = user_id, clone_id
        self.page, self.sort, self.rows, self.total = page, sort if sort in SORTS else "recent", list(rows), total
        self._build()

    def _build(self) -> None:
        self.clear_items()
        sort = discord.ui.Select(
            placeholder=text("collection.sort"), row=0,
            options=[discord.SelectOption(label=label, value=key, default=key == self.sort) for key, label in SORT_LABELS.items()],
        )
        sort.callback = self._sort
        self.add_item(sort)
        if self.rows:
            fav = discord.ui.Select(
                placeholder=text("collection.favorite"), row=1,
                options=[
                    discord.SelectOption(
                        label=f"#{r.id} {r.nickname or r.name} (Lv.{r.level})"[:100], value=str(r.id),
                        description=("Remove from favourites" if r.favorite else "Add to favourites"),
                    )
                    for r in self.rows
                ],
            )
            fav.callback = self._favorite
            self.add_item(fav)
        pages = page_count(self.total, 10)
        prev_btn = discord.ui.Button(label=text("ui.prev"), style=button_style("navigation"), disabled=self.page <= 0, row=2)
        prev_btn.callback = self._prev
        self.add_item(prev_btn)
        next_btn = discord.ui.Button(label=text("ui.next"), style=button_style("navigation"), disabled=self.page >= pages - 1, row=2)
        next_btn.callback = self._next
        self.add_item(next_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(text("ui.not_yours"), ephemeral=True)
            return False
        return True

    async def _reload(self, interaction: discord.Interaction) -> None:
        self.rows, self.total, self.page = await list_owned(self.user_id, self.clone_id, page=self.page, sort=self.sort)
        self._build()
        await interaction.edit_original_response(embed=_collection_embed(self.rows, self.total, self.page, self.sort), view=self)

    async def _sort(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        chosen = (interaction.data or {}).get("values", ["recent"])[0]
        self.sort, self.page = (chosen if chosen in SORTS else "recent"), 0
        await self._reload(interaction)

    async def _prev(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        self.page -= 1
        await self._reload(interaction)

    async def _next(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        self.page += 1
        await self._reload(interaction)

    async def _favorite(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            owned_id = int((interaction.data or {}).get("values", [""])[0])
        except ValueError:
            await interaction.followup.send(text("collection.not_found"), ephemeral=True)
            return
        if await set_favorite(owned_id, self.user_id, self.clone_id) is None:
            await interaction.followup.send(text("collection.not_found"), ephemeral=True)
        await self._reload(interaction)


class CollectionBrowseView(CollectionView):
    """The Collection list plus Phase 3 controls: open a creature, and the trainer card.

    Added on top of ``CollectionView`` (children 0-3 are unchanged) so the list, sort,
    favourite toggle and paging behave exactly as before.
    """

    def _build(self) -> None:
        super()._build()
        profile_btn = discord.ui.Button(
            label=text("profile.button"), emoji=catch_emoji.mark("ui", "profile"),
            style=button_style("navigation"), row=2,
        )
        profile_btn.callback = self._profile
        self.add_item(profile_btn)
        if self.rows:
            pick = discord.ui.Select(
                placeholder=text("creature.pick"), row=3,
                options=[
                    discord.SelectOption(
                        label=f"#{r.id} {r.nickname or r.name} (Lv.{r.level})"[:100], value=str(r.id),
                        emoji=catch_emoji.mark("rarity", r.rarity),
                    )
                    for r in self.rows
                ],
            )
            pick.callback = self._open_creature
            self.add_item(pick)

    async def _back_to_list(self, interaction: discord.Interaction) -> None:
        """Redraw the list (used by Back on the creature and trainer card screens)."""
        try:
            self.rows, self.total, self.page = await list_owned(
                self.user_id, self.clone_id, page=self.page, sort=self.sort,
            )
        except Exception:
            logger.exception("Catch collection reload failed user=%s", self.user_id)
            await interaction.followup.send(text("collection.error"), ephemeral=True)
            return
        self._build()
        await interaction.edit_original_response(
            content=None, embed=_collection_embed(self.rows, self.total, self.page, self.sort), view=self,
        )

    async def _open_creature(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            owned_id = int((interaction.data or {}).get("values", [""])[0])
        except ValueError:
            await interaction.followup.send(text("collection.not_found"), ephemeral=True)
            return
        await open_creature_detail(interaction, self.user_id, self.clone_id, owned_id, back=self._back_to_list)

    async def _profile(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await open_profile(interaction, self.user_id, self.clone_id, back=self._back_to_list)


class DexView(discord.ui.View):
    def __init__(self, user_id: int, entries, *, page: int = 0):
        super().__init__(timeout=600)
        self.user_id, self.entries, self.page = user_id, list(entries), page
        self._build()

    def _build(self) -> None:
        self.clear_items()
        pages = page_count(len(self.entries), 12)
        prev_btn = discord.ui.Button(label=text("ui.prev"), style=button_style("navigation"), disabled=self.page <= 0)
        prev_btn.callback = self._prev
        self.add_item(prev_btn)
        next_btn = discord.ui.Button(label=text("ui.next"), style=button_style("navigation"), disabled=self.page >= pages - 1)
        next_btn.callback = self._next
        self.add_item(next_btn)

    def embed(self) -> discord.Embed:
        chunk, self.page = dex_page(self.entries, self.page)
        caught, seen, total = dex_summary(self.entries)
        embed = discord.Embed(
            title=text("dex.title"),
            description="\n".join(format_dex_line(e) for e in chunk) or text("dex.empty"),
            colour=state_color("info"),
        )
        embed.set_footer(text=text("dex.footer", caught=caught, seen=seen, total=total,
                                   page=self.page + 1, pages=page_count(len(self.entries), 12)))
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(text("ui.not_yours"), ephemeral=True)
            return False
        return True

    async def _turn(self, interaction: discord.Interaction, delta: int) -> None:
        self.page += delta
        embed = self.embed()
        self._build()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _prev(self, interaction: discord.Interaction) -> None:
        await self._turn(interaction, -1)

    async def _next(self, interaction: discord.Interaction) -> None:
        await self._turn(interaction, 1)


async def open_collection(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    clone_id = getattr(interaction.client, "clone_id", None)
    gate = await check_player_allowed(interaction.user.id, interaction.guild_id, "view", clone_id)
    if not gate.allowed:
        await interaction.followup.send(text("catch.unavailable", reason=gate.reason or "disabled"), ephemeral=True)
        return
    try:
        rows, total, page = await list_owned(interaction.user.id, clone_id)
    except Exception:
        logger.exception("Catch collection load failed user=%s", interaction.user.id)
        await interaction.followup.send(text("collection.error"), ephemeral=True)
        return
    view = CollectionBrowseView(interaction.user.id, clone_id, page=page, rows=rows, total=total)
    await interaction.followup.send(embed=_collection_embed(rows, total, page, "recent"), view=view, ephemeral=True)


async def open_dex(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    clone_id = getattr(interaction.client, "clone_id", None)
    gate = await check_player_allowed(interaction.user.id, interaction.guild_id, "view", clone_id)
    if not gate.allowed:
        await interaction.followup.send(text("catch.unavailable", reason=gate.reason or "disabled"), ephemeral=True)
        return
    try:
        entries = await load_dex(interaction.user.id, clone_id)
    except Exception:
        logger.exception("Catch dex load failed user=%s", interaction.user.id)
        await interaction.followup.send(text("dex.error"), ephemeral=True)
        return
    view = DexView(interaction.user.id, entries)
    await interaction.followup.send(embed=view.embed(), view=view, ephemeral=True)


__all__ = ["CollectionBrowseView", "CollectionView", "DexView", "open_collection", "open_dex"]
