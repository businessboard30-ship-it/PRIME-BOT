# path: discord_bot/cogs/_views_leveling_boost.py

"""
"⚡ Boost XP" button — the only surface for the per-user paid XP boost (see
leveling-boost-build-prompt.md §2). No slash command; this button is
attached in two places:

  1. discord_bot/cogs/leveling.py's _send_level_up_card, but ONLY while the
     member is still below level 3 (see that file — pitching a boost before
     someone's shown any real investment in leveling reads as "pay to skip
     a wall you just hit" rather than "speed up your climb").
  2. The same cog's `leaderboard` command, unconditionally, alongside the
     existing leader-link buttons.

Same restart-proof DynamicItem pattern as every other persistent button in
this codebase — see discord_bot/cogs/_views_music_panel.py's
MusicProUpgradeButton, which this mirrors almost exactly.
"""

import re

import discord

import config as app_config


class BoostXPButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^levelboost_xp:(\d+):(-?\d+|-)$"):
    """guild_id is baked into the custom_id (not read from the interaction)
    so this survives being clicked from a DM'd or forwarded message and
    still activates the boost in the RIGHT guild — same reasoning as
    MusicProUpgradeButton's guild_id param."""

    def __init__(self, guild_id: int, clone_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        clone_part = "-" if clone_id is None else str(clone_id)
        super().__init__(discord.ui.Button(
            label="Boost XP", emoji="⚡",
            style=discord.ButtonStyle.primary,
            custom_id=f"levelboost_xp:{guild_id}:{clone_part}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id = int(match.group(1))
        clone_part = match.group(2)
        clone_id = None if clone_part == "-" else int(clone_part)
        return cls(guild_id, clone_id)

    async def callback(self, interaction: discord.Interaction):
        # Bundles are only offered once their Gumroad product link exists
        # (see config.GUMROAD_PRODUCT_LINKS) — until then this behaves exactly
        # as before: straight to the single-boost checkout.
        bundles = _available_bundles()
        if not bundles:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await _start_boost_checkout(interaction, "xp_boost", self.guild_id)
            return
        lines = [
            "⚡ **Boost XP** — pick a pack. Bundles stack onto any boost time you already have.",
            "",
            f"• **1 boost** — ${app_config.XP_BOOST_FEE_USD:g} · {app_config.XP_BOOST_MULTIPLIER:g}x XP for {app_config.XP_BOOST_DURATION_DAYS} days",
        ]
        for key, b in bundles.items():
            days = b["boosts"] * app_config.XP_BOOST_DURATION_DAYS
            per = b["fee_usd"] / b["boosts"]
            lines.append(
                f"• **{b['label']} ({b['boosts']} boosts)** — ${b['fee_usd']:g} · "
                f"{app_config.XP_BOOST_MULTIPLIER:g}x XP for {days} days (${per:.2f} per boost)"
            )
        await interaction.response.send_message(
            "\n".join(lines), view=_BoostPackPickerView(self.guild_id, bundles), ephemeral=True,
        )


def _available_bundles() -> dict:
    """Bundles whose Gumroad product link is configured."""
    return {
        key: b for key, b in app_config.XP_BOOST_BUNDLES.items()
        if app_config.GUMROAD_PRODUCT_LINKS.get(key)
    }


async def _start_boost_checkout(interaction: discord.Interaction, payment_type: str, guild_id: int):
    """Starts checkout for a single boost (payment_type 'xp_boost') or a bundle
    key from config.XP_BOOST_BUNDLES. Call after interaction.response.defer."""
    from payments_manual import start_dual_mode_payment
    mult = app_config.XP_BOOST_MULTIPLIER
    if payment_type == "xp_boost":
        price = float(app_config.XP_BOOST_FEE_USD)
        title = "⚡ XP Boost"
        desc = f"⚡ {mult:g}x XP for you, {app_config.XP_BOOST_DURATION_DAYS} days."
    else:
        b = app_config.XP_BOOST_BUNDLES[payment_type]
        price = float(b["fee_usd"])
        days = b["boosts"] * app_config.XP_BOOST_DURATION_DAYS
        title = f"⚡ XP Boost {b['label']}"
        desc = f"⚡ {mult:g}x XP for you, {days} days ({b['boosts']} boosts — stacks onto any boost time you have left)."
    await start_dual_mode_payment(
        interaction, payment_type=payment_type, price_usd=price,
        product_title=title, product_description=desc,
        amount_display_manual=f"${price:g}", guild_id=guild_id,
    )


class _BoostPackPickerView(discord.ui.View):
    """Ephemeral one-shot picker shown after tapping Boost XP. Plain buttons
    (not DynamicItems): it lives only in that ephemeral reply, and the durable
    entry point is BoostXPButton itself."""

    def __init__(self, guild_id: int, bundles: dict):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self._add("xp_boost", f"1 boost — ${app_config.XP_BOOST_FEE_USD:g}", discord.ButtonStyle.secondary)
        for key, b in bundles.items():
            self._add(key, f"{b['label']} — ${b['fee_usd']:g}", discord.ButtonStyle.success)

    def _add(self, payment_type: str, label: str, style: discord.ButtonStyle):
        button = discord.ui.Button(label=label, emoji="⚡", style=style)

        async def _cb(interaction: discord.Interaction, _pt=payment_type):
            await interaction.response.defer(ephemeral=True, thinking=True)
            await _start_boost_checkout(interaction, _pt, self.guild_id)

        button.callback = _cb
        self.add_item(button)


def build_boost_xp_view(guild_id: int, clone_id) -> discord.ui.View:
    """Small helper so both call sites (level-up card, leaderboard) build
    the button the same way instead of duplicating the View(timeout=None)
    + add_item boilerplate."""
    view = discord.ui.View(timeout=None)
    view.add_item(BoostXPButton(guild_id, clone_id))
    return view


DYNAMIC_ITEMS = (BoostXPButton,)
