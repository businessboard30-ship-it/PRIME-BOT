# path: discord_bot/cogs/_views_premium.py

"""Premium (config.PREMIUM_FEE_USD per month, per server): the entry points ("Go Premium" buttons,
send_premium_pitch, the renewal DM's Subscribe button) and the checkout hand-off. What the buyer actually
sees -- the plan picker, the perk list, the compare table -- is the Components V2 wizard in
_views_premium_v2.py; this module opens it. Used by the join-DM's Go Premium button
(_views_join_dm.py) and by the renewal reminder DM (guild_premium.py).

The Subscribe button is a DynamicItem (guild_id/clone_id live in the
custom_id) so it keeps working across restarts, same as every other button
in this codebase. It hands off to payments_manual.start_dual_mode_payment
with payment_type="premium" — Paystack (Ghana, 30 days per payment) and
Gumroad (international, auto-renewing membership) are picked there exactly
like every other paid feature; the unlock itself is
payments_manual.UNLOCK_HANDLERS["premium"].
"""

import logging
import re

import discord

import config
from database import db
from modules import premium_perks as pp

logger = logging.getLogger(__name__)

_SUB_RE = re.compile(r"^premium_sub:(\d+):(-|\d+)$")


_PERKS = (
    "🎉 **Giveaways** — bonus entries, auto-reroll, scheduling & custom colors\n"
    "🎫 **Tickets** — categories, custom buttons, transcripts & auto-close\n"
    "🎵 **Music Pro** — unlimited plays, uploads & downloads\n"
    "🎴 **Welcome Cards** — premium themes + your own background\n"
    "🔤 **Fonts & Designs** — 20+ fonts, frames & emoji tags\n"
    "🎨 **Custom Roles** — every member styles their own\n"
    "💀 **Hardcore Roast** — unfiltered roast battles\n"
    "🤖 **AI Chat** — 5x daily limit (30/day)\n"
    "🖼️ **Bot Branding** — your own bot name, avatar & banner\n"
    "🎮 **Roblox Alerts** — auto game update posts\n"
    "🛡️ **Anti-raid Pro** — join-profile filter, raid reports & quarantine-and-review\n"
    "🔍 **Scam Shield AI** — 20 AI image scam checks a day (free: 5), plus backup checks\n"
    "📝 **Application Forms** — 10 live forms, 5 questions, 10 colours & auto-role on accept\n"
    "🆕 **Every future feature** — free, automatically"
)
_PLUS_SWITCH_NOTE = ("-# Switching from Premium? Cancel the old Premium membership on Gumroad after you subscribe to Plus, "
                     "so you aren't billed for both.")


def _yearly_savings_pct() -> int:
    full = config.PREMIUM_FEE_USD * 12
    return round((1 - config.PREMIUM_YEARLY_FEE_USD / full) * 100) if full else 0


def _is_lifetime_expiry(expires_at) -> bool:
    import datetime as _dt
    return expires_at > _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=config.PREMIUM_LIFETIME_THRESHOLD_DAYS)


_FOOTER = "-# Not included: clones & temporary XP boosts. Anything you bought separately stays yours."


def _pitch_text(fee: float, status_line: str) -> str:
    return (
        f"## 💎 Go Premium — ${fee:g}/month for your whole server\n"
        f"-# Or ${config.PREMIUM_YEARLY_FEE_USD:g}/year (save {_yearly_savings_pct()}%) · "
        f"${config.PREMIUM_LIFETIME_FEE_USD:g} lifetime, one-time\n"
        f"{status_line}\n\n{_PERKS}\n\n"
        f"✨ **Premium Plus** — ${config.PREMIUM_PLUS_FEE_USD:g}/month or ${config.PREMIUM_PLUS_YEARLY_FEE_USD:g}/year: "
        f"AI chat {config.PREMIUM_PLUS_REPLY_CAP}/day and {config.PREMIUM_PLUS_SCAM_VISION_DAILY} AI scam checks a day\n\n"
        "🌍 Global: Gumroad, cancel anytime · 🇬🇭 Ghana: Paystack, 30 days per payment\n"
        + _FOOTER
    )


async def build_pitch(guild_id: int, clone_id) -> str:
    row = await db.get_guild_premium(guild_id, clone_id=clone_id)
    status = "Not active on this server yet."
    if row and await db.is_guild_premium_active(guild_id, clone_id):
        if _is_lifetime_expiry(row["expires_at"]):
            status = "♾️ **Lifetime Premium is active** on this server. Nothing more to pay, ever."
        else:
            ts = int(row["expires_at"].timestamp())
            status = f"✅ **Premium is active** on this server until <t:{ts}:D> (<t:{ts}:R>). Renew below to add more time."
            if row.get("plus_expires_at") and await db.is_guild_plus_active(guild_id, clone_id):
                pts = int(row["plus_expires_at"].timestamp())
                status += f"\n✨ **Premium Plus is active** until <t:{pts}:D> (<t:{pts}:R>)."
    return _pitch_text(config.PREMIUM_FEE_USD, status)


class PremiumSubscribeButton(discord.ui.DynamicItem[discord.ui.Button], template=_SUB_RE.pattern):
    def __init__(self, guild_id: int, clone_id):
        self.guild_id = guild_id
        self.clone_id = clone_id
        super().__init__(discord.ui.Button(
            label=f"Get Premium — ${config.PREMIUM_FEE_USD:g}/month", style=discord.ButtonStyle.primary, emoji="💎",
            custom_id=f"premium_sub:{guild_id}:{'-' if clone_id is None else clone_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(int(match.group(1)), None if match.group(2) == "-" else int(match.group(2)))

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.client.get_guild(self.guild_id)
        if guild is None:
            await interaction.response.send_message("I'm not in that server anymore.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await _start_checkout(interaction, guild, self.clone_id)


_PLAN_INFO = {
    "premium": ("monthly", lambda: config.PREMIUM_FEE_USD,
                lambda f: f"${f:g}/month for the whole server"),
    "premium_yearly": ("yearly", lambda: config.PREMIUM_YEARLY_FEE_USD,
                       lambda f: f"${f:g}/year for the whole server"),
    "premium_lifetime": ("lifetime", lambda: config.PREMIUM_LIFETIME_FEE_USD,
                         lambda f: f"${f:g} one-time, lifetime, for the whole server"),
    "premium_plus": ("plus monthly", lambda: config.PREMIUM_PLUS_FEE_USD,
                     lambda f: f"${f:g}/month for the whole server (Premium Plus)"),
    "premium_plus_yearly": ("plus yearly", lambda: config.PREMIUM_PLUS_YEARLY_FEE_USD,
                            lambda f: f"${f:g}/year for the whole server (Premium Plus)"),
}


async def _start_plan_checkout(interaction: discord.Interaction, guild, clone_id, plan: str) -> None:
    from payments_manual import start_dual_mode_payment
    name, fee_fn, display_fn = _PLAN_INFO[plan]
    fee = fee_fn()
    row = await db.get_guild_premium(guild.id, clone_id=clone_id)
    is_plus = plan in ("premium_plus", "premium_plus_yearly")
    status = ""
    if row and await db.is_guild_premium_active(guild.id, clone_id):
        if _is_lifetime_expiry(row["expires_at"]) and not is_plus:
            await interaction.followup.send(
                "♾️ This server already has **Lifetime Premium** — nothing more to buy.", ephemeral=True)
            return
        if is_plus:
            if row.get("plus_expires_at") and await db.is_guild_plus_active(guild.id, clone_id):
                ts = int(row["plus_expires_at"].timestamp())
                status = f"✅ **Plus active** until <t:{ts}:D> — this plan adds its time on top.\n\n"
        else:
            ts = int(row["expires_at"].timestamp())
            status = f"✅ **Active** until <t:{ts}:D> — this plan adds its time on top.\n\n"
    title = f"✨ Premium Plus ({name.title()})" if is_plus else f"💎 Go Premium ({name.title()})"
    # Short on purpose: this text sits above the pay buttons, and a screen of perks (which the buyer has just
    # seen in the wizard) pushed the buttons below the fold. The perk list lives in the wizard.
    if is_plus:
        unlocks = (f"Everything in Premium, plus {pp.plus_extras()}.\n{_PLUS_SWITCH_NOTE}")
    else:
        unlocks = f"Unlocks all {len(pp.perks())} Premium perks for the whole server."
    await start_dual_mode_payment(
        interaction, payment_type=plan, price_usd=fee,
        product_title=f"{title} — {guild.name}",
        product_description=f"{status}{unlocks}\n{_FOOTER}",
        amount_display_manual=display_fn(fee), guild_id=guild.id,
    )


async def _start_checkout(interaction: discord.Interaction, guild, clone_id) -> None:
    """Opens the Premium wizard (plans, perks, compare). Anyone in the server can pay for it. A server
    that already has Lifetime Premium still gets the wizard: it hides the Premium plans and offers Plus
    (the unlock path already supports Plus on top of Lifetime)."""
    from discord_bot.cogs._views_premium_v2 import open_premium_wizard
    await open_premium_wizard(interaction, guild, clone_id)


async def send_premium_pitch(interaction: discord.Interaction, guild_id: int, clone_id) -> None:
    """Call AFTER interaction.response.defer(ephemeral=True) (the join-DM
    feature button already does). One tap from 'Go Premium' to the pay buttons."""
    guild = interaction.client.get_guild(guild_id)
    if guild:
        await _start_checkout(interaction, guild, clone_id)
        return
    await interaction.followup.send(await build_pitch(guild_id, clone_id), ephemeral=True)


class PremiumPitchButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^premium_pitch$"):
    """Persistent 'Go Premium' button used by /help and the AI's premium
    answers. Fixed custom_id and no per-message state (the guild comes from
    the interaction itself), so it keeps working after a bot restart or
    after the message's view has timed out."""

    def __init__(self):
        super().__init__(discord.ui.Button(
            label=f"Go Premium 💎 — ${config.PREMIUM_FEE_USD:g}/month",
            style=discord.ButtonStyle.primary, custom_id="premium_pitch", row=1,
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.response.send_message(
                "Premium is per-server — run this command inside a server.", ephemeral=True
            )
            return
        clone_id = getattr(interaction.client, "clone_id", None)
        await interaction.response.defer(ephemeral=True)
        await send_premium_pitch(interaction, guild_id, clone_id)


DYNAMIC_ITEMS = (PremiumSubscribeButton, PremiumPitchButton)


# Payment types whose perks are already included in Go Premium — their pay
# messages get a "Go Premium" option next to the pay buttons.
PREMIUM_INCLUDES = {"welcome_card_pack", "ultra_welcome_pack"}


def add_go_premium_option(view: discord.ui.View, interaction: discord.Interaction,
                          payment_type: str, guild_id) -> None:
    """Adds the Go Premium button to `view` when the purchase is a card/ultra
    pack for a real guild. Safe to call from any payment path."""
    if payment_type not in PREMIUM_INCLUDES or not guild_id:
        return
    # Reuse the persistent PremiumSubscribeButton (custom_id carries guild/clone,
    # registered via DYNAMIC_ITEMS) so it survives restarts, even on the
    # no-timeout Gumroad message. Only the label/row differ.
    btn = PremiumSubscribeButton(int(guild_id), getattr(interaction.client, "clone_id", None))
    btn.item.label = f"Go Premium — ${config.PREMIUM_FEE_USD:g}/month (everything)"
    btn.item.row = 1
    view.add_item(btn)
