"""Sell screen for the catch hub.

Opens as an ephemeral message owned by the player who pressed the hub button. Every
callback defers first (rule B.6), then checks the gate and talks to the DB. The creature id
in the select is only a hint: ``sell_creature`` re-validates ownership, favourite and lock
state in SQL under a row lock, and the price always comes from the service. Selling needs
a confirm press, and a second click while a sale is running is ignored.
"""

from __future__ import annotations

import logging

import discord

from modules.catch_gate import check_player_allowed
from modules.catch_coin_card import coin_art, edit_kwargs, send_kwargs
from modules.catch_i18n import text
from modules.catch_items import load_player
from modules.catch_sell import RARITY_MARK, SELL_PAGE_SIZE, SellRow, display_name, list_sellable, sell_creature
from modules.catch_theme import button_style, state_color

logger = logging.getLogger(__name__)


def sell_embed(coins: int, rows: list[SellRow], page: int, total: int, per_page: int, selected: SellRow | None = None,
               notice: str | None = None) -> discord.Embed:
    if rows:
        lines = [
            f"{'▶ ' if selected and row.id == selected.id else ''}{RARITY_MARK.get(row.rarity, '⚪')} "
            f"**{display_name(row)}** · Lv {row.level} · {text('sell.value', value=row.value)}"
            for row in rows
        ]
        body = text("sell.description") + "\n\n" + "\n".join(lines)
    else:
        body = text("sell.empty")
    if notice:
        body = notice + "\n\n" + body
    embed = discord.Embed(title=text("sell.title"), description=body, colour=state_color("info"))
    embed.add_field(name=text("sell.balance"), value=f"{coins:,}", inline=True)
    pages = max(1, -(-total // per_page))
    embed.set_footer(text=text("sell.page", page=page + 1, pages=pages, total=total))
    return embed


class SellView(discord.ui.View):
    def __init__(self, user_id: int, clone_id: int | None, coins: int, rows: list[SellRow], total: int, page: int = 0):
        super().__init__(timeout=600)
        self.user_id, self.clone_id, self.coins = user_id, clone_id, coins
        self.rows, self.total, self.page = list(rows), total, page
        self.selected: SellRow | None = None
        self.notice: str | None = None
        self._busy = False
        self._build()

    def _build(self) -> None:
        self.clear_items()
        if self.rows:
            select = discord.ui.Select(
                placeholder=text("sell.pick"), row=0,
                options=[
                    discord.SelectOption(
                        label=f"{display_name(row)} · Lv {row.level}"[:100], value=str(row.id),
                        description=text("sell.value", value=row.value),
                        emoji=RARITY_MARK.get(row.rarity, "⚪"),
                        default=self.selected is not None and row.id == self.selected.id,
                    )
                    for row in self.rows
                ],
            )
            select.callback = self._choose
            self.add_item(select)
        prev_button = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary, row=1, disabled=self.page <= 0)
        prev_button.callback = self._prev
        next_button = discord.ui.Button(
            label="▶", style=discord.ButtonStyle.secondary, row=1,
            disabled=(self.page + 1) * SELL_PAGE_SIZE >= self.total,
        )
        next_button.callback = self._next
        sell_button = discord.ui.Button(
            label=text("sell.button", value=self.selected.value) if self.selected else text("sell.button_idle"),
            style=button_style("confirm"), row=2, disabled=self.selected is None,
        )
        sell_button.callback = self._sell
        cancel_button = discord.ui.Button(label=text("sell.cancel"), style=discord.ButtonStyle.secondary, row=2, disabled=self.selected is None)
        cancel_button.callback = self._cancel
        for item in (prev_button, next_button, sell_button, cancel_button):
            self.add_item(item)

    def embed(self) -> discord.Embed:
        return sell_embed(self.coins, self.rows, self.page, self.total, SELL_PAGE_SIZE, self.selected, self.notice)

    async def message(self, *, edit: bool) -> dict:
        """Embed plus coin banner, as keyword arguments for an edit (``edit=True``) or a send."""
        embed = self.embed()
        file = await coin_art(embed, label="SELL", coins=self.coins, drop_field=text("sell.balance"))
        return {"embed": embed, **(edit_kwargs(file) if edit else send_kwargs(file))}

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(text("ui.not_yours"), ephemeral=True)
            return False
        return True

    async def _reload(self, page: int) -> None:
        self.rows, self.total, self.page = await list_sellable(self.user_id, self.clone_id, page=page)
        if self.selected is not None and all(row.id != self.selected.id for row in self.rows):
            self.selected = None

    async def _choose(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        value = (interaction.data or {}).get("values", [""])[0]
        self.selected = next((row for row in self.rows if str(row.id) == value), None)
        self.notice = None
        self._build()
        await interaction.edit_original_response(view=self, **await self.message(edit=True))

    async def _cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        self.selected = None
        self.notice = text("sell.cancelled")
        self._build()
        await interaction.edit_original_response(view=self, **await self.message(edit=True))

    async def _turn(self, interaction: discord.Interaction, delta: int) -> None:
        await interaction.response.defer()
        try:
            await self._reload(self.page + delta)
        except Exception:
            logger.exception("Catch sell page load failed user=%s", self.user_id)
            await interaction.followup.send(text("sell.load_error"), ephemeral=True)
            return
        self.notice = None
        self._build()
        await interaction.edit_original_response(view=self, **await self.message(edit=True))

    async def _prev(self, interaction: discord.Interaction) -> None:
        await self._turn(interaction, -1)

    async def _next(self, interaction: discord.Interaction) -> None:
        await self._turn(interaction, 1)

    async def _sell(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if self._busy:
            return
        self._busy = True
        try:
            gate = await check_player_allowed(interaction.user.id, interaction.guild_id, "shop", self.clone_id)
            if not gate.allowed:
                await interaction.followup.send(text("catch.unavailable", reason=gate.reason or "disabled"), ephemeral=True)
                return
            target = self.selected
            if target is None:
                await interaction.followup.send(text("sell.no_selection"), ephemeral=True)
                return
            try:
                result = await sell_creature(target.id, self.user_id, self.clone_id, guild_id=interaction.guild_id)
            except Exception:
                logger.exception("Catch sell failed user=%s owned=%s", self.user_id, target.id)
                await interaction.followup.send(text("sell.error"), ephemeral=True)
                return
            if result.ok:
                self.coins = result.coins_left
                self.notice = text("sell.sold", name=display_name(target), value=result.value)
            elif result.reason == "favorite":
                self.notice = text("sell.favorite", name=display_name(target))
            elif result.reason == "locked":
                self.notice = text("sell.locked", name=display_name(target))
            elif result.reason == "unknown_species":
                self.notice = text("sell.unknown")
            else:
                self.notice = text("sell.gone")
            self.selected = None
            try:
                await self._reload(self.page)
            except Exception:
                logger.exception("Catch sell reload failed user=%s", self.user_id)
                self.rows = [row for row in self.rows if row.id != target.id]
            self._build()
            await interaction.edit_original_response(view=self, **await self.message(edit=True))
        finally:
            self._busy = False


async def open_sell(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    clone_id = getattr(interaction.client, "clone_id", None)
    gate = await check_player_allowed(interaction.user.id, interaction.guild_id, "shop", clone_id)
    if not gate.allowed:
        await interaction.followup.send(text("catch.unavailable", reason=gate.reason or "disabled"), ephemeral=True)
        return
    try:
        player = await load_player(interaction.user.id, clone_id)
        rows, total, page = await list_sellable(interaction.user.id, clone_id)
    except Exception:
        logger.exception("Catch sell load failed user=%s", interaction.user.id)
        await interaction.followup.send(text("sell.load_error"), ephemeral=True)
        return
    view = SellView(interaction.user.id, clone_id, player.coins, rows, total, page)
    await interaction.followup.send(view=view, ephemeral=True, **await view.message(edit=False))


__all__ = ["SellView", "open_sell", "sell_embed"]
