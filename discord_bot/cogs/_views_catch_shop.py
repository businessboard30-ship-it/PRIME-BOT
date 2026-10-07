"""Shop and Wallet screens for the catch hub.

Both open as ephemeral messages owned by the player who pressed the hub button. Every
callback defers first (rule B.6), then checks the gate and talks to the DB. The price a
player pays comes from ``modules.catch_shop.CATALOG`` in the service, never from the
button or select value, and the buy buttons are guarded against a double click.
"""

from __future__ import annotations

import logging

import discord

from modules.catch_gate import check_player_allowed
from modules.catch_card import edit_kwargs, send_kwargs
from modules.catch_coin_card import coin_art
from modules.catch_i18n import text
from modules.catch_notice import notice_for
from modules.catch_items import item_name, load_player
from modules.catch_shop import CATALOG, QUANTITIES, load_wallet, purchase, total_price
from modules.catch_theme import button_style, state_color

logger = logging.getLogger(__name__)


def shop_embed(coins: int, selected: str | None = None, notice: str | None = None) -> discord.Embed:
    lines = [
        f"{'▶ ' if key == selected else ''}**{item_name(key)}** · {text('shop.each', price=price)}"
        for key, price in CATALOG.items()
    ]
    description = text("shop.description") + "\n\n" + "\n".join(lines)
    if notice:
        description = notice + "\n\n" + description
    embed = discord.Embed(title=text("shop.title"), description=description, colour=state_color("info"))
    embed.add_field(name=text("shop.balance"), value=f"{coins:,}", inline=True)
    return embed


def wallet_embed(wallet) -> discord.Embed:
    embed = discord.Embed(title=text("wallet.title"), colour=state_color("info"))
    embed.add_field(name=text("wallet.balance"), value=f"{wallet.coins:,}", inline=False)
    if wallet.entries:
        lines = []
        for entry in wallet.entries:
            sign = "+" if entry.coins > 0 else "−"
            when = f" · <t:{int(entry.at.timestamp())}:R>" if hasattr(entry.at, "timestamp") else ""
            lines.append(f"`{sign}{abs(entry.coins):,}` {entry.summary}{when}")
        embed.add_field(name=text("wallet.recent"), value="\n".join(lines), inline=False)
    else:
        embed.description = text("wallet.empty")
    return embed


class ShopView(discord.ui.View):
    def __init__(self, user_id: int, clone_id: int | None, coins: int, *, selected: str | None = None):
        super().__init__(timeout=600)
        self.user_id, self.clone_id, self.coins = user_id, clone_id, coins
        self.selected = selected if selected in CATALOG else None
        self.notice: str | None = None
        self._busy = False
        self._build()

    def _build(self) -> None:
        self.clear_items()
        select = discord.ui.Select(
            placeholder=text("shop.pick"), row=0,
            options=[
                discord.SelectOption(
                    label=item_name(key), value=key, description=text("shop.each", price=price),
                    default=key == self.selected,
                )
                for key, price in CATALOG.items()
            ],
        )
        select.callback = self._choose
        self.add_item(select)
        for quantity in QUANTITIES:
            if self.selected is None:
                label, affordable = f"Buy ×{quantity}", False
            else:
                total = total_price(self.selected, quantity)
                label, affordable = f"Buy ×{quantity} · {total:,} coins", self.coins >= total
            button = discord.ui.Button(label=label, style=button_style("confirm"), disabled=not affordable, row=1)
            button.callback = self._buy_callback(quantity)
            self.add_item(button)

    def embed(self) -> discord.Embed:
        return shop_embed(self.coins, self.selected, self.notice)

    async def message(self, *, edit: bool) -> dict:
        """Embed plus coin banner, as keyword arguments for an edit (``edit=True``) or a send."""
        embed = self.embed()
        file = await coin_art(embed, label="SHOP", coins=self.coins, drop_field=text("shop.balance"))
        return {"embed": embed, **(edit_kwargs(file) if edit else send_kwargs(file))}

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(**notice_for("ui.not_yours"), ephemeral=True)
            return False
        return True

    async def _choose(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        value = (interaction.data or {}).get("values", [""])[0]
        self.selected = value if value in CATALOG else None
        self.notice = None
        self._build()
        await interaction.edit_original_response(view=self, **await self.message(edit=True))

    def _buy_callback(self, quantity: int):
        async def callback(interaction: discord.Interaction) -> None:
            await self._buy(interaction, quantity)
        return callback

    async def _buy(self, interaction: discord.Interaction, quantity: int) -> None:
        await interaction.response.defer()
        if self._busy:
            return
        self._busy = True
        try:
            gate = await check_player_allowed(interaction.user.id, interaction.guild_id, "shop", self.clone_id)
            if not gate.allowed:
                await interaction.followup.send(**notice_for("catch.unavailable", reason=gate.reason or "disabled"), ephemeral=True)
                return
            if self.selected is None:
                await interaction.followup.send(**notice_for("shop.no_selection"), ephemeral=True)
                return
            try:
                result = await purchase(
                    self.user_id, self.clone_id, self.selected, quantity, guild_id=interaction.guild_id,
                )
            except Exception:
                logger.exception("Catch shop purchase failed user=%s item=%s", self.user_id, self.selected)
                await interaction.followup.send(**notice_for("shop.error"), ephemeral=True)
                return
            if result.ok:
                self.coins = result.coins_left
                self.notice = text("shop.bought", item=item_name(result.item_key), quantity=result.quantity, total=result.total)
            elif result.reason == "insufficient_coins":
                self.coins = result.coins_left
                self.notice = text("shop.insufficient", total=result.total, have=result.coins_left)
            else:
                self.notice = text("shop.not_for_sale")
            self._build()
            await interaction.edit_original_response(view=self, **await self.message(edit=True))
        finally:
            self._busy = False


async def open_shop(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    clone_id = getattr(interaction.client, "clone_id", None)
    gate = await check_player_allowed(interaction.user.id, interaction.guild_id, "shop", clone_id)
    if not gate.allowed:
        await interaction.followup.send(**notice_for("catch.unavailable", reason=gate.reason or "disabled"), ephemeral=True)
        return
    try:
        player = await load_player(interaction.user.id, clone_id)
    except Exception:
        logger.exception("Catch shop load failed user=%s", interaction.user.id)
        await interaction.followup.send(**notice_for("shop.load_error"), ephemeral=True)
        return
    view = ShopView(interaction.user.id, clone_id, player.coins)
    await interaction.followup.send(view=view, ephemeral=True, **await view.message(edit=False))


async def open_wallet(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    clone_id = getattr(interaction.client, "clone_id", None)
    gate = await check_player_allowed(interaction.user.id, interaction.guild_id, "view", clone_id)
    if not gate.allowed:
        await interaction.followup.send(**notice_for("catch.unavailable", reason=gate.reason or "disabled"), ephemeral=True)
        return
    try:
        wallet = await load_wallet(interaction.user.id, clone_id)
    except Exception:
        logger.exception("Catch wallet load failed user=%s", interaction.user.id)
        await interaction.followup.send(**notice_for("wallet.error"), ephemeral=True)
        return
    embed = wallet_embed(wallet)
    file = await coin_art(embed, label="WALLET", coins=wallet.coins, drop_field=text("wallet.balance"))
    await interaction.followup.send(embed=embed, ephemeral=True, **send_kwargs(file))


__all__ = ["ShopView", "open_shop", "open_wallet", "shop_embed", "wallet_embed"]
