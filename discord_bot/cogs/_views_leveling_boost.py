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
import logging

logger = logging.getLogger(__name__)


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
        # The $1 single boost is gone: the Level Pass ($1.99: custom level-up card + one XP boost) and the
        # multi-boost packs are what is sold. Nothing on sale yet -> say so instead of offering something else.
        bundles = _available_bundles()
        offer_pass = await pass_offered(self.clone_id)
        if not bundles and not offer_pass:
            await interaction.response.send_message(
                "⚡ XP boosts aren't on sale right now. Try again in a little while.", ephemeral=True)
            return
        lines = ["⚡ **Boost XP** — pick an option. Boosts stack onto any boost time you already have.", ""]
        if offer_pass:
            lines.append(
                f"• **🎫 Level Pass** — ${app_config.LEVEL_PASS_FEE_USD:g} once · your own level-up card for "
                f"{app_config.LEVEL_PASS_CARD_DAYS} days **plus** {app_config.XP_BOOST_MULTIPLIER:g}x XP for "
                f"{app_config.XP_BOOST_DURATION_DAYS} days. One payment, no subscription.")
        for key, b in bundles.items():
            days = b["boosts"] * app_config.XP_BOOST_DURATION_DAYS
            per = b["fee_usd"] / b["boosts"]
            lines.append(
                f"• **{b['label']} ({b['boosts']} boosts)** — ${b['fee_usd']:g} · "
                f"{app_config.XP_BOOST_MULTIPLIER:g}x XP for {days} days (${per:.2f} per boost) · includes your own "
                f"level-up card for {app_config.LEVEL_PASS_CARD_DAYS} days"
            )
        await interaction.response.send_message(
            view=build_boost_picker_view(self.guild_id, bundles, offer_pass, "\n".join(lines)), ephemeral=True,
        )


async def pass_offered(clone_id) -> bool:
    """Offer the Level Pass when its Gumroad product is ready, or when this bot's payments don't go through Gumroad
    (Paystack/manual need no product link). Any problem -> False, so the old single-boost flow keeps working.
    Always False while config.LEVELUP_PAYMENTS_ENABLED is off (paid level-ups removed)."""
    if not getattr(app_config, "LEVELUP_PAYMENTS_ENABLED", False):
        return False
    try:
        import gumroad_autocreate as _auto
        await _auto.load_runtime()
        if _auto.link_for("level_pass"):
            return True
        from database import db
        return (await db.get_payment_mode(clone_id)) != "gumroad"
    except Exception:
        logger.warning("swallowed exception in pass_offered", exc_info=True)
        return False


def _available_bundles() -> dict:
    """Bundles whose Gumroad product link is configured (none while paid level-ups are switched off)."""
    if not getattr(app_config, "LEVELUP_PAYMENTS_ENABLED", False):
        return {}
    return {
        key: b for key, b in app_config.XP_BOOST_BUNDLES.items()
        if app_config.GUMROAD_PRODUCT_LINKS.get(key)
    }


async def _start_boost_checkout(interaction: discord.Interaction, payment_type: str, guild_id: int):
    """Starts checkout for the Level Pass (payment_type 'level_pass') or a bundle
    key from config.XP_BOOST_BUNDLES. Call after interaction.response.defer."""
    from payments_manual import start_dual_mode_payment
    mult = app_config.XP_BOOST_MULTIPLIER
    if payment_type == "level_pass":
        price = float(app_config.LEVEL_PASS_FEE_USD)
        title = "🎫 Level Pass"
        desc = (f"🎫 Your own level-up card for {app_config.LEVEL_PASS_CARD_DAYS} days + {mult:g}x XP for you, "
                f"{app_config.XP_BOOST_DURATION_DAYS} days. One payment, no subscription.")
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


class BoostBuyButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^lvlboostbuy:(\d+):([a-z0-9_]+)$"):
    """One purchase option in the Boost XP picker. Guild and product key live in the custom_id and
    the offer is re-validated against what is on sale NOW, so the picker never expires and still
    works after a restart (and a product that was withdrawn since says so instead of charging)."""

    def __init__(self, guild_id: int, payment_type: str, label: str = "", style=discord.ButtonStyle.success,
                 emoji: str = "⚡"):
        self.guild_id = guild_id
        self.payment_type = payment_type
        super().__init__(discord.ui.Button(
            label=label or payment_type, emoji=emoji, style=style,
            custom_id=f"lvlboostbuy:{guild_id}:{payment_type}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(int(match.group(1)), match.group(2))

    async def callback(self, interaction: discord.Interaction):
        clone_id = getattr(interaction.client, "clone_id", None)
        if self.payment_type == "level_pass":
            on_sale = await pass_offered(clone_id)
        else:
            on_sale = self.payment_type in _available_bundles()
        if not on_sale:
            await interaction.response.send_message(
                "That option isn't on sale any more. Tap **Boost XP** again to see what's available.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await _start_boost_checkout(interaction, self.payment_type, self.guild_id)


def build_boost_picker_view(guild_id: int, bundles: dict, offer_pass: bool, text: str) -> discord.ui.LayoutView:
    """Ephemeral 'pick a pack' message (Components V2, no timeout)."""
    buttons = []
    if offer_pass:
        buttons.append(BoostBuyButton(guild_id, "level_pass", f"Level Pass — ${app_config.LEVEL_PASS_FEE_USD:g}", emoji="🎫"))
    for key, b in bundles.items():
        buttons.append(BoostBuyButton(guild_id, key, f"{b['label']} — ${b['fee_usd']:g}"))
    view = discord.ui.LayoutView(timeout=None)
    box = discord.ui.Container(accent_colour=discord.Color.gold())
    box.add_item(discord.ui.TextDisplay(text))
    box.add_item(discord.ui.Separator())
    for i in range(0, len(buttons), 5):                      # an action row holds at most 5 buttons
        box.add_item(discord.ui.ActionRow(*buttons[i:i + 5]))
    view.add_item(box)
    return view


def build_boost_xp_view(guild_id: int, clone_id) -> discord.ui.View:
    """Small helper so both call sites (level-up card, leaderboard) build
    the button the same way instead of duplicating the View(timeout=None)
    + add_item boilerplate."""
    view = discord.ui.View(timeout=None)
    view.add_item(BoostXPButton(guild_id, clone_id))
    return view


DYNAMIC_ITEMS = (BoostXPButton, BoostBuyButton)
