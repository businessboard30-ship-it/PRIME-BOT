# path: discord_bot/cogs/_views_cat_home.py

"""Home screen (Cat menu > Home): browse the five homes with their pictures, buy or move up (two taps, the second one confirms),
and use the vault (deposit, withdraw). In place, with Back to menu. One Container, budget-tested.

The rules and every database change live in modules/cat_home.py; nothing here touches the database directly."""

from __future__ import annotations

import logging
from typing import Optional

import discord

from discord_bot.cogs import _cat_visibility as vis
from modules import cat_home as ch
from modules import cat_wallet as cw

logger = logging.getLogger(__name__)

PINK = discord.Color.from_rgb(255, 182, 193)
OOPS = "😿 Something went wrong, try again in a moment."


def _clone_id(client):
    return getattr(client, "clone_id", None)


def _btn(label, cb, style=discord.ButtonStyle.secondary, emoji=None, disabled=False) -> discord.ui.Button:
    b = discord.ui.Button(label=label, emoji=emoji, style=style, disabled=disabled)
    b.callback = cb
    return b


def buy_note(res: ch.BuyResult) -> str:
    t = ch.tier(res.tier)
    name = f"{t.emoji} **{t.name}**" if t else "that home"
    return {
        "ok": f"🎉 You now own {name}! You paid {ch.price_text(res.coins, res.primole)}.",
        "poor": f"You need {ch.price_text(res.coins, res.primole)} for {name}. Nothing was taken.",
        "poor_primole": f"You need {res.primole:,} {cw.PRIMOLE} Primole for {name}. Nothing was taken. "
                        f"Diamonds turn into Primole in **My cats** (one way).",
        "same": "That's already your home.",
        "lower": "You already own a bigger home than that.",
        "unknown": "That home isn't available.",
    }.get(res.status, OOPS)


def vault_note(res: ch.VaultResult, deposit: bool) -> str:
    if res.status == "ok":
        return (f"🔒 Put {res.moved:,} {cw.COINS} in the vault ({res.vault:,} inside now)." if deposit
                else f"💰 Took {res.moved:,} {cw.COINS} out of the vault ({res.vault:,} left).")
    return {
        "no_home": "Buy a home first, then you get a vault.",
        "bad": "Type a whole number of coins, like 10000, 10k or 2.5m.",
        "full": "That doesn't fit. The vault is full, or the amount is more than the room left.",
        "poor": "You don't have that many coins in your wallet.",
        "empty": "The vault doesn't hold that much.",
    }.get(res.status, OOPS)


class HomeButton(discord.ui.Button):
    """The Home entry in the Cat menu."""

    def __init__(self):
        super().__init__(label="Home", emoji="🏡", style=discord.ButtonStyle.primary, custom_id="cat_feature:home")

    async def callback(self, interaction: discord.Interaction):
        try:
            view = HomeView()
            await view.reload(interaction)
        except Exception:
            logger.exception("[cat-home] couldn't open the home screen")
            await interaction.response.send_message(OOPS, ephemeral=True)
            return
        await vis.send(interaction, view=view)


class AmountModal(discord.ui.Modal):
    amount = discord.ui.TextInput(label="Coins", placeholder="10000, 10k or 2.5m", min_length=1, max_length=20)

    def __init__(self, parent: "HomeView", deposit: bool):
        super().__init__(title="Deposit coins" if deposit else "Withdraw coins")
        self.parent, self.deposit = parent, deposit

    async def on_submit(self, interaction: discord.Interaction):
        n = ch.parse_amount(str(self.amount))
        await self.parent.vault_move(interaction, self.deposit, n, typed=True)


class HomeView(discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=600)
        self.user_id = 0
        self.clone_id = None
        self.home: Optional[ch.Home] = None
        self.wallet = cw.Wallet()
        self.shown: str = ch.DEFAULT_KEY
        self.armed = False
        self.note: Optional[str] = None

    async def reload(self, interaction: discord.Interaction, first: bool = True) -> None:
        self.user_id, self.clone_id = interaction.user.id, _clone_id(interaction.client)
        self.home = await ch.get_home(self.user_id, self.clone_id)
        self.wallet = await cw.safe_wallet(self.user_id, self.clone_id)
        if first and self.home and self.home.t:
            self.shown = self.home.t.key
        self._build()

    def text(self) -> str:
        return ch.screen_text(self.home, self.wallet, ch.tier(self.shown) or ch.TIERS[0], self.armed, self.note)

    def _cost(self):
        own = self.home.t.key if self.home and self.home.t else None
        return ch.cost_to_reach(own, self.shown)

    def _build(self) -> None:
        self.clear_items()
        shown = ch.tier(self.shown) or ch.TIERS[0]
        own = self.home.t if self.home else None
        box = discord.ui.Container(accent_colour=PINK)
        box.add_item(discord.ui.TextDisplay(self.text()[:3900]))
        box.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(ch.image_url(shown.key), description=shown.name[:256])))
        box.add_item(discord.ui.Separator())

        select = discord.ui.Select(
            placeholder="Look at another home",
            options=[discord.SelectOption(label=t.name, value=t.key, emoji=t.emoji, default=(t.key == shown.key),
                                          description=ch.price_text(t.coins, t.primole)[:100]) for t in ch.TIERS])
        select.callback = self._pick
        box.add_item(discord.ui.ActionRow(select))

        cost = self._cost()
        if own and shown.key == own.key:
            buy = _btn("Your home", self._noop, discord.ButtonStyle.secondary, "✅", disabled=True)
        elif cost is None:
            buy = _btn("Smaller than yours", self._noop, discord.ButtonStyle.secondary, "🚫", disabled=True)
        elif self.armed:
            buy = _btn(f"Confirm: pay {cost[0]:,}" + (f" + {cost[1]:,}" if cost[1] else ""), self._buy,
                       discord.ButtonStyle.danger, "✅")
        else:
            buy = _btn("Move up" if own else "Buy this home", self._buy, discord.ButtonStyle.success, "🏡")
        box.add_item(discord.ui.ActionRow(buy, _btn("Refresh", self._refresh, discord.ButtonStyle.secondary, "🔄")))

        if own:
            box.add_item(discord.ui.ActionRow(
                _btn("Deposit…", self._deposit, discord.ButtonStyle.primary, "🔒"),
                _btn("Deposit max", self._deposit_max, discord.ButtonStyle.primary, "📥"),
                _btn("Withdraw…", self._withdraw, discord.ButtonStyle.secondary, "💰"),
                _btn("Withdraw all", self._withdraw_all, discord.ButtonStyle.secondary, "📤")))
        self.add_item(box)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return True

    async def push(self, interaction: discord.Interaction, note: Optional[str] = None, first: bool = False) -> None:
        self.note = note
        try:
            await self.reload(interaction, first=first)
        except Exception:
            logger.exception("[cat-home] couldn't refresh the home screen")
            self.note = OOPS
        await vis.edit(interaction, view=self)

    async def _noop(self, interaction: discord.Interaction):
        await interaction.response.defer()

    async def _refresh(self, interaction: discord.Interaction):
        self.armed = False
        await self.push(interaction)

    async def _pick(self, interaction: discord.Interaction):
        values = interaction.data.get("values") or []
        if values and ch.tier(values[0]):
            self.shown = values[0]
        self.armed = False
        await self.push(interaction)

    async def _buy(self, interaction: discord.Interaction):
        if self._cost() is None:
            self.armed = False
            await self.push(interaction)
            return
        if not self.armed:
            self.armed = True
            await self.push(interaction)
            return
        self.armed = False
        try:
            res = await ch.buy(self.user_id, self.clone_id, self.shown)
        except Exception:
            logger.exception("[cat-home] buy failed")
            await self.push(interaction, OOPS)
            return
        await self.push(interaction, buy_note(res), first=(res.status == "ok"))

    async def _deposit(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AmountModal(self, True))

    async def _withdraw(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AmountModal(self, False))

    async def _deposit_max(self, interaction: discord.Interaction):
        await self.vault_move(interaction, True, None)

    async def _withdraw_all(self, interaction: discord.Interaction):
        await self.vault_move(interaction, False, None)

    async def vault_move(self, interaction: discord.Interaction, deposit: bool, amount: Optional[int], typed: bool = False) -> None:
        self.armed = False
        if typed and amount is None:
            await self.push(interaction, vault_note(ch.VaultResult("bad"), deposit))
            return
        try:
            fn = ch.deposit if deposit else ch.withdraw
            res = await fn(self.user_id, self.clone_id, amount)
        except Exception:
            logger.exception("[cat-home] vault move failed")
            await self.push(interaction, OOPS)
            return
        await self.push(interaction, vault_note(res, deposit))
