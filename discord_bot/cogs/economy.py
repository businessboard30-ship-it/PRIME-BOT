"""
Economy game — Discord equivalent of Dank Memer, ad-supported by design.

Deliberately its own point pool (discord_economy_balances), NOT shared with
discord_xp — see discord-bot-expansion-spec.md §2.5's decision log: leveling
is an engagement mechanic that should be hard to game, the economy is a
gambling-adjacent minigame with its own faucets/sinks, and mixing the two
means every currency exploit becomes a leveling exploit too.

Monetization (spec §4, open question #1) — the owner explicitly rejected a
"buy currency with real money" flow, full stop. This cog supports all three
of the interpretations that were on the table, each independently toggled
per guild via /ecoconfig, none of them a real-money path:

  - Vote-gated bonus (/vote): posts the guild's configured voting-site link
    and, on a cooldown, grants a bonus. The slash command itself stays
    honor-system (it can't confirm you actually clicked vote) but there is
    now also a verified path: api/vote_webhook.py receives top.gg's /
    discordbotlist.com's server-to-server vote notification and grants the
    same bonus via db.grant_vote_bonus_for_voter — configure
    config.TOPGG_WEBHOOK_AUTH and point the listing site's webhook URL at
    <PUBLIC_BASE_URL>/api/vote_webhook to enable it. Both paths share the
    same cooldown field (last_vote_bonus_at) so a member can't double-dip
    by running /vote right after the webhook already credited them.
  - Sponsored/affiliate embed (/watchad): shows the guild's configured
    sponsor embed, grants a bonus on a separate (shorter) cooldown.
  - Ad-network SDK/webview: not implemented — Discord's UI has no clean
    webview/SDK surface inside a slash command response (see the spec's
    own caveat on this option), so /watchad's embed-then-bonus pattern is
    the closest fit achievable inside Discord today. If a real ad SDK
    becomes available this is where it would slot in.

Siloed per (guild_id, clone_id): a clone owner running the bot across many
guilds gets a separate balance/shop per guild, matching the
discord_premium_groups convention, so currency can't be farmed in one lax
guild and spent in another the same clone also runs.

Every balance mutation goes through db.adjust_economy_balance, which floors
at 0 and writes a discord_economy_transactions row — nothing here mutates
the balance column directly, so the audit trail can't drift from reality.

i18n: every user-facing string below goes through discord_bot.i18n_helpers'
tr(english_template, lang, **kwargs) rather than being sent as raw English.
lang comes from get_lang(interaction) (the user's saved language pref,
shared with the Telegram bot). tr() looks the template up in the
AI-generated locale cache (see i18n.py + scripts/generate_ai_locales.py)
and only calls Groq live if that cache doesn't have it yet — English users
never pay that cost since lang == 'en' short-circuits before any lookup.
"""

import logging
import random
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from discord_bot.cogs._dm_support import GuildOnlyCog

from database import db
from discord_bot.cogs._views_economy_wizard import (
    build_wizard_view as build_economy_wizard_view,
    remember_wizard_message as remember_economy_wizard_message,
    refresh_posted_wizard as refresh_economy_wizard,
)
from discord_bot.i18n_helpers import get_lang, tr
from discord_bot.cogs._views_economy import EconomyCardView

logger = logging.getLogger(__name__)

# Coin-for-coin sends: tax is a pure sink (funds nothing), the cap is a
# multiple of the guild's own /ecoconfig daily_amount so it scales with
# each guild's economy instead of being one hardcoded number. Both exist to
# blunt alt-account funneling and real-money coin sales — see the plan
# discussion in the economy cog's module docstring neighbor, _views_economy.py.
SEND_TAX_RATE = 0.05
SEND_DAILY_CAP_MULTIPLIER = 10
ROB_PROTECTION_HOURS = 1
MAX_SHOP_IMAGE_BYTES = 200 * 1024


def _require_perm(interaction: discord.Interaction, perm: str) -> bool:
    """Checks the invoking user's permission in the current channel.

    Uses interaction.permissions (always populated by Discord for any
    command run inside a guild channel) rather than
    interaction.user.guild_permissions, because interaction.user comes
    back as a plain discord.User instead of discord.Member when this app
    is invoked via a user-install context — even while run inside a real
    server channel — which made guild_permissions unreachable for anyone
    using the bot as a personal (user-installed) app, including owners.
    """
    if interaction.guild is None:
        return False
    return getattr(interaction.permissions, perm, False)


async def _deny(interaction: discord.Interaction, perm_name: str, lang: str):
    msg = await tr("You need the **{perm_name}** permission to do that.", lang, perm_name=perm_name)
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


def _clone_id_of(interaction: discord.Interaction):
    """Same convention as every other cog in this expansion: None on the
    main bot, the clone's row id on a clone process."""
    return getattr(interaction.client, "clone_id", None)


def _seconds_since(ts) -> float:
    if ts is None:
        return float("inf")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()


def _fmt_cooldown(seconds_left: float) -> str:
    seconds_left = max(0, int(seconds_left))
    hours, rem = divmod(seconds_left, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


class EconomyCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── shared cooldown-gated earn helper ──────────────────────────────
    async def _earn(self, interaction: discord.Interaction, *, cooldown_field: str,
                     cooldown_hours: float, amount_min: int, amount_max: int, reason: str,
                     verb: str):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        clone_id = _clone_id_of(interaction)
        cfg = await db.get_economy_config(interaction.guild_id, clone_id=clone_id)
        balance = await db.get_economy_balance(interaction.guild_id, interaction.user.id, clone_id=clone_id)
        elapsed = _seconds_since(balance.get(cooldown_field))
        cooldown_seconds = cooldown_hours * 3600
        if elapsed < cooldown_seconds:
            msg = await tr(
                "⏳ You can {verb} again in **{cooldown}**.", lang,
                verb=verb, cooldown=_fmt_cooldown(cooldown_seconds - elapsed)
            )
            await interaction.followup.send(msg, ephemeral=True)
            return
        amount = random.randint(amount_min, amount_max) if amount_max > amount_min else amount_min
        new_balance = await db.adjust_economy_balance(
            interaction.guild_id, interaction.user.id, amount, reason,
            clone_id=clone_id, cooldown_field=cooldown_field
        )
        line = await tr(
            "You {verb} and earned **{amount} {symbol} {currency}**. Balance: **{balance} {symbol}**", lang,
            verb=verb, amount=amount, symbol=cfg["currency_symbol"], currency=cfg["currency_name"],
            balance=new_balance
        )
        card = EconomyCardView("💰 Earned", [line], discord.Color.green(), buttons=["balance", "daily", "work", "shop"])
        await interaction.followup.send(view=card)

    # ── plain (button-reusable) versions of the earn commands ──────────
    async def claim_daily(self, interaction: discord.Interaction):
        cfg = await db.get_economy_config(interaction.guild_id, clone_id=_clone_id_of(interaction))
        await self._earn(
            interaction, cooldown_field="last_daily_at", cooldown_hours=24,
            amount_min=cfg["daily_amount"], amount_max=cfg["daily_amount"],
            reason="daily", verb="claimed your daily reward"
        )

    async def claim_work(self, interaction: discord.Interaction):
        cfg = await db.get_economy_config(interaction.guild_id, clone_id=_clone_id_of(interaction))
        await self._earn(
            interaction, cooldown_field="last_work_at", cooldown_hours=1,
            amount_min=cfg["work_min"], amount_max=cfg["work_max"],
            reason="work", verb="worked a shift"
        )

    # All ten of this cog's commands used to be top-level (/daily, /work,
    # /beg, /balance, /leaderboard-economy, /coinflip, /rob, /buy, /vote,
    # /watchad) which cost 10 of Discord's 100 global command slots. Folded
    # into one /economy group here — Discord only counts a group as 1 slot
    # no matter how many subcommands it has, so this drops the cost to 1.
    # Subcommands inherit guild_only from the group decorator below, same
    # as shop_group/eco_group already did — no per-command decorator needed.
    economy_group = app_commands.guild_only()(app_commands.Group(name="economy", description="Earn, bet, and spend your currency"))

    @economy_group.command(name="daily", description="Claim your daily currency bonus")
    async def daily(self, interaction: discord.Interaction):
        await self.claim_daily(interaction)

    @economy_group.command(name="work", description="Work a shift for currency")
    async def work(self, interaction: discord.Interaction):
        await self.claim_work(interaction)

    @economy_group.command(name="beg", description="Beg for spare change")
    async def beg(self, interaction: discord.Interaction):
        cfg = await db.get_economy_config(interaction.guild_id, clone_id=_clone_id_of(interaction))
        await self._earn(
            interaction, cooldown_field="last_beg_at", cooldown_hours=0.25,
            amount_min=cfg["beg_min"], amount_max=cfg["beg_max"],
            reason="beg", verb="begged for change"
        )

    async def send_balance(self, interaction: discord.Interaction, target: discord.Member):
        await interaction.response.defer()
        lang = await get_lang(interaction)
        clone_id = _clone_id_of(interaction)
        cfg = await db.get_economy_config(interaction.guild_id, clone_id=clone_id)
        bal = await db.get_economy_balance(interaction.guild_id, target.id, clone_id=clone_id)
        line = await tr(
            "**{name}** has **{balance} {symbol} {currency}**", lang,
            name=target.display_name, balance=bal["balance"],
            symbol=cfg["currency_symbol"], currency=cfg["currency_name"]
        )
        card = EconomyCardView("💰 Balance", [line], discord.Color.blurple(), buttons=["send", "inventory", "leaderboard", "shop"])
        await interaction.followup.send(view=card)

    @economy_group.command(name="balance", description="Check your (or someone else's) balance")
    @app_commands.describe(member="Member to check (optional)")
    async def balance(self, interaction: discord.Interaction, member: discord.Member = None):
        await self.send_balance(interaction, member or interaction.user)

    async def send_leaderboard(self, interaction: discord.Interaction, net_worth: bool = False):
        if not interaction.response.is_done():
            await interaction.response.defer()
        lang = await get_lang(interaction)
        clone_id = _clone_id_of(interaction)
        cfg = await db.get_economy_config(interaction.guild_id, clone_id=clone_id)
        if net_worth:
            rows = await db.get_economy_leaderboard_networth(interaction.guild_id, clone_id=clone_id, limit=10)
        else:
            rows = await db.get_economy_leaderboard(interaction.guild_id, clone_id=clone_id, limit=10)
        if not rows:
            msg = await tr("No one has earned anything yet.", lang)
            await interaction.followup.send(msg, ephemeral=True)
            return
        lines = []
        for i, row in enumerate(rows, start=1):
            member = interaction.guild.get_member(row["user_id"])
            name = member.display_name if member else f"User {row['user_id']}"
            amount = row["net_worth"] if net_worth else row["balance"]
            lines.append(f"**{i}.** {name} — {amount} {cfg['currency_symbol']}")
        title = "🏆 Leaderboard — net worth" if net_worth else "🏆 Leaderboard — coins"
        card = EconomyCardView(title, lines, discord.Color.gold(),
                                buttons=["leaderboard_toggle", "balance", "shop"])
        await interaction.followup.send(view=card)

    @economy_group.command(name="leaderboard", description="Show this server's richest members")
    async def leaderboard_economy(self, interaction: discord.Interaction):
        await self.send_leaderboard(interaction)

    # ── minigames ────────────────────────────────────────────────────────
    @economy_group.command(name="coinflip", description="Bet currency on a coin flip")
    @app_commands.describe(amount="Amount to bet", choice="heads or tails")
    @app_commands.choices(choice=[
        app_commands.Choice(name="Heads", value="heads"),
        app_commands.Choice(name="Tails", value="tails"),
    ])
    async def coinflip(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, None],
                        choice: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        clone_id = _clone_id_of(interaction)
        cfg = await db.get_economy_config(interaction.guild_id, clone_id=clone_id)
        bal = await db.get_economy_balance(interaction.guild_id, interaction.user.id, clone_id=clone_id)
        if amount > bal["balance"]:
            msg = await tr("❌ You don't have that much.", lang)
            await interaction.followup.send(msg, ephemeral=True)
            return
        result = random.choice(["heads", "tails"])
        won = result == choice.value
        delta = amount if won else -amount
        new_balance = await db.adjust_economy_balance(
            interaction.guild_id, interaction.user.id, delta, "coinflip", clone_id=clone_id
        )
        if won:
            line = await tr(
                "🪙 It landed on **{result}** — you won! Balance: **{balance} {symbol}**", lang,
                result=result, balance=new_balance, symbol=cfg["currency_symbol"]
            )
        else:
            line = await tr(
                "🪙 It landed on **{result}** — you lost! Balance: **{balance} {symbol}**", lang,
                result=result, balance=new_balance, symbol=cfg["currency_symbol"]
            )
        card = EconomyCardView("Coinflip", [line], discord.Color.green() if won else discord.Color.red(),
                                buttons=["balance", "shop"])
        await interaction.followup.send(view=card)

    @economy_group.command(name="rob", description="Attempt to rob another member")
    async def rob(self, interaction: discord.Interaction, member: discord.Member):
        await self.attempt_rob(interaction, member)

    async def attempt_rob(self, interaction: discord.Interaction, member: discord.Member):
        """2 attempts per UTC day, entirely atomic (see db.attempt_robbery —
        one transaction checks the daily allowance, checks the victim's 1h
        post-rob protection, rolls the outcome and moves the coins), so
        concurrent attempts can't double-spend the last attempt of the day
        or hit a victim mid-protection-window."""
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        if member.id == interaction.user.id or member.bot:
            msg = await tr("❌ You can't rob that person.", lang)
            await interaction.followup.send(msg, ephemeral=True)
            return
        clone_id = _clone_id_of(interaction)
        cfg = await db.get_economy_config(interaction.guild_id, clone_id=clone_id)
        victim_bal = await db.get_economy_balance(interaction.guild_id, member.id, clone_id=clone_id)
        if victim_bal["balance"] < 10:
            msg = await tr("❌ {name} has nothing worth stealing.", lang, name=member.display_name)
            await interaction.followup.send(msg, ephemeral=True)
            return
        try:
            result = await db.attempt_robbery(
                interaction.guild_id, clone_id, interaction.user.id, member.id,
                cfg["rob_success_chance"], protection_hours=ROB_PROTECTION_HOURS
            )
        except db.RobUnavailable as e:
            if e.code == "no_attempts":
                msg = await tr("⏳ You've used both robbery attempts today. Try again tomorrow.", lang)
            else:
                msg = await tr(
                    "🛡️ {name} is protected from robbery for **{cooldown}**.", lang,
                    name=member.display_name, cooldown=_fmt_cooldown(e.seconds_left)
                )
            await interaction.followup.send(msg, ephemeral=True)
            return
        if result["success"]:
            line = await tr(
                "🦹 You robbed **{stolen} {symbol}** from {name}! Balance: **{balance} {symbol}**\n"
                "-# {left} attempt(s) left today", lang,
                stolen=result["stolen"], symbol=cfg["currency_symbol"], name=member.display_name,
                balance=result["robber_balance"], left=result["attempts_left"]
            )
            card = EconomyCardView("Rob — success", [line], discord.Color.green(), buttons=["balance", "shop"])
        else:
            line = await tr(
                "🚨 You got caught trying to rob {name} and paid a **{fine} {symbol}** fine. "
                "Balance: **{balance} {symbol}**\n-# {left} attempt(s) left today", lang,
                name=member.display_name, fine=result["fine"], symbol=cfg["currency_symbol"],
                balance=result["robber_balance"], left=result["attempts_left"]
            )
            card = EconomyCardView("Rob — caught", [line], discord.Color.red(), buttons=["balance", "shop"])
        await interaction.followup.send(view=card)

    # ── inventory ────────────────────────────────────────────────────────
    async def send_inventory(self, interaction: discord.Interaction, target: discord.Member = None):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        target = target or interaction.user
        clone_id = _clone_id_of(interaction)
        items = await db.get_inventory(interaction.guild_id, target.id, clone_id=clone_id)
        if not items:
            msg = await tr("🎒 {name}'s inventory is empty.", lang, name=target.display_name)
            await interaction.followup.send(msg, ephemeral=True)
            return
        cfg = await db.get_economy_config(interaction.guild_id, clone_id=clone_id)
        lines = [f"**#{i['item_id']} — {i['name']}** x{i['quantity']} — worth {i['price']} {cfg['currency_symbol']} each"
                  for i in items]
        card = EconomyCardView("🎒 Inventory", lines, discord.Color.blurple(), buttons=["trade", "shop"])
        await interaction.followup.send(view=card)

    # ── shop buy → now grants an inventory row, not just the role ─────────

    # ── shop ────────────────────────────────────────────────────────────
    shop_group = app_commands.guild_only()(app_commands.Group(name="shop", description="Browse and manage the server shop"))

    async def send_shop_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        clone_id = _clone_id_of(interaction)
        cfg = await db.get_economy_config(interaction.guild_id, clone_id=clone_id)
        items = await db.get_shop_items(interaction.guild_id, clone_id=clone_id)
        if not items:
            msg = await tr("The shop is empty.", lang)
            await interaction.followup.send(msg, ephemeral=True)
            return
        lines = []
        for i in items:
            line = f"**#{i['item_id']} — {i['name']}** — {i['price']} {cfg['currency_symbol']}"
            if i["description"]:
                line += f"\n-# {i['description']}"
            lines.append(line)
        card = EconomyCardView("🛒 Shop", lines, discord.Color.green(), buttons=["buy", "inventory", "balance"])
        await interaction.followup.send(view=card)

    @shop_group.command(name="list", description="List items in the shop")
    async def shop_list(self, interaction: discord.Interaction):
        await self.send_shop_list(interaction)

    async def buy_item(self, interaction: discord.Interaction, item_id: int):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        clone_id = _clone_id_of(interaction)
        cfg = await db.get_economy_config(interaction.guild_id, clone_id=clone_id)
        item = await db.get_shop_item(interaction.guild_id, item_id, clone_id=clone_id)
        if not item:
            msg = await tr("❌ No such item.", lang)
            await interaction.followup.send(msg, ephemeral=True)
            return
        bal = await db.get_economy_balance(interaction.guild_id, interaction.user.id, clone_id=clone_id)
        if bal["balance"] < item["price"]:
            msg = await tr("❌ You can't afford that.", lang)
            await interaction.followup.send(msg, ephemeral=True)
            return
        new_balance = await db.adjust_economy_balance(
            interaction.guild_id, interaction.user.id, -item["price"], f"shop_buy:{item['item_id']}", clone_id=clone_id
        )
        await db.grant_inventory_item(interaction.guild_id, interaction.user.id, item["item_id"], clone_id=clone_id)
        role_note = ""
        if item["role_id"] and isinstance(interaction.user, discord.Member):
            role = interaction.guild.get_role(item["role_id"])
            if role:
                try:
                    await interaction.user.add_roles(role, reason=f"Purchased shop item #{item['item_id']}")
                    role_note = await tr(" and received **{role}**", lang, role=role.name)
                except discord.Forbidden:
                    role_note = await tr(" (couldn't grant the role — check my role position)", lang)
        msg = await tr(
            "✅ Bought **{name}**{role_note}. Balance: **{balance} {symbol}**", lang,
            name=item["name"], role_note=role_note, balance=new_balance, symbol=cfg["currency_symbol"]
        )
        card = EconomyCardView("Purchase complete", [msg], discord.Color.green(), buttons=["shop", "balance"])
        await interaction.followup.send(view=card)

    @economy_group.command(name="buy", description="Buy an item from the shop")
    @app_commands.describe(item_id="The item's # from /shop list")
    async def buy(self, interaction: discord.Interaction, item_id: int):
        await self.buy_item(interaction, item_id)

    @shop_group.command(name="add", description="Add an item to the shop (admin)")
    @app_commands.describe(image="Small png/jpeg for the item (max 200KB)")
    async def shop_add(self, interaction: discord.Interaction, name: str, price: app_commands.Range[int, 1, None],
                        description: str = None, role: discord.Role = None, image: discord.Attachment = None):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        if not _require_perm(interaction, "manage_guild"):
            await _deny(interaction, "Manage Server", lang)
            return
        image_data, image_ext = await self._read_shop_image(interaction, image, lang)
        if image is not None and image_data is None:
            return  # _read_shop_image already sent the rejection message
        item_id = await db.add_shop_item(
            interaction.guild_id, name, description, price, interaction.user.id,
            role_id=role.id if role else None, clone_id=_clone_id_of(interaction),
            image_data=image_data, image_ext=image_ext
        )
        msg = await tr("✅ Added **{name}** as item #{item_id}.", lang, name=name, item_id=item_id)
        await interaction.followup.send(msg, ephemeral=True)

    async def _read_shop_image(self, interaction: discord.Interaction, image, lang: str):
        """Shared by the /shop add command and the button-driven admin
        wizard in _views_economy.py. Returns (None, None) for no image, or
        sends a rejection and returns (None, None) too — callers check
        `image is not None` themselves to tell "no image given" apart from
        "image given but rejected"."""
        if image is None:
            return None, None
        ext = (image.filename.rsplit(".", 1)[-1] if "." in image.filename else "").lower()
        if ext not in ("png", "jpg", "jpeg") or (image.content_type or "") not in (
            "image/png", "image/jpeg", "image/jpg"
        ):
            msg = await tr("❌ Item image must be a png or jpeg.", lang)
            await interaction.followup.send(msg, ephemeral=True)
            return None, None
        if image.size > MAX_SHOP_IMAGE_BYTES:
            msg = await tr(
                "❌ That image is too big — keep it under **{kb}KB**.", lang,
                kb=MAX_SHOP_IMAGE_BYTES // 1024
            )
            await interaction.followup.send(msg, ephemeral=True)
            return None, None
        return await image.read(), ext

    @shop_group.command(name="remove", description="Remove an item from the shop (admin)")
    async def shop_remove(self, interaction: discord.Interaction, item_id: int):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        if not _require_perm(interaction, "manage_guild"):
            await _deny(interaction, "Manage Server", lang)
            return
        ok = await db.remove_shop_item(interaction.guild_id, item_id, clone_id=_clone_id_of(interaction))
        msg = await tr("✅ Removed.", lang) if ok else await tr("No such item.", lang)
        await interaction.followup.send(msg, ephemeral=True)

    # ── trade board (cross-server browsing, same-guild settlement) ────────
    async def send_trade_board(self, interaction: discord.Interaction, all_servers: bool = True):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        clone_id = _clone_id_of(interaction)
        cfg = await db.get_economy_config(interaction.guild_id, clone_id=clone_id)
        rows = await db.get_trade_listings(
            guild_id=None if all_servers else interaction.guild_id, clone_id=clone_id, limit=10
        )
        if not rows:
            msg = await tr("📋 No active trade listings.", lang)
            await interaction.followup.send(msg, ephemeral=True)
            return
        lines = []
        for r in rows:
            here = r["guild_id"] == interaction.guild_id
            where = "" if here else f" -# on {r['guild_name'] or 'another server'}"
            lines.append(f"**#{r['id']} — {r['item_name']}** — {r['price']} {cfg['currency_symbol']}{where}")
        title = "🌍 Trade board — all servers" if all_servers else "📋 Trade board — this server"
        card = EconomyCardView(title, lines, discord.Color.teal(),
                                buttons=["trade_buy", "trade_sell", "balance"])
        await interaction.followup.send(view=card)

    async def sell_on_trade_board(self, interaction: discord.Interaction, item_id: int, price: int):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        clone_id = _clone_id_of(interaction)
        cfg = await db.get_economy_config(interaction.guild_id, clone_id=clone_id)
        listing_id = await db.list_item_for_trade(
            interaction.guild_id, clone_id, interaction.guild.name, interaction.user.id, item_id, price
        )
        if listing_id is None:
            msg = await tr("❌ You don't own that item.", lang)
            await interaction.followup.send(msg, ephemeral=True)
            return
        msg = await tr(
            "✅ Listed as **#{listing_id}** for {price} {symbol}. Visible server-wide.", lang,
            listing_id=listing_id, price=price, symbol=cfg["currency_symbol"]
        )
        card = EconomyCardView("Listed for trade", [msg], discord.Color.teal(), buttons=["trade", "shop"])
        await interaction.followup.send(view=card)

    async def buy_trade_listing(self, interaction: discord.Interaction, listing_id: int):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        clone_id = _clone_id_of(interaction)
        cfg = await db.get_economy_config(interaction.guild_id, clone_id=clone_id)
        result = await db.buy_trade_listing(listing_id, interaction.user.id, interaction.guild_id, clone_id)
        err = result.get("error")
        if err == "not_found":
            msg = await tr("❌ That listing is no longer available.", lang)
        elif err == "own_listing":
            msg = await tr("❌ You can't buy your own listing — cancel it instead.", lang)
        elif err == "wrong_guild":
            msg = await tr(
                "❌ That listing is on **{guild}** — buy it from there; each server's coins are separate.", lang,
                guild=result.get("guild_name") or "another server"
            )
        elif err == "insufficient_funds":
            msg = await tr("❌ You can't afford that.", lang)
        else:
            msg = await tr(
                "✅ Bought item #{item_id} for {price} {symbol}. Balance: **{balance} {symbol}**", lang,
                item_id=result["item_id"], price=result["price"], symbol=cfg["currency_symbol"],
                balance=result["buyer_balance"]
            )
            card = EconomyCardView("Trade complete", [msg], discord.Color.green(), buttons=["trade", "balance"])
            await interaction.followup.send(view=card)
            return
        await interaction.followup.send(msg, ephemeral=True)

    # ── coin-for-coin sends: 5% tax + daily cap (see module-level consts) ──
    async def send_coins(self, interaction: discord.Interaction, target: discord.Member, amount: int):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        if target.id == interaction.user.id or target.bot or amount <= 0:
            msg = await tr("❌ Invalid send.", lang)
            await interaction.followup.send(msg, ephemeral=True)
            return
        clone_id = _clone_id_of(interaction)
        cfg = await db.get_economy_config(interaction.guild_id, clone_id=clone_id)
        sender_bal = await db.get_economy_balance(interaction.guild_id, interaction.user.id, clone_id=clone_id)
        if sender_bal.get("last_daily_at") is None:
            msg = await tr("❌ Claim your daily reward at least once before sending coins.", lang)
            await interaction.followup.send(msg, ephemeral=True)
            return
        daily_cap = cfg["daily_amount"] * SEND_DAILY_CAP_MULTIPLIER
        try:
            result = await db.send_coins(
                interaction.guild_id, clone_id, interaction.user.id, target.id, amount,
                daily_cap=daily_cap, tax_rate=SEND_TAX_RATE
            )
        except db.InsufficientFunds:
            msg = await tr("❌ You can't afford that.", lang)
            await interaction.followup.send(msg, ephemeral=True)
            return
        except db.DailyCapExceeded as e:
            msg = await tr(
                "❌ Daily send limit reached. You can send **{remaining} {symbol}** more today.", lang,
                remaining=max(0, e.remaining), symbol=cfg["currency_symbol"]
            )
            await interaction.followup.send(msg, ephemeral=True)
            return
        line = await tr(
            "💸 Sent **{received} {symbol}** to {name} (**{tax}** taxed). Balance: **{balance} {symbol}**", lang,
            received=result["received"], symbol=cfg["currency_symbol"], name=target.display_name,
            tax=result["tax"], balance=result["from_balance"]
        )
        card = EconomyCardView("Sent", [line], discord.Color.green(), buttons=["balance", "shop"])
        await interaction.followup.send(view=card)

    # ── ad-supported bonuses (spec §4 open question #1) ────────────────
    @economy_group.command(name="vote", description="Vote for the bot for a currency bonus")
    async def vote(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        clone_id = _clone_id_of(interaction)
        cfg = await db.get_economy_config(interaction.guild_id, clone_id=clone_id)
        if not cfg["vote_bonus_enabled"]:
            msg = await tr("Voting bonuses aren't enabled on this server.", lang)
            await interaction.followup.send(msg, ephemeral=True)
            return
        bal = await db.get_economy_balance(interaction.guild_id, interaction.user.id, clone_id=clone_id)
        elapsed = _seconds_since(bal.get("last_vote_bonus_at"))
        cooldown_seconds = cfg["vote_cooldown_hours"] * 3600
        if elapsed < cooldown_seconds:
            msg = await tr(
                "⏳ You already claimed your vote bonus. Next one in **{cooldown}**.", lang,
                cooldown=_fmt_cooldown(cooldown_seconds - elapsed)
            )
            await interaction.followup.send(msg, ephemeral=True)
            return
        title = await tr("Vote for the bot!", lang)
        no_link = await tr("(no vote link configured)", lang)
        description = await tr(
            "Vote here: {url}\n\nClick a link above, then run `/vote` again to claim your bonus.", lang,
            url=cfg["vote_url"] or no_link
        )
        new_balance = await db.adjust_economy_balance(
            interaction.guild_id, interaction.user.id, cfg["vote_bonus_amount"], "vote_bonus",
            clone_id=clone_id, cooldown_field="last_vote_bonus_at"
        )
        bonus_line = await tr(
            "**Bonus claimed:** +{amount} {symbol} — Balance: {balance} {symbol}", lang,
            amount=cfg["vote_bonus_amount"], symbol=cfg["currency_symbol"], balance=new_balance
        )
        card = EconomyCardView(title, [description, bonus_line], discord.Color.blurple(), buttons=["balance"])
        await interaction.followup.send(view=card)

    @economy_group.command(name="watchad", description="View a sponsor message for a currency bonus")
    async def watchad(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        clone_id = _clone_id_of(interaction)
        cfg = await db.get_economy_config(interaction.guild_id, clone_id=clone_id)
        if not cfg["ad_bonus_enabled"]:
            msg = await tr("Sponsor bonuses aren't enabled on this server.", lang)
            await interaction.followup.send(msg, ephemeral=True)
            return
        bal = await db.get_economy_balance(interaction.guild_id, interaction.user.id, clone_id=clone_id)
        elapsed = _seconds_since(bal.get("last_ad_bonus_at"))
        cooldown_seconds = cfg["ad_cooldown_hours"] * 3600
        if elapsed < cooldown_seconds:
            msg = await tr(
                "⏳ Next sponsor bonus available in **{cooldown}**.", lang,
                cooldown=_fmt_cooldown(cooldown_seconds - elapsed)
            )
            await interaction.followup.send(msg, ephemeral=True)
            return
        # Note: ad_embed_title/description are guild-admin-authored (set via
        # /ecoconfig ads), not part of the bot's own UI copy, so they're
        # sent as-is — translating another admin's freeform sponsor text
        # without their say-so isn't this cog's call to make.
        default_title = await tr("Sponsored", lang)
        default_description = await tr("Thanks for supporting the server!", lang)
        title = cfg["ad_embed_title"] or default_title
        description = cfg["ad_embed_description"] or default_description
        if cfg["ad_embed_url"]:
            description += f"\n{cfg['ad_embed_url']}"
        new_balance = await db.adjust_economy_balance(
            interaction.guild_id, interaction.user.id, cfg["ad_bonus_amount"], "ad_bonus",
            clone_id=clone_id, cooldown_field="last_ad_bonus_at"
        )
        bonus_line = await tr(
            "**Bonus claimed:** +{amount} {symbol} — Balance: {balance} {symbol}", lang,
            amount=cfg["ad_bonus_amount"], symbol=cfg["currency_symbol"], balance=new_balance
        )
        card = EconomyCardView(title, [description, bonus_line], discord.Color.blurple(), buttons=["balance"])
        await interaction.followup.send(view=card)

    # ── admin config ─────────────────────────────────────────────────────
    eco_group = app_commands.guild_only()(app_commands.Group(name="ecoconfig", description="Configure this server's economy"))

    @eco_group.command(name="setup", description="Set up the economy with a guided step-by-step wizard")
    async def eco_setup_wizard(self, interaction: discord.Interaction):
        await interaction.response.defer()
        lang = await get_lang(interaction)
        if not _require_perm(interaction, "manage_guild"):
            await _deny(interaction, "Manage Server", lang)
            return
        clone_id = _clone_id_of(interaction)
        config = await db.get_economy_config(interaction.guild_id, clone_id=clone_id)
        view = build_economy_wizard_view(interaction.guild_id, clone_id, interaction.user.id, config)
        await interaction.followup.send(view=view)
        sent = await interaction.original_response()
        await remember_economy_wizard_message(interaction.guild_id, clone_id, interaction.user.id, sent.channel.id, sent.id)

    @eco_group.command(name="currency", description="Set the currency name and symbol")
    async def eco_currency(self, interaction: discord.Interaction, name: str, symbol: str):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        if not _require_perm(interaction, "manage_guild"):
            await _deny(interaction, "Manage Server", lang)
            return
        await db.set_economy_config(interaction.guild_id, clone_id=_clone_id_of(interaction),
                                     currency_name=name, currency_symbol=symbol)
        await refresh_economy_wizard(interaction.client, interaction.guild_id, clone_id=_clone_id_of(interaction))
        msg = await tr("✅ Currency set to **{symbol} {name}**.", lang, symbol=symbol, name=name)
        await interaction.followup.send(msg, ephemeral=True)

    @eco_group.command(name="vote", description="Configure the vote-bonus (top.gg-style) mechanic")
    async def eco_vote(self, interaction: discord.Interaction, enabled: bool, amount: int = None,
                        cooldown_hours: int = None, vote_url: str = None):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        if not _require_perm(interaction, "manage_guild"):
            await _deny(interaction, "Manage Server", lang)
            return
        fields = {"vote_bonus_enabled": enabled}
        if amount is not None:
            fields["vote_bonus_amount"] = amount
        if cooldown_hours is not None:
            fields["vote_cooldown_hours"] = cooldown_hours
        if vote_url is not None:
            fields["vote_url"] = vote_url
        await db.set_economy_config(interaction.guild_id, clone_id=_clone_id_of(interaction), **fields)
        await refresh_economy_wizard(interaction.client, interaction.guild_id, clone_id=_clone_id_of(interaction))
        msg = await tr("✅ Vote bonus settings updated.", lang)
        await interaction.followup.send(msg, ephemeral=True)

    @eco_group.command(name="ads", description="Configure the sponsor-embed bonus mechanic")
    async def eco_ads(self, interaction: discord.Interaction, enabled: bool, amount: int = None,
                       cooldown_hours: int = None, title: str = None, description: str = None, url: str = None):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        if not _require_perm(interaction, "manage_guild"):
            await _deny(interaction, "Manage Server", lang)
            return
        fields = {"ad_bonus_enabled": enabled}
        if amount is not None:
            fields["ad_bonus_amount"] = amount
        if cooldown_hours is not None:
            fields["ad_cooldown_hours"] = cooldown_hours
        if title is not None:
            fields["ad_embed_title"] = title
        if description is not None:
            fields["ad_embed_description"] = description
        if url is not None:
            fields["ad_embed_url"] = url
        await db.set_economy_config(interaction.guild_id, clone_id=_clone_id_of(interaction), **fields)
        await refresh_economy_wizard(interaction.client, interaction.guild_id, clone_id=_clone_id_of(interaction))
        msg = await tr("✅ Sponsor bonus settings updated.", lang)
        await interaction.followup.send(msg, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(EconomyCog(bot))
