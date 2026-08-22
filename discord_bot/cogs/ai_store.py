"""
AI Store — Discord commands.

Buyers spend credits (bought with GHS via Paystack) chatting with Claude,
GPT, or Gemini — always on the PLATFORM'S OWN API keys, never a personal
subscription. Sellers list "personas" for placement/discovery only; there
is no revenue share (see config.py) — sellers get exposure/traffic from a
listing, not a cut of buyer spend, so no payout/cashout system exists here.

Persistent views (TopupPayView, BoostPayView, VerifyCreditsView,
VerifyBoostView) use fixed custom_ids and are registered once on bot
startup in discord_bot/bot.py, matching the pattern in discord_bot/views.py.

Flow:
  /aistore credits            — check wallet balance
  /aistore topup               — buy credits with GHS (Paystack)
  /aistore newchat provider — start a fresh AI Store conversation
  /aistore ask message          — talk in your active conversation
  /aistore endchat          — end active AI Store conversation
  /aistore history              — recent wallet transactions
  /sessions              — recent conversations
  /aistore browse search category — browse seller listings, blue buttons to start chatting
  /aistore sell name description system_prompt provider category — list a persona (auto-reviewed)
  /aistore mylistings            — seller's own listings
  /aistore boost listing_id       — pay to feature/top a listing
  /aistore flagbad                — report last response as broken, request refund
  /aistore reviewqueue [approve|reject] [listing_id]   — Store Admin: listing moderation queue
  /aistore refundqueue [approve|deny] [refund_id]      — Store Admin: refund queue
"""

import logging
import asyncio
import os

import discord
from discord import app_commands
from discord.ext import commands

from database import db, InsufficientCreditsError
from payments import resolve_gateway, resolve_gateway_for_provider, gateway_charge_amount, charge_error_message
from modules.ai_store_providers import list_providers, list_models, MODELS
from modules.ai_store_chatflow import run_chat_turn
from modules.ai_store_moderation import review_listing
import config

logger = logging.getLogger(__name__)

TOPUP_PAYMENT_TYPE = "ai_store_topup"
BOOST_PAYMENT_TYPE = "ai_store_boost"

AUTO_REFUND_MIN_LENGTH = 5  # responses shorter than this are treated as obvious failures


def _clone_id_of(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None)


def _is_store_admin(interaction: discord.Interaction) -> bool:
    role_id = os.getenv("STORE_ADMIN_ROLE_ID")
    if not role_id:
        return False
    member = interaction.user if isinstance(interaction.user, discord.Member) else None
    if member is None or member.roles is None:
        return False
    return any(str(r.id) == role_id for r in member.roles)


# ─────────────────────────────────────────────────────────────────────
# Persistent payment views
# ─────────────────────────────────────────────────────────────────────

async def _verify_topup(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    user = interaction.user

    pending = await db.get_latest_pending_payment(user.id, TOPUP_PAYMENT_TYPE)
    if not pending:
        await interaction.followup.send("No pending top-up found — tap **Top Up** first.", ephemeral=True)
        return

    reference = pending["paystack_reference"]
    clone_id = _clone_id_of(interaction) or 0
    gateway, api_key = await resolve_gateway_for_provider(clone_id, pending.get("provider") or "paystack", platform="discord")
    result = await asyncio.to_thread(gateway.verify_payment, reference, api_key=api_key)

    if result and result.get("status") == "success":
        await db.mark_payment_paid(reference)
        amount_ghs = float(pending["amount"])
        credits = amount_ghs * config.AI_STORE_CREDIT_RATE_PER_GHS
        new_balance = await db.ai_store_add_credits(user.id, credits, "topup", {"reference": reference, "ghs": amount_ghs})
        await interaction.followup.send(
            f"✅ Payment confirmed — **{credits:.0f} credits** added. New balance: **{new_balance:.2f}**.",
            ephemeral=True,
        )
    else:
        await interaction.followup.send("Payment not confirmed yet — wait a few seconds and tap Verify again.", ephemeral=True)


async def _verify_boost(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    user = interaction.user

    pending = await db.get_latest_pending_payment(user.id, BOOST_PAYMENT_TYPE)
    if not pending:
        await interaction.followup.send("No pending boost payment found.", ephemeral=True)
        return

    reference = pending["paystack_reference"]
    clone_id = _clone_id_of(interaction) or 0
    gateway, api_key = await resolve_gateway_for_provider(clone_id, pending.get("provider") or "paystack", platform="discord")
    result = await asyncio.to_thread(gateway.verify_payment, reference, api_key=api_key)

    if result and result.get("status") == "success":
        await db.mark_payment_paid(reference)
        listing_id = pending["group_id"]  # reused column to carry listing_id
        tier = "top" if float(pending["amount"]) >= config.AI_STORE_TOP_FEE_GHS else "featured"
        await db.ai_store_set_placement(listing_id, tier, days=30)
        await interaction.followup.send(f"✅ Boost active — listing is now **{tier}** placement for 30 days.", ephemeral=True)
    else:
        await interaction.followup.send("Payment not confirmed yet — wait a few seconds and tap Verify again.", ephemeral=True)


class VerifyCreditsView(discord.ui.View):
    """Persistent 'I've Paid — Verify' button for credit top-ups — fixed
    custom_id, registered once in discord_bot/bot.py so it survives restarts."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ I've Paid — Verify", style=discord.ButtonStyle.success, custom_id="ai_store_topup_verify")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _verify_topup(interaction)


class VerifyCreditsButton(discord.ui.Button):
    """Non-persistent twin used on the ephemeral pay message (which already
    has its own timeout) — same underlying logic as VerifyCreditsView."""

    def __init__(self):
        super().__init__(label="✅ I've Paid — Verify", style=discord.ButtonStyle.success)

    async def callback(self, interaction: discord.Interaction):
        await _verify_topup(interaction)


class TopupPayView(discord.ui.View):
    def __init__(self, payment_link: str):
        super().__init__(timeout=300)
        self.add_item(discord.ui.Button(label="💳 Pay Now", url=payment_link, style=discord.ButtonStyle.link))
        self.add_item(VerifyCreditsButton())


class VerifyBoostView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ I've Paid — Verify", style=discord.ButtonStyle.success, custom_id="ai_store_boost_verify")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _verify_boost(interaction)


class BoostVerifyButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="✅ I've Paid — Verify", style=discord.ButtonStyle.success)

    async def callback(self, interaction: discord.Interaction):
        await _verify_boost(interaction)


# ─────────────────────────────────────────────────────────────────────
# Persistent menu — a button backup for people who'd rather tap than
# type. Fixed custom_ids, registered once on startup (discord_bot/bot.py)
# so the buttons keep working on old messages after a restart, same
# pattern as PremiumPayView. Posted via /aistoremenu (see cog below) or
# can be pinned wherever makes sense (welcome message, a store channel).
# ─────────────────────────────────────────────────────────────────────

class AIStoreMenuView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="💰 Balance", style=discord.ButtonStyle.primary, custom_id="ai_store_menu_balance")
    async def balance_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        balance = await db.ai_store_get_balance(interaction.user.id)
        ghs = balance / config.AI_STORE_CREDIT_RATE_PER_GHS
        embed = discord.Embed(color=discord.Color.blue(), title="Your Balance",
                               description=f"**{balance:.2f} credits**\n≈ GHS {ghs:.2f}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="💳 Top Up", style=discord.ButtonStyle.primary, custom_id="ai_store_menu_topup")
    async def topup_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            f"Choose an amount (min GHS {config.AI_STORE_MIN_TOPUP_GHS}):", view=TopupChoiceView(), ephemeral=True
        )

    @discord.ui.button(label="🏪 Browse Store", style=discord.ButtonStyle.primary, custom_id="ai_store_menu_browse")
    async def browse_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        results = await db.ai_store_search_listings(interaction.guild_id, "", None, 10)
        if not results:
            await interaction.followup.send("No listings yet — check back soon, or `/aistore sell` to list one.", ephemeral=True)
            return
        lines = []
        for l in results:
            badge = "⭐ " if l["placement_tier"] == "top" else "🔹 " if l["placement_tier"] == "featured" else ""
            lines.append(f"{badge}**{l['name']}**\n{l['description']}\n`{l['provider']}/{l['model']}` · ID: `{l['id']}`")
        embed = discord.Embed(color=discord.Color.blue(), title="AI Store — Assistant Listings", description="\n\n".join(lines))
        await interaction.followup.send(embed=embed, view=ListingPickerView(results, interaction.guild_id), ephemeral=True)

    @discord.ui.button(label="💬 New Chat", style=discord.ButtonStyle.primary, custom_id="ai_store_menu_newchat")
    async def newchat_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        provider_view = discord.ui.View(timeout=120)
        for p in list_providers():
            provider_view.add_item(ProviderMenuButton(p, interaction.guild_id))
        await interaction.response.send_message("Pick a provider:", view=provider_view, ephemeral=True)


class ProviderMenuButton(discord.ui.Button):
    """Non-persistent — only lives for the /aistoremenu 'New Chat' flow,
    same role /storenewchat's provider Choice option plays for the slash command."""

    def __init__(self, provider: str, guild_id):
        super().__init__(label=provider, style=discord.ButtonStyle.primary)
        self.provider = provider
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content=f"Pick a model for **{self.provider}**:",
            view=ModelPickerView(self.provider, self.guild_id),
        )


class ModelPickerView(discord.ui.View):
    """Shows up to 5 model buttons for the chosen provider."""

    def __init__(self, provider: str, guild_id):
        super().__init__(timeout=120)
        for key, label in list_models(provider)[:5]:
            self.add_item(ModelButton(provider, key, label, guild_id))


class ModelButton(discord.ui.Button):
    def __init__(self, provider, model_key, label, guild_id):
        super().__init__(label=label.split(" — ")[0][:80], style=discord.ButtonStyle.primary)
        self.provider = provider
        self.model_key = model_key
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await db.ai_store_create_session(interaction.user.id, self.guild_id, self.provider, self.model_key)
        await interaction.edit_original_response(
            content=f"New chat started with **{self.provider}/{self.model_key}**. Use `/ask` to talk.", view=None
        )


class ListingPickerView(discord.ui.View):
    def __init__(self, listings: list, guild_id):
        super().__init__(timeout=120)
        for listing in listings[:5]:
            self.add_item(ListingButton(listing, guild_id))


class ListingButton(discord.ui.Button):
    def __init__(self, listing: dict, guild_id):
        super().__init__(label=listing["name"][:80], style=discord.ButtonStyle.primary)
        self.listing = listing
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        listing = await db.ai_store_get_listing(self.listing["id"])
        if not listing or listing["review_status"] != "approved" or not listing["active"]:
            await interaction.edit_original_response(content="That listing isn't available right now.", view=None)
            return
        await db.ai_store_create_session(
            interaction.user.id, self.guild_id, listing["provider"], listing["model"], listing_id=listing["id"]
        )
        await interaction.edit_original_response(
            content=f"Started a chat with **{listing['name']}**. Use `/ask` to talk.", view=None
        )


class TopupChoiceView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="GHS 10", style=discord.ButtonStyle.primary)
    async def ghs10(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _start_topup(interaction, 10)

    @discord.ui.button(label="GHS 20", style=discord.ButtonStyle.primary)
    async def ghs20(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _start_topup(interaction, 20)

    @discord.ui.button(label="GHS 50", style=discord.ButtonStyle.primary)
    async def ghs50(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _start_topup(interaction, 50)


async def _start_topup(interaction: discord.Interaction, amount_ghs: float):
    await interaction.response.defer(ephemeral=True, thinking=True)
    user = interaction.user
    clone_id = _clone_id_of(interaction) or 0
    gateway, api_key, _provider = await resolve_gateway(clone_id, platform="discord")
    email = f"user_{user.id}@animebot.com"

    charge = gateway_charge_amount(_provider, amount_ghs)
    if charge.get("error"):
        await interaction.followup.send(charge_error_message(charge), ephemeral=True)
        return

    payment_result = await asyncio.to_thread(
        gateway.initialize_payment,
        email, charge["amount_minor_units"], user.id, f"AIStoreTopup_{user.id}",
        payment_type=TOPUP_PAYMENT_TYPE, extra_metadata={"provider": "discord"}, api_key=api_key,
        currency=charge["currency"],
    )
    if not payment_result or payment_result.get("status") != "success":
        await interaction.followup.send("Couldn't start a payment right now — try again shortly.", ephemeral=True)
        return

    reference = payment_result["reference"]
    await db.log_payment(user.id, amount_ghs, reference, status="pending", payment_type=TOPUP_PAYMENT_TYPE, provider=_provider)

    credits = amount_ghs * config.AI_STORE_CREDIT_RATE_PER_GHS
    charged_amount_display = (
        f"GHS {amount_ghs:g}" if charge["currency"] == "GHS"
        else f"{charge['amount_minor_units'] / 100:.2f} {charge['currency'].upper()} (converted from GHS {amount_ghs:g})"
    )
    embed = discord.Embed(
        title="💳 Top Up Credits",
        description=f"**{charged_amount_display}** → **{credits:.0f} credits**\n\nTap **Pay Now**, complete checkout, then tap **Verify**.",
        color=discord.Color.blue(),
    )
    view = TopupPayView(payment_result["authorization_url"])
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


# ─────────────────────────────────────────────────────────────────────
# The cog
# ─────────────────────────────────────────────────────────────────────

class AIStoreCog(commands.Cog):
    # All AI Store commands live under one top-level "/aistore" group instead
    # of 14 separate top-level commands — Discord caps global commands at
    # 100 total, and a Group only costs 1 of those regardless of how many
    # subcommands it holds (see discord_bot/bot.py's CommandLimitReached
    # notes for why this matters).
    aistore = app_commands.Group(name="aistore", description="AI Store — chat with paid AI personas")

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @aistore.command(name="credits", description="Check your AI Store credit balance")
    async def credits_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        balance = await db.ai_store_get_balance(interaction.user.id)
        ghs = balance / config.AI_STORE_CREDIT_RATE_PER_GHS
        embed = discord.Embed(color=discord.Color.blue(), title="Your Balance",
                               description=f"**{balance:.2f} credits**\n≈ GHS {ghs:.2f}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @aistore.command(name="topup", description="Buy AI Store credits with GHS")
    async def topup_cmd(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            f"Choose an amount (min GHS {config.AI_STORE_MIN_TOPUP_GHS}):", view=TopupChoiceView(), ephemeral=True
        )

    @aistore.command(name="newchat", description="Start a new AI Store conversation")
    @app_commands.describe(provider="Which AI provider")
    @app_commands.choices(provider=[app_commands.Choice(name=p, value=p) for p in list_providers()])
    async def newchat_cmd(self, interaction: discord.Interaction, provider: app_commands.Choice[str]):
        await interaction.response.send_message(
            f"Pick a model for **{provider.value}**:",
            view=ModelPickerView(provider.value, interaction.guild_id),
            ephemeral=True,
        )

    @aistore.command(name="ask", description="Send a message in your active conversation")
    @app_commands.describe(message="Your message")
    async def ask_cmd(self, interaction: discord.Interaction, message: str):
        user = interaction.user
        session = await db.ai_store_get_active_session(user.id)
        if not session:
            await interaction.followup.send("No active chat — use `/aistore newchat` first.", ephemeral=True)
            return

        allowed, retry_after = await db.ai_store_check_and_consume_rate_limit(user.id)
        if not allowed:
            await interaction.followup.send(f"Sending too fast — try again in {retry_after}s.", ephemeral=True)
            return

        await interaction.response.defer()
        try:
            result = await run_chat_turn(
                user.id, session["id"], session["provider"], session["model"], message,
                listing_id=session.get("listing_id"),
            )
            embed = discord.Embed(color=discord.Color.blue(), description=result["text"][:4000])
            embed.set_footer(
                text=f"{result['cost_credits']:.2f} credits used · {result['balance_after']:.2f} remaining · "
                     f"flag ID: {result['message_id']}"
            )
            await interaction.followup.send(embed=embed)
        except InsufficientCreditsError as e:
            await interaction.followup.send(
                f"Not enough credits (needs ~{e.needed:.2f}, you have {e.have:.2f}). Use `/aistore topup`."
            )
        except Exception as e:
            logger.error(f"[ai_store] /ask failed: {e}")
            await interaction.followup.send("Something went wrong reaching the AI provider — you were not charged. Try again shortly.")

    @aistore.command(name="endchat", description="End your active AI Store conversation")
    async def endchat_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        session = await db.ai_store_get_active_session(interaction.user.id)
        if not session:
            await interaction.followup.send("No active chat to end.", ephemeral=True)
            return
        await db.ai_store_end_session(session["id"])
        await interaction.followup.send("Chat ended. Start a new one with `/aistore newchat`.", ephemeral=True)

    @aistore.command(name="history", description="View your recent AI Store transactions")
    async def history_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        rows = await db.ai_store_get_history(interaction.user.id, 10)
        lines = [f"{'+' if r['type']=='topup' else '-'}{float(r['amount']):.2f} · {r['type']} · balance {float(r['balance_after']):.2f}" for r in rows]
        embed = discord.Embed(color=discord.Color.blue(), title="Recent Transactions",
                               description="\n".join(lines) or "No transactions yet.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @aistore.command(name="browse", description="Browse AI assistant listings")
    @app_commands.describe(search="Search by name or description", category="Filter by category")
    async def aistore_cmd(self, interaction: discord.Interaction, search: str = "", category: str = None):
        await interaction.response.defer(ephemeral=True)
        results = await db.ai_store_search_listings(interaction.guild_id, search, category, 10)
        if not results:
            await interaction.followup.send("No listings found.", ephemeral=True)
            return
        lines = []
        for l in results:
            badge = "⭐ " if l["placement_tier"] == "top" else "🔹 " if l["placement_tier"] == "featured" else ""
            lines.append(f"{badge}**{l['name']}**\n{l['description']}\n`{l['provider']}/{l['model']}` · ID: `{l['id']}`")
        embed = discord.Embed(color=discord.Color.blue(), title="AI Store — Assistant Listings", description="\n\n".join(lines))
        await interaction.followup.send(embed=embed, view=ListingPickerView(results, interaction.guild_id), ephemeral=True)

    @aistore.command(name="sell", description="List an assistant persona in the AI Store")
    @app_commands.describe(
        name="Listing name", description="What it does", system_prompt="Persona instructions",
        provider="Underlying AI provider", category="Category (default: general)",
        scope="This server only, or platform-wide"
    )
    @app_commands.choices(
        provider=[app_commands.Choice(name=p, value=p) for p in list_providers()],
        scope=[app_commands.Choice(name="This server only", value="guild"), app_commands.Choice(name="Platform-wide", value="platform")],
    )
    async def sell_cmd(self, interaction: discord.Interaction, name: str, description: str, system_prompt: str,
                        provider: app_commands.Choice[str], category: str = "general",
                        scope: app_commands.Choice[str] = None):
        await interaction.response.defer(ephemeral=True)
        default_model = list(MODELS[provider.value].keys())[0]
        guild_id = interaction.guild_id if (scope is None or scope.value == "guild") else None

        listing_id = await db.ai_store_create_listing(
            interaction.user.id, guild_id, name, description, category, system_prompt, provider.value, default_model
        )

        review = await review_listing(name, description, system_prompt, category)
        await db.ai_store_set_review_result(listing_id, review["status"], review["reason"])

        if review["status"] == "approved":
            embed = discord.Embed(
                title=f"{name} — approved",
                description=f"Passed automated review and is now live in `/aistore browse`.",
                color=discord.Color.green(),
            )
            embed.set_footer(text=f"Listing ID {listing_id}")
        elif review["status"] == "rejected":
            embed = discord.Embed(
                title=f"{name} — rejected",
                description=f"{review['reason']}\n\nEdit the persona and try `/aistore sell` again.",
                color=discord.Color.red(),
            )
        else:
            embed = discord.Embed(
                title=f"{name} — needs human review",
                description=f"{review['reason']}\n\nWon't appear in `/aistore browse` until approved.",
                color=discord.Color.orange(),
            )
            embed.set_footer(text=f"Listing ID {listing_id}")

        await interaction.followup.send(embed=embed, ephemeral=True)

    @aistore.command(name="mylistings", description="View your AI Store listings")
    async def mylistings_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        listings = await db.ai_store_list_seller_listings(interaction.user.id)
        if not listings:
            await interaction.followup.send("You don't have any listings yet. Use `/aistore sell` to create one.", ephemeral=True)
            return
        lines = [f"**{l['name']}** (`{l['id']}`) — {l['placement_tier']} · {l['review_status']} · {l['uses_count']} uses" for l in listings]
        embed = discord.Embed(color=discord.Color.blue(), title="Your Listings", description="\n".join(lines))
        await interaction.followup.send(embed=embed, ephemeral=True)

    @aistore.command(name="boost", description="Pay to boost a listing's placement")
    @app_commands.describe(listing_id="Listing ID")
    async def boost_cmd(self, interaction: discord.Interaction, listing_id: int):
        await interaction.response.defer(ephemeral=True)
        listing = await db.ai_store_get_listing(listing_id)
        if not listing or listing["seller_id"] != interaction.user.id:
            await interaction.followup.send("Listing not found or not yours.", ephemeral=True)
            return

        view = discord.ui.View(timeout=60)
        view.add_item(BoostTierButton(listing_id, "featured", config.AI_STORE_FEATURED_FEE_GHS))
        view.add_item(BoostTierButton(listing_id, "top", config.AI_STORE_TOP_FEE_GHS))
        await interaction.followup.send(f"Choose a placement tier for **{listing['name']}**:", view=view, ephemeral=True)

    @aistore.command(name="flagbad", description="Report your last AI response as broken and request a refund")
    async def flagbad_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        session = await db.ai_store_get_active_session(interaction.user.id)
        if not session:
            await interaction.followup.send("No active chat to flag a message from.", ephemeral=True)
            return
        last_msg = await db.ai_store_get_last_assistant_message(session["id"])
        if not last_msg:
            await interaction.followup.send("No response found to flag yet.", ephemeral=True)
            return

        is_obvious_failure = not last_msg["content"] or len(last_msg["content"].strip()) < AUTO_REFUND_MIN_LENGTH
        refund_id, status = await db.ai_store_file_refund_request(
            interaction.user.id, last_msg["id"], session["id"], float(last_msg["cost_credits"]),
            "buyer reported broken/unusable response", auto_approve=is_obvious_failure,
        )
        if status == "auto_approved":
            await db.ai_store_add_credits(interaction.user.id, float(last_msg["cost_credits"]), "refund",
                                           {"refund_request_id": refund_id, "auto": True})
            await interaction.followup.send(
                f"That response looked broken — **{float(last_msg['cost_credits']):.2f} credits** refunded automatically.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"Refund request `{refund_id}` submitted for review by a Store Admin.", ephemeral=True
            )

    @aistore.command(name="reviewqueue", description="[Admin] View/decide listings flagged for human review")
    @app_commands.describe(action="approve or reject", listing_id="Listing ID for the action")
    async def reviewqueue_cmd(self, interaction: discord.Interaction, action: str = None, listing_id: int = None):
        await interaction.response.defer(ephemeral=True)
        if not _is_store_admin(interaction):
            await interaction.followup.send("This command is restricted to Store Admins.", ephemeral=True)
            return

        if action and listing_id:
            await db.ai_store_human_review_decision(listing_id, action.lower() == "approve")
            await interaction.followup.send(f"Listing `{listing_id}` {'approved' if action.lower()=='approve' else 'rejected'}.", ephemeral=True)
            return

        pending = await db.ai_store_get_pending_reviews()
        if not pending:
            await interaction.followup.send("Review queue is empty.", ephemeral=True)
            return
        lines = [f"**{l['name']}** (`{l['id']}`)\nFlagged: {l['review_reason']}\nPrompt: {l['system_prompt'][:200]}" for l in pending]
        embed = discord.Embed(color=discord.Color.blue(), title="Pending Human Review", description="\n\n".join(lines))
        embed.set_footer(text="Use /aistore reviewqueue action:approve|reject listing_id:<id>")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @aistore.command(name="refundqueue", description="[Admin] Review pending refund requests")
    @app_commands.describe(action="approve or deny", refund_id="Refund request ID")
    async def refundqueue_cmd(self, interaction: discord.Interaction, action: str = None, refund_id: int = None):
        await interaction.response.defer(ephemeral=True)
        if not _is_store_admin(interaction):
            await interaction.followup.send("This command is restricted to Store Admins.", ephemeral=True)
            return

        if action and refund_id:
            req = await db.ai_store_decide_refund(refund_id, action.lower() == "approve")
            if not req:
                await interaction.followup.send("Refund request not found or already decided.", ephemeral=True)
                return
            if action.lower() == "approve":
                await db.ai_store_add_credits(req["user_id"], float(req["amount_credits"]), "refund", {"refund_request_id": refund_id})
            await interaction.followup.send(f"Refund `{refund_id}` {'approved and credited' if action.lower()=='approve' else 'denied'}.", ephemeral=True)
            return

        pending = await db.ai_store_get_pending_refunds()
        if not pending:
            await interaction.followup.send("No pending refund requests.", ephemeral=True)
            return
        lines = [f"`{r['id']}` — {float(r['amount_credits']):.2f} credits — {r['reason']}" for r in pending]
        embed = discord.Embed(color=discord.Color.blue(), title="Pending Refund Requests", description="\n".join(lines))
        embed.set_footer(text="Use /aistore refundqueue action:approve|deny refund_id:<id>")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @aistore.command(name="menu", description="Post a button menu for the AI Store (backup for /commands)")
    async def aistoremenu_cmd(self, interaction: discord.Interaction):
        """Posts the persistent button menu. Buttons use fixed custom_ids and
        the view is re-registered on every startup (discord_bot/bot.py), so
        this message keeps working after restarts — pin it in a store
        channel, or repost with this command whenever it's handy."""
        embed = discord.Embed(
            color=discord.Color.blue(),
            title="🤖 AI Store",
            description="Tap a button below, or use the matching `/command` any time — both do the same thing.",
        )
        await interaction.response.send_message(embed=embed, view=AIStoreMenuView())


class BoostTierButton(discord.ui.Button):
    def __init__(self, listing_id: int, tier: str, price_ghs: float):
        super().__init__(label=f"{tier.title()} — GHS {price_ghs:g}/30d", style=discord.ButtonStyle.primary)
        self.listing_id = listing_id
        self.tier = tier
        self.price_ghs = price_ghs

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        user = interaction.user
        clone_id = _clone_id_of(interaction) or 0
        gateway, api_key, _provider = await resolve_gateway(clone_id, platform="discord")
        email = f"user_{user.id}@animebot.com"

        charge = gateway_charge_amount(_provider, self.price_ghs)
        if charge.get("error"):
            await interaction.followup.send(charge_error_message(charge), ephemeral=True)
            return

        payment_result = await asyncio.to_thread(
            gateway.initialize_payment,
            email, charge["amount_minor_units"], user.id, f"AIStoreBoost_{user.id}_{self.listing_id}",
            payment_type=BOOST_PAYMENT_TYPE, extra_metadata={"listing_id": self.listing_id, "tier": self.tier}, api_key=api_key,
            currency=charge["currency"],
        )
        if not payment_result or payment_result.get("status") != "success":
            await interaction.followup.send("Couldn't start a payment right now — try again shortly.", ephemeral=True)
            return

        reference = payment_result["reference"]
        # group_id column reused to carry listing_id — matches the pattern
        # premium.py uses group_id for; keeps schema additions minimal.
        await db.log_payment(user.id, self.price_ghs, reference, status="pending",
                              payment_type=BOOST_PAYMENT_TYPE, group_id=self.listing_id, provider=_provider)

        charged_amount_display = (
            f"GHS {self.price_ghs:g}" if charge["currency"] == "GHS"
            else f"{charge['amount_minor_units'] / 100:.2f} {charge['currency'].upper()} (converted from GHS {self.price_ghs:g})"
        )
        embed = discord.Embed(
            title=f"🚀 Boost — {self.tier.title()}",
            description=f"**{charged_amount_display}** for 30 days of {self.tier} placement.\n\nTap **Pay Now**, then **Verify**.",
            color=discord.Color.blue(),
        )
        view = discord.ui.View(timeout=300)
        view.add_item(discord.ui.Button(label="💳 Pay Now", url=payment_result["authorization_url"], style=discord.ButtonStyle.link))
        view.add_item(BoostVerifyButton())
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AIStoreCog(bot))
