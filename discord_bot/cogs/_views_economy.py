"""
Inline keyboards for EconomyCog.

Kept in their own module (rather than inline in economy.py) so the cog file
stays about the economy logic, not the UI wiring. Every button calls back
into the *same* cog methods the slash commands use — no duplicated logic —
so a balance change from a button and from typing /balance always agree.

Discord only supports 5 button styles (discord.ButtonStyle): primary
(blurple), secondary (grey), success (green), danger (red), link (grey,
underlined, requires a URL). We stick to that palette everywhere below.

Persistence: every button below uses a FIXED custom_id ("eco:<key>") and
timeout=None, so it survives a bot restart. That only works because
discord_bot/bot.py's setup_hook registers one EconomyCardView(persistent=True)
containing every known button key BEFORE on_ready fires — discord.py matches
an incoming component interaction to a registered view purely by custom_id,
not by which message or which original view instance sent it, so a card
sent with only ["balance", "shop"] still works after a restart as long as
those two custom_ids were part of whatever got registered at startup.
"""

import discord

from database import db

_PERSISTENT_KEYS = [
    "balance", "daily", "work", "beg", "shop", "leaderboard", "leaderboard_toggle",
    "buy", "rob", "inventory", "trade", "trade_buy", "trade_sell", "send",
]


def get_economy_cog(interaction: discord.Interaction):
    return interaction.client.get_cog("EconomyCog")


class EconomyCardView(discord.ui.LayoutView):
    """Components V2 replacement for the old plain-text-plus-View pattern.
    One reusable card: header + body lines in a colored Container, with an
    ActionRow of nav buttons that call back into the same cog methods the
    slash commands use — so a click and a retyped command always agree.

    button presets are short keys rather than full Button objects so every
    call site stays a one-liner; add a new preset here if a new nav target
    is ever needed instead of hand-building buttons at each call site.
    """

    _PRESETS = {
        "balance": ("Balance", discord.ButtonStyle.secondary, "💰"),
        "daily": ("Daily", discord.ButtonStyle.primary, "🎁"),
        "work": ("Work", discord.ButtonStyle.primary, "💼"),
        "beg": ("Beg", discord.ButtonStyle.secondary, "🙏"),
        "shop": ("Shop", discord.ButtonStyle.success, "🛒"),
        "leaderboard": ("Leaderboard", discord.ButtonStyle.secondary, "🏆"),
        "leaderboard_toggle": ("Toggle view", discord.ButtonStyle.secondary, "🔄"),
        "buy": ("Buy", discord.ButtonStyle.primary, "🛍️"),
        "rob": ("Rob", discord.ButtonStyle.danger, "🦹"),
        "inventory": ("Inventory", discord.ButtonStyle.secondary, "🎒"),
        "trade": ("Trade board", discord.ButtonStyle.primary, "📋"),
        "trade_buy": ("Buy listing", discord.ButtonStyle.primary, "🛍️"),
        "trade_sell": ("List item", discord.ButtonStyle.success, "🏷️"),
        "send": ("Send", discord.ButtonStyle.secondary, "💸"),
    }

    def __init__(self, header: str = "", lines: list[str] = None, accent: discord.Color = discord.Color.blurple(),
                 buttons: list[str] = None, *, persistent: bool = False):
        # timeout=None only for the one prototype instance bot.py registers
        # at startup — every real card sent to a channel keeps the 180s
        # timeout (Discord ignores it for the persistence match anyway,
        # since matching happens by custom_id against the registered
        # prototype, not against this particular message's view).
        super().__init__(timeout=None if persistent else 180)
        button_keys = buttons if buttons is not None else (_PERSISTENT_KEYS if persistent else [])
        row = None
        text = None
        if not persistent:
            text = discord.ui.TextDisplay("\n".join([f"### {header}", *(lines or [])]))
        # Discord caps an ActionRow at 5 children — chunk the requested keys
        # into as many rows as needed (persistent registers all 14 presets
        # at once, which alone needs 3 rows; a plain ActionRow.add_item
        # raises ValueError('maximum number of children exceeded') past 5,
        # which is exactly what crashed startup before this chunking existed).
        rows = []
        for i in range(0, len(button_keys), 5):
            row = discord.ui.ActionRow()
            for key in button_keys[i:i + 5]:
                label, style, emoji = self._PRESETS[key]
                button = discord.ui.Button(label=label, style=style, emoji=emoji, custom_id=f"eco:{key}")
                button.callback = self._make_callback(key)
                row.add_item(button)
            rows.append(row)
        if persistent:
            # The registered prototype never gets displayed — it exists
            # purely to give every custom_id a live callback — but
            # LayoutView still requires at least one non-empty container.
            placeholder = discord.ui.TextDisplay("economy card prototype")
            children = [placeholder, discord.ui.Separator(), *rows] if rows else [placeholder]
        else:
            children = [text, discord.ui.Separator(), *rows] if rows else [text]
        self.add_item(discord.ui.Container(*children, accent_colour=accent))

    def _make_callback(self, key: str):
        async def callback(interaction: discord.Interaction):
            cog = get_economy_cog(interaction)
            if cog is None:
                await interaction.response.send_message("Economy is unavailable right now.", ephemeral=True)
                return
            if key == "balance":
                await cog.send_balance(interaction, interaction.user)
            elif key == "daily":
                await cog.claim_daily(interaction)
            elif key == "work":
                await cog.claim_work(interaction)
            elif key == "beg":
                cfg = await db.get_economy_config(interaction.guild_id, clone_id=_clone_id(interaction))
                await cog._earn(
                    interaction, cooldown_field="last_beg_at", cooldown_hours=0.25,
                    amount_min=cfg["beg_min"], amount_max=cfg["beg_max"],
                    reason="beg", verb="begged for change"
                )
            elif key == "shop":
                await cog.send_shop_list(interaction)
            elif key == "leaderboard":
                await cog.send_leaderboard(interaction)
            elif key == "leaderboard_toggle":
                current_net_worth = _message_says_net_worth(interaction.message)
                await cog.send_leaderboard(interaction, net_worth=not current_net_worth)
            elif key == "buy":
                await interaction.response.send_modal(ShopBuyModal())
            elif key == "rob":
                await interaction.response.send_message(
                    "Who do you want to rob?", view=RobPickerView(), ephemeral=True
                )
            elif key == "inventory":
                await cog.send_inventory(interaction)
            elif key == "trade":
                await cog.send_trade_board(interaction)
            elif key == "trade_buy":
                await interaction.response.send_modal(TradeBuyModal())
            elif key == "trade_sell":
                await interaction.response.send_modal(TradeSellModal())
            elif key == "send":
                await interaction.response.send_message(
                    "Who do you want to send coins to?", view=SendPickerView(), ephemeral=True
                )
        return callback


def _clone_id(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None)


def _message_says_net_worth(message: discord.Message) -> bool:
    """Persistent buttons have no per-instance state after a restart, so
    the toggle reads the card's own header text back off the message
    rather than closing over a flag — works whether or not the process
    that sent the card is the one handling the click."""
    try:
        for component in message.components:
            for child in getattr(component, "children", []):
                content = getattr(child, "content", None)
                if content and "net worth" in content.lower():
                    return True
    except Exception:
        pass
    return False


class ShopBuyModal(discord.ui.Modal, title="Buy shop item"):
    item_id = discord.ui.TextInput(label="Item # (from the shop list)", placeholder="1", max_length=10)

    def __init__(self):
        super().__init__()

    async def on_submit(self, interaction: discord.Interaction):
        cog = get_economy_cog(interaction)
        if not str(self.item_id).isdigit():
            await interaction.response.send_message("That's not a valid item number.", ephemeral=True)
            return
        await cog.buy_item(interaction, int(str(self.item_id)))


class TradeBuyModal(discord.ui.Modal, title="Buy from trade board"):
    listing_id = discord.ui.TextInput(label="Listing # (from the trade board)", placeholder="1", max_length=10)

    async def on_submit(self, interaction: discord.Interaction):
        cog = get_economy_cog(interaction)
        if not str(self.listing_id).isdigit():
            await interaction.response.send_message("That's not a valid listing number.", ephemeral=True)
            return
        await cog.buy_trade_listing(interaction, int(str(self.listing_id)))


class TradeSellModal(discord.ui.Modal, title="List an item for trade"):
    item_id = discord.ui.TextInput(label="Item # (from your inventory)", placeholder="1", max_length=10)
    price = discord.ui.TextInput(label="Asking price", placeholder="100", max_length=15)

    async def on_submit(self, interaction: discord.Interaction):
        cog = get_economy_cog(interaction)
        if not str(self.item_id).isdigit() or not str(self.price).isdigit() or int(str(self.price)) <= 0:
            await interaction.response.send_message("Enter a valid item # and a positive price.", ephemeral=True)
            return
        await cog.sell_on_trade_board(interaction, int(str(self.item_id)), int(str(self.price)))


class SendAmountModal(discord.ui.Modal, title="Send coins"):
    amount = discord.ui.TextInput(label="Amount to send", placeholder="100", max_length=15)

    def __init__(self, target: discord.Member):
        super().__init__()
        self.target = target

    async def on_submit(self, interaction: discord.Interaction):
        cog = get_economy_cog(interaction)
        if not str(self.amount).isdigit() or int(str(self.amount)) <= 0:
            await interaction.response.send_message("Enter a positive whole number.", ephemeral=True)
            return
        await cog.send_coins(interaction, self.target, int(str(self.amount)))


class RobPickerView(discord.ui.View):
    """Short-lived (not persistent) — sent fresh every time someone clicks
    Rob, so it never needs to survive a restart. A UserSelect can't live
    inside a LayoutView Container alongside the other economy buttons, so
    this is its own small ephemeral followup instead of being on the card."""
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Pick a member to rob")
    async def picked(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        cog = get_economy_cog(interaction)
        member = select.values[0]
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("Couldn't resolve that member.", ephemeral=True)
            return
        await cog.attempt_rob(interaction, member)


class SendPickerView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Pick who to send coins to")
    async def picked(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        member = select.values[0]
        if not isinstance(member, discord.Member) or member.bot:
            await interaction.response.send_message("Can't send coins to that member.", ephemeral=True)
            return
        await interaction.response.send_modal(SendAmountModal(member))
