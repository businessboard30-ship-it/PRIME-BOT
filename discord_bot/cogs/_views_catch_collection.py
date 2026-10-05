"""Collection and Dex screens for the catch hub (Phase 3 slice).

Both open as ephemeral messages owned by the player who pressed the hub button.
Every callback defers first (rule B.6), then reads; the view only accepts clicks from
its owner, and favouriting re-validates ownership in SQL.
"""

from __future__ import annotations

import logging

import discord

from discord_bot.cogs._views_catch_creature import open_creature_detail, stat_lines
from discord_bot.cogs._views_catch_profile import open_profile
from modules import catch_emoji
from modules.catch_collection import (
    SEARCH_MAX, SORTS, CollectionFilter, dex_page, dex_summary, filter_summary, format_dex_line,
    format_owned_line, list_owned, load_dex, page_count, set_favorite,
)
from modules.catch_dex import SpeciesInfo, rarity_completion, species_info
from modules.catch_game import ELEMENTS, RARITIES
from modules.catch_gate import check_player_allowed
from modules.catch_i18n import text
from modules.catch_theme import button_style, rarity_color, state_color

logger = logging.getLogger(__name__)

SORT_LABELS = {"recent": "Newest", "level": "Highest level", "rarity": "Rarest", "favorites": "Favourites first"}


def _collection_embed(rows, total: int, page: int, sort: str, flt: CollectionFilter | None = None) -> discord.Embed:
    pages = page_count(total, 10)
    summary = filter_summary(flt)
    if not rows:
        empty = text("collection.filtered_empty", filters=summary) if summary else text("collection.empty")
        return discord.Embed(title=text("collection.title"), description=empty, colour=state_color("info"))
    embed = discord.Embed(
        title=text("collection.title"),
        description="\n".join(format_owned_line(r) for r in rows),
        colour=state_color("info"),
    )
    if summary:
        embed.set_footer(text=text("collection.footer_filtered", page=page + 1, pages=pages, total=total,
                                   sort=SORT_LABELS[sort], filters=summary))
    else:
        embed.set_footer(text=text("collection.footer", page=page + 1, pages=pages, total=total, sort=SORT_LABELS[sort]))
    return embed


class CollectionView(discord.ui.View):
    def __init__(self, user_id: int, clone_id: int | None, *, page: int = 0, sort: str = "recent",
                 rows=(), total: int = 0, flt: CollectionFilter | None = None):
        super().__init__(timeout=600)
        self.user_id, self.clone_id = user_id, clone_id
        self.flt = (flt or CollectionFilter()).clean()
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
        self.rows, self.total, self.page = await list_owned(
            self.user_id, self.clone_id, page=self.page, sort=self.sort, flt=self.flt,
        )
        self._build()
        await interaction.edit_original_response(
            embed=_collection_embed(self.rows, self.total, self.page, self.sort, self.flt), view=self,
        )

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
                self.user_id, self.clone_id, page=self.page, sort=self.sort, flt=self.flt,
            )
        except Exception:
            logger.exception("Catch collection reload failed user=%s", self.user_id)
            await interaction.followup.send(text("collection.error"), ephemeral=True)
            return
        self._build()
        await interaction.edit_original_response(
            content=None, embed=_collection_embed(self.rows, self.total, self.page, self.sort, self.flt), view=self,
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


class CollectionBoxView(CollectionBrowseView):
    """Browse view plus the box controls: Filter screen and Go to page.

    Added on top of ``CollectionBrowseView`` (its children are unchanged); the two new
    buttons sit on row 2 beside Prev, Next and Trainer card.
    """

    def _build(self) -> None:
        super()._build()
        filter_btn = discord.ui.Button(
            label=text("collection.filter.button"), emoji=catch_emoji.mark("ui", "filter"),
            style=button_style("navigation"), row=2,
        )
        filter_btn.callback = self._open_filters
        self.add_item(filter_btn)
        jump_btn = discord.ui.Button(
            label=text("collection.jump.button"), emoji=catch_emoji.mark("ui", "jump"),
            style=button_style("navigation"), row=2, disabled=page_count(self.total, 10) <= 1,
        )
        jump_btn.callback = self._open_jump
        self.add_item(jump_btn)

    async def _open_filters(self, interaction: discord.Interaction) -> None:
        # Pure UI state: no database work, so the response is the screen itself.
        screen = CollectionFilterView(self)
        await interaction.response.edit_message(content=None, embed=screen.embed(), view=screen)

    async def _open_jump(self, interaction: discord.Interaction) -> None:
        # A modal is itself the interaction response; it does no database work here.
        await interaction.response.send_modal(PageJumpModal(self))

    async def apply_filter(self, interaction: discord.Interaction, flt: CollectionFilter) -> None:
        """Adopt ``flt``, go back to page 1 and redraw the list. The caller has already responded."""
        self.flt, self.page = flt.clean(), 0
        await self._back_to_list(interaction)


class CollectionFilterView(discord.ui.View):
    """Rarity, element, shiny, favourite and search; Show results returns to the list."""

    def __init__(self, box: CollectionBoxView):
        super().__init__(timeout=600)
        self.box, self.user_id = box, box.user_id
        self.flt = box.flt
        self._busy = False
        self._build()

    def embed(self) -> discord.Embed:
        summary = filter_summary(self.flt) or text("collection.filter.none")
        return discord.Embed(
            title=text("collection.filter.title"), description=text("collection.filter.current", filters=summary),
            colour=state_color("info"),
        )

    def _build(self) -> None:
        self.clear_items()
        any_label = text("collection.filter.any")
        rarity = discord.ui.Select(
            placeholder=text("collection.filter.rarity"), row=0,
            options=[discord.SelectOption(label=any_label, value="any", default=self.flt.rarity is None)] + [
                discord.SelectOption(label=r.key.title(), value=r.key, emoji=catch_emoji.mark("rarity", r.key),
                                     default=self.flt.rarity == r.key)
                for r in RARITIES
            ],
        )
        rarity.callback = self._rarity
        self.add_item(rarity)
        element = discord.ui.Select(
            placeholder=text("collection.filter.element"), row=1,
            options=[discord.SelectOption(label=any_label, value="any", default=self.flt.element is None)] + [
                discord.SelectOption(label=e.title(), value=e, emoji=catch_emoji.mark("element", e),
                                     default=self.flt.element == e)
                for e in ELEMENTS
            ],
        )
        element.callback = self._element
        self.add_item(element)
        shiny = discord.ui.Button(
            label=text("collection.filter.shiny"), emoji=catch_emoji.mark("flag", "shiny"), row=2,
            style=button_style("toggle_on" if self.flt.shiny else "toggle_off"),
        )
        shiny.callback = self._shiny
        self.add_item(shiny)
        fav = discord.ui.Button(
            label=text("collection.filter.favorite"), emoji=catch_emoji.mark("flag", "favorite"), row=2,
            style=button_style("toggle_on" if self.flt.favorite else "toggle_off"),
        )
        fav.callback = self._favorite
        self.add_item(fav)
        search = discord.ui.Button(
            label=text("collection.filter.search"), emoji=catch_emoji.mark("ui", "filter"), row=2,
            style=button_style("toggle_on" if self.flt.search else "toggle_off"),
        )
        search.callback = self._search
        self.add_item(search)
        clear = discord.ui.Button(
            label=text("collection.filter.clear"), emoji=catch_emoji.mark("ui", "clear"), row=3,
            style=button_style("navigation"), disabled=not self.flt.active,
        )
        clear.callback = self._clear
        self.add_item(clear)
        show = discord.ui.Button(label=text("collection.filter.apply"), emoji=catch_emoji.mark("ui", "ok"),
                                 row=3, style=button_style("main"))
        show.callback = self._apply
        self.add_item(show)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(text("ui.not_yours"), ephemeral=True)
            return False
        return True

    async def _redraw(self, interaction: discord.Interaction) -> None:
        self._build()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @staticmethod
    def _picked(interaction: discord.Interaction) -> str | None:
        value = ((interaction.data or {}).get("values") or ["any"])[0]
        return None if value == "any" else str(value)

    async def _rarity(self, interaction: discord.Interaction) -> None:
        self.flt = CollectionFilter(self._picked(interaction), self.flt.element, self.flt.shiny,
                                    self.flt.favorite, self.flt.search).clean()
        await self._redraw(interaction)

    async def _element(self, interaction: discord.Interaction) -> None:
        self.flt = CollectionFilter(self.flt.rarity, self._picked(interaction), self.flt.shiny,
                                    self.flt.favorite, self.flt.search).clean()
        await self._redraw(interaction)

    async def _shiny(self, interaction: discord.Interaction) -> None:
        self.flt = CollectionFilter(self.flt.rarity, self.flt.element, not self.flt.shiny,
                                    self.flt.favorite, self.flt.search).clean()
        await self._redraw(interaction)

    async def _favorite(self, interaction: discord.Interaction) -> None:
        self.flt = CollectionFilter(self.flt.rarity, self.flt.element, self.flt.shiny,
                                    not self.flt.favorite, self.flt.search).clean()
        await self._redraw(interaction)

    async def _clear(self, interaction: discord.Interaction) -> None:
        self.flt = CollectionFilter()
        await self._redraw(interaction)

    async def _search(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(SearchModal(self))

    async def _apply(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if self._busy:
            await interaction.followup.send(text("collection.busy"), ephemeral=True)
            return
        self._busy = True
        try:
            gate = await check_player_allowed(interaction.user.id, interaction.guild_id, "view", self.box.clone_id)
            if not gate.allowed:
                await interaction.followup.send(
                    text("catch.unavailable", reason=gate.reason or "disabled"), ephemeral=True)
                return
            await self.box.apply_filter(interaction, self.flt)
        except Exception:
            logger.exception("Catch collection filter failed user=%s", self.user_id)
            await interaction.followup.send(text("collection.error"), ephemeral=True)
        finally:
            self._busy = False


class SearchModal(discord.ui.Modal):
    def __init__(self, screen: CollectionFilterView):
        super().__init__(title=text("collection.filter.search_title"), timeout=300)
        self.screen = screen
        self.term = discord.ui.TextInput(
            label=text("collection.filter.search_label"),
            placeholder=text("collection.filter.search_placeholder"),
            default=screen.flt.search or None, required=False, max_length=SEARCH_MAX,
        )
        self.add_item(self.term)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        screen = self.screen
        screen.flt = CollectionFilter(screen.flt.rarity, screen.flt.element, screen.flt.shiny,
                                      screen.flt.favorite, str(self.term.value or "")).clean()
        screen._build()
        await interaction.edit_original_response(embed=screen.embed(), view=screen)


class PageJumpModal(discord.ui.Modal):
    def __init__(self, box: CollectionBoxView):
        super().__init__(title=text("collection.jump.title"), timeout=300)
        self.box = box
        pages = page_count(box.total, 10)
        self.number = discord.ui.TextInput(
            label=text("collection.jump.label", pages=pages), required=True, max_length=6,
            placeholder=str(box.page + 1),
        )
        self.add_item(self.number)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        box = self.box
        try:
            wanted = int(str(self.number.value).strip())
        except ValueError:
            await interaction.followup.send(
                text("collection.jump.invalid", pages=page_count(box.total, 10)), ephemeral=True)
            return
        try:
            gate = await check_player_allowed(interaction.user.id, interaction.guild_id, "view", box.clone_id)
            if not gate.allowed:
                await interaction.followup.send(
                    text("catch.unavailable", reason=gate.reason or "disabled"), ephemeral=True)
                return
            box.page = max(0, wanted - 1)  # list_owned clamps it into range
            await box._back_to_list(interaction)
        except Exception:
            logger.exception("Catch collection page jump failed user=%s", box.user_id)
            await interaction.followup.send(text("collection.error"), ephemeral=True)


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


def species_embed(info: SpeciesInfo) -> discord.Embed:
    """The species info page. Only what the player has discovered is shown."""
    mark = catch_emoji.mark("rarity", info.rarity)
    elements = " / ".join(
        f"{catch_emoji.mark('element', e)} {e.title()}" for e in (info.element, info.element2) if e
    )
    embed = discord.Embed(
        title=text("dex.info.title", number=f"{info.species_id:03d}", name=info.name),
        colour=rarity_color(info.rarity),
    )
    embed.add_field(name=text("dex.info.rarity"), value=f"{mark} {info.rarity.title()}", inline=True)
    embed.add_field(name=text("dex.info.types"), value=elements, inline=True)
    if not info.caught:
        embed.description = text("dex.info.locked")
        return embed
    if info.habitat:
        embed.add_field(name=text("dex.info.habitat"), value=info.habitat.title(), inline=True)
    shiny = text("dex.info.shiny", shiny=info.shiny_caught) if info.shiny_caught else ""
    embed.add_field(name=text("dex.info.record"), value=text("dex.info.caught_count", count=info.caught_count) + shiny, inline=False)
    if info.stats:
        embed.add_field(name=text("dex.info.stats"), value=stat_lines(info.stats), inline=False)
    lines = []
    if info.evolves_from:
        lines.append(text("dex.info.evo_from", name=info.evolves_from))
    if info.evolves_to is None:
        lines.append(text("dex.info.evo_final"))
    elif info.evolve_item:
        lines.append(text("dex.info.evo_item", to=info.evolves_to, item=info.evolve_item.replace("_", " ").title()))
    elif info.evolve_level:
        lines.append(text("dex.info.evo_level", to=info.evolves_to, level=info.evolve_level))
    else:
        lines.append(text("dex.info.evo_other", to=info.evolves_to))
    embed.add_field(name=text("dex.info.evolution"), value="\n".join(lines), inline=False)
    return embed


class DexBrowseView(DexView):
    """The Dex plus per-rarity completion and an \"Open a species\" select.

    Added on top of ``DexView`` (its Prev/Next buttons and paging are unchanged). The
    species page needs no database work: it is built from the entries already loaded.
    """

    def _build(self) -> None:
        super()._build()
        chunk, _ = dex_page(self.entries, self.page)
        known = [e for e in chunk if e.seen or e.caught]
        if known:
            pick = discord.ui.Select(
                placeholder=text("dex.pick"), row=1,
                options=[
                    discord.SelectOption(
                        label=f"{e.species_id:03d} {e.name}"[:100], value=str(e.species_id),
                        emoji=catch_emoji.mark("rarity", e.rarity),
                    )
                    for e in known
                ],
            )
            pick.callback = self._open_species
            self.add_item(pick)

    def embed(self) -> discord.Embed:
        embed = super().embed()
        lines = [
            text("dex.completion_line", mark=catch_emoji.mark("rarity", p.rarity), rarity=p.rarity.title(),
                 caught=p.caught, total=p.total, bar=catch_emoji.bar(p.caught, p.total))
            for p in rarity_completion(self.entries)
        ]
        if lines:
            embed.add_field(name=text("dex.completion"), value="\n".join(lines), inline=False)
        return embed

    async def _open_species(self, interaction: discord.Interaction) -> None:
        try:
            species_id = int(((interaction.data or {}).get("values") or [""])[0])
        except ValueError:
            species_id = 0
        info = species_info(self.entries, species_id)
        if info is None:
            await interaction.response.send_message(text("dex.info.not_found"), ephemeral=True)
            return
        screen = DexInfoView(self)
        await interaction.response.edit_message(embed=species_embed(info), view=screen)


class DexInfoView(discord.ui.View):
    def __init__(self, dex: DexBrowseView):
        super().__init__(timeout=600)
        self.dex, self.user_id = dex, dex.user_id
        back = discord.ui.Button(label=text("dex.info.back"), emoji=catch_emoji.mark("ui", "back"),
                                 style=button_style("navigation"))
        back.callback = self._back
        self.add_item(back)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(text("ui.not_yours"), ephemeral=True)
            return False
        return True

    async def _back(self, interaction: discord.Interaction) -> None:
        embed = self.dex.embed()
        self.dex._build()
        await interaction.response.edit_message(embed=embed, view=self.dex)


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
    view = CollectionBoxView(interaction.user.id, clone_id, page=page, rows=rows, total=total)
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
    view = DexBrowseView(interaction.user.id, entries)
    await interaction.followup.send(embed=view.embed(), view=view, ephemeral=True)


__all__ = [
    "CollectionBoxView", "CollectionBrowseView", "CollectionFilterView", "CollectionView", "DexBrowseView",
    "DexInfoView", "DexView", "species_embed",
    "PageJumpModal", "SearchModal", "open_collection", "open_dex",
]
