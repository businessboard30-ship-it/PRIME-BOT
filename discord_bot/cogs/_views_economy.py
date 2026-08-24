"""
Inline keyboards for EconomyCog.

Kept in their own module (rather than inline in economy.py) so the cog file
stays about the economy logic, not the UI wiring. Every button calls back
into the *same* cog methods the slash commands use — no duplicated logic —
so a balance change from a button and from typing /balance always agree.

Discord only supports 5 button styles (discord.ButtonStyle): primary
(blurple), secondary (grey), success (green), danger (red), link (grey,
underlined, requires a URL). We stick to that palette everywhere below.
"""

import discord


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
        "shop": ("Shop", discord.ButtonStyle.success, "🛒"),
        "leaderboard": ("Leaderboard", discord.ButtonStyle.secondary, "🏆"),
        "buy": ("Buy", discord.ButtonStyle.primary, "🛍️"),
    }

    def __init__(self, header: str, lines: list[str], accent: discord.Color, buttons: list[str] = None):
        super().__init__(timeout=180)
        text = discord.ui.TextDisplay("\n".join([f"### {header}", *lines]))
        row = None
        if buttons:
            row = discord.ui.ActionRow()
            for key in buttons:
                label, style, emoji = self._PRESETS[key]
                button = discord.ui.Button(label=label, style=style, emoji=emoji)
                button.callback = self._make_callback(key)
                row.add_item(button)
        children = [text, discord.ui.Separator(), row] if row else [text]
        self.add_item(discord.ui.Container(*children, accent_colour=accent))

    def _make_callback(self, key: str):
        async def callback(interaction: discord.Interaction):
            cog = get_economy_cog(interaction)
            if key == "balance":
                await cog.send_balance(interaction, interaction.user)
            elif key == "daily":
                await cog.claim_daily(interaction)
            elif key == "work":
                await cog.claim_work(interaction)
            elif key == "shop":
                await cog.send_shop_list(interaction)
            elif key == "leaderboard":
                await cog.send_leaderboard(interaction)
            elif key == "buy":
                await interaction.response.send_modal(ShopBuyModal())
        return callback


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
