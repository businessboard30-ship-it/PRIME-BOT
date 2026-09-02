"""
Slash commands for the Discord bot-cloning growth loop — Discord equivalent
of handlers/clone_bot.py, adapted for the fact that a Discord clone needs
its own always-on process rather than just a webhook routing rule (see
discord_bot/clone_manager.py's docstring for why).

Only loaded on the main bot (see discord_bot/bot.py's setup_hook) — a clone
registering further clones would need its own token to hand out, which
defeats the point.
"""

import asyncio
import csv
import io
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from database import db
from discord_clone_service import validate_bot_token, build_invite_url, set_default_install_params
from utils.crypto import secret_manager
from payments import paystack
from config import (
    DISCORD_CLONE_FEE_GHS, DISCORD_CLONE_FREE_EVERY_NTH, DISCORD_CLONE_ADMIN_IDS,
    CLONE_MONETIZATION_FEE_GHS, CLONE_MONETIZATION_DAYS, PRICE_REGISTRY,
    DISCORD_OWNER_BROADCAST_IDS, SELAR_PRODUCT_LINKS,
)
from discord_bot.cogs._views_shared import ActionButton, NavCardView, refresh_button

logger = logging.getLogger(__name__)


def _is_clone_admin(user_id: int) -> bool:
    return user_id in DISCORD_CLONE_ADMIN_IDS


async def _owes_payment(owner_id: int) -> bool:
    """False for an admin bypass, or if this registration lands on the
    every-Nth-clone-free perk (e.g. Nth=3 -> their 3rd, 6th, 9th... clone is
    free). Counts clones registered so far, so the clone ABOUT to be created
    would be existing_count + 1."""
    if _is_clone_admin(owner_id):
        return False
    if DISCORD_CLONE_FREE_EVERY_NTH > 0:
        existing = await db.count_discord_clones_by_owner(owner_id)
        if (existing + 1) % DISCORD_CLONE_FREE_EVERY_NTH == 0:
            return False
    return True


class CloneAdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # user_id -> {"clone_id", "reference"} for an in-flight monetization
        # activation payment — mirrors ImageSearchCog's _pending_unlock
        # pattern in discord_bot/cogs/image_search.py.
        self._pending_monetize: dict[int, dict] = {}

    # ── /registerclone ───────────────────────────────────────────────────
    @app_commands.command(
        name="registerclone",
        description="Register your own Discord bot token to run it as a clone of this bot",
    )
    @app_commands.describe(token="Your bot's token, from the Discord Developer Portal (Bot tab)")
    async def registerclone(self, interaction: discord.Interaction, token: str):
        # Force DM-only so a pasted token is never visible in a channel's
        # history (or to anyone with message-history access) even for a
        # split second — same reasoning as Telegram's clone flow keeping
        # token entry in a private chat.
        if interaction.guild_id is not None:
            await interaction.response.send_message(
                "For your token's safety, please send me this command in a DM instead of a server channel.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        result = await validate_bot_token(token)
        if not result["ok"]:
            await interaction.followup.send(f"❌ Couldn't validate that token: {result['error']}", ephemeral=True)
            return

        # Best-effort: make Discord's own App Directory / Discover "Add to
        # Server" button work out of the box for this clone (see
        # set_default_install_params's docstring for why this is needed).
        # Never blocks registration — a clone owner can still fix this
        # manually in the Portal if it fails.
        install_result = await set_default_install_params(token)
        if not install_result.get("ok"):
            logger.warning(
                f"[v0] couldn't set default install params for application "
                f"{result['application_id']}: {install_result.get('error')}"
            )

        owner_id = interaction.user.id
        encrypted = secret_manager.encrypt(token)

        if not await _owes_payment(owner_id):
            clone_id = await db.create_discord_clone(
                owner_id=owner_id,
                bot_token_encrypted=encrypted,
                bot_user_id=result["bot_user_id"],
                bot_username=result["bot_username"],
                application_id=result["application_id"],
            )
            await self._send_registered(interaction, result, clone_id, free=True)
            return

        # Paid path: don't create the clone yet — stash the validated token
        # and bot info, start a Paystack charge, and let
        # api/paystack_webhook.py's discord_clone case finish the job the
        # moment Paystack confirms it server-to-server. No manual "Verify"
        # step needed on this end.
        email = f"discorduser_{owner_id}@animebot.com"
        payment_result = paystack.initialize_payment(
            email,
            DISCORD_CLONE_FEE_GHS * 100,  # GHS -> pesewas
            owner_id,
            f"DiscordClone_{owner_id}",
            payment_type="discord_clone",
        )
        if not payment_result or payment_result.get("status") != "success":
            await interaction.followup.send(
                "❌ Couldn't start a payment right now — please try again shortly.", ephemeral=True
            )
            return

        reference = payment_result["reference"]
        await db.store_discord_clone_pending_payment(
            reference=reference,
            owner_id=owner_id,
            bot_token_encrypted=encrypted,
            bot_user_id=result["bot_user_id"],
            bot_username=result["bot_username"],
            application_id=result["application_id"],
        )

        view = discord.ui.View()
        view.add_item(discord.ui.Button(
            label="💳 Pay Now", url=payment_result["authorization_url"], style=discord.ButtonStyle.link
        ))
        await interaction.followup.send(
            f"💰 Registering **{result['bot_username']}** costs **GHS {DISCORD_CLONE_FEE_GHS}**.\n\n"
            f"Tap **Pay Now** and complete checkout — your clone will be created automatically "
            f"within a minute or two of payment, no need to come back and confirm. Check `/myclones` "
            f"once it's done.",
            view=view,
            ephemeral=True,
        )

    async def _send_registered(self, interaction: discord.Interaction, result: dict, clone_id: int, free: bool = False):
        invite = build_invite_url(result["application_id"])
        prefix = "✅ Registered" if not free else "✅ Registered (free clone!)"
        await interaction.followup.send(
            f"{prefix} **{result['bot_username']}** as clone `#{clone_id}`.\n\n"
            f"It'll come online within about a minute. Invite it to your server(s) here:\n{invite}\n\n"
            f"Its Discord App Directory listing is also set up to add it as a real member "
            f"(not just register commands), so \"Add to Server\" from Discover works correctly too.\n\n"
            f"Once it's in a server, an admin there can run `/createpremium` to set up its own "
            f"premium group(s) — completely separate from this bot's and from any other clone's.",
            ephemeral=True,
        )

    # ── /myclones ─────────────────────────────────────────────────────────
    @app_commands.command(name="myclones", description="List the Discord bot clones you own")
    async def myclones(self, interaction: discord.Interaction):
        await interaction.response.defer()
        clones = await db.get_discord_clones_by_owner(interaction.user.id)
        if not clones:
            await interaction.followup.send(
                "You don't have any clones yet — DM me `/registerclone` with a bot token to create one.",
                ephemeral=True,
            )
            return
        lines = []
        buttons = [refresh_button(self, "myclones")]
        for c in clones:
            heartbeat = c["last_heartbeat"].strftime("%Y-%m-%d %H:%M UTC") if c["last_heartbeat"] else "never"
            lines.append(f"**#{c['clone_id']} — {c['bot_username']}**\n{c['status']} · last seen {heartbeat}")
            buttons.append(ActionButton(
                f"Monetize #{c['clone_id']}", discord.ButtonStyle.primary, self,
                "monetize_activate", args=(c["clone_id"],),
            ))
        card = NavCardView("Your clones", lines, discord.Color.blurple(), buttons)
        await interaction.followup.send(view=card, ephemeral=True)

    # ── /removeclone ─────────────────────────────────────────────────────
    @app_commands.command(name="removeclone", description="Deactivate one of your Discord bot clones")
    @app_commands.describe(clone_id="The id shown by /myclones")
    async def removeclone(self, interaction: discord.Interaction, clone_id: int):
        await interaction.response.defer(ephemeral=True)
        clone = await db.get_discord_clone(clone_id)
        if not clone or clone["owner_id"] != interaction.user.id:
            await interaction.followup.send("You don't own a clone with that id.", ephemeral=True)
            return
        await db.set_discord_clone_status(clone_id, "inactive")
        await interaction.followup.send(
            f"Clone `#{clone_id}` deactivated — the supervisor will shut its process down within about a minute.",
            ephemeral=True,
        )

    # ── /allservers ───────────────────────────────────────────────────────
    # Admin-only cross-bot roster: every server the main bot AND every
    # clone are currently in, with the server's own owner and (for clones)
    # who manages that clone — one row per server. Rendered as a monospace
    # table since Discord has no native table component; also attached as
    # CSV since the table gets truncated once the roster is large.
    @app_commands.command(
        name="allservers",
        description="[Admin] List every server across the main bot and all clones, with owners/managers",
    )
    @app_commands.describe(include_left="Include servers the bot/clone has since left (default: no)")
    async def allservers(self, interaction: discord.Interaction, include_left: bool = False):
        if not _is_clone_admin(interaction.user.id):
            await interaction.response.send_message("You're not authorized to use this.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        rows = await db.get_all_guilds_with_managers(include_left=include_left)
        if not rows:
            await interaction.followup.send("No servers on record yet.", ephemeral=True)
            return

        def bot_label(r: dict) -> str:
            if r["clone_id"] is None:
                return "Main bot"
            name = r["bot_username"] or "unknown"
            return f"Clone #{r['clone_id']} ({name})"

        def manager_label(r: dict) -> str:
            if r["clone_id"] is None:
                return "—"
            return str(r["manager_id"]) if r["manager_id"] else "unknown"

        # Column widths sized off the actual data (with sane caps) so the
        # monospace table stays aligned without wasting space on short rows.
        col_server = min(max((len(r["guild_name"] or "Unknown") for r in rows), default=6), 28)
        col_bot = min(max((len(bot_label(r)) for r in rows), default=8), 24)

        def fmt_row(vals: list) -> str:
            server, bot, server_owner, manager, members = vals
            return (
                f"{server[:col_server]:<{col_server}} | "
                f"{bot[:col_bot]:<{col_bot}} | "
                f"{server_owner:<20} | "
                f"{manager:<20} | "
                f"{members}"
            )

        header = fmt_row(["Server", "Bot", "Server Owner", "Managed By", "Members"])
        separator = "-" * len(header)
        table_lines = [header, separator]
        for r in rows:
            table_lines.append(fmt_row([
                r["guild_name"] or "Unknown",
                bot_label(r),
                str(r["server_owner_id"]) if r["server_owner_id"] else "unknown",
                manager_label(r),
                str(r["member_count"] or "?"),
            ]))

        # Chunk into <=1900-char code blocks (2000-char message cap minus
        # the ``` fences) so a large roster spans multiple messages instead
        # of getting silently truncated.
        chunks, current = [], ""
        for line in table_lines:
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) > 1900:
                chunks.append(current)
                current = line
            else:
                current = candidate
        if current:
            chunks.append(current)

        for i, chunk in enumerate(chunks):
            content = f"🗂️ **All servers ({len(rows)})** — part {i + 1}/{len(chunks)}\n```\n{chunk}\n```"
            await interaction.followup.send(content, ephemeral=True)

        # Full CSV export alongside the table, for anything the chunked
        # preview cut off or for pulling into a spreadsheet.
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=[
            "guild_id", "guild_name", "member_count", "server_owner_id",
            "clone_id", "bot_username", "manager_id", "clone_status", "joined_at", "left_at",
        ], extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
        data = io.BytesIO(buf.getvalue().encode("utf-8"))
        await interaction.followup.send(
            file=discord.File(data, filename="all_servers.csv"),
            ephemeral=True,
        )

    # ── /clonemonetize — Discord equivalent of handlers/clone_bot.py's ────
    # monetization menu (show_monetization_menu / handle_monetization_
    # activate / handle_monetization_verify / show_clone_prices /
    # start_edit_clone_price / show_payment_settings /
    # handle_set_payment_provider). Everything here checks
    # get_discord_clone_for_owner first — same "not found or not yours"
    # response shape as /removeclone above — so a clone_id belonging to
    # someone else, or one that's been deactivated, can't be probed or
    # edited via a guessed id.
    clonemonetize = app_commands.Group(name="clonemonetize", description="Monetization settings for a clone you own")

    async def _owned_clone_or_deny(self, interaction: discord.Interaction, clone_id: int):
        await interaction.response.defer(ephemeral=True)
        clone = await db.get_discord_clone_for_owner(clone_id, interaction.user.id)
        if clone is None:
            await interaction.followup.send("You don't own an active clone with that id.", ephemeral=True)
            return None
        return clone

    @clonemonetize.command(name="status", description="Check monetization status for a clone you own")
    @app_commands.describe(clone_id="The id shown by /myclones")
    async def monetize_status(self, interaction: discord.Interaction, clone_id: int):
        await interaction.response.defer()
        if await self._owned_clone_or_deny(interaction, clone_id) is None:
            return
        active = await db.is_discord_monetization_active(clone_id)
        sub = await db.get_discord_monetization_subscription(clone_id)
        if active:
            expiry = sub["expires_at"].strftime("%Y-%m-%d") if sub and sub.get("expires_at") else "unknown"
            await interaction.followup.send(
                f"💰 Monetization is **active** on clone `#{clone_id}` until **{expiry}**.\n"
                f"Use `/clonemonetize prices` and `/clonemonetize setpayment` to configure it.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"💰 Monetization is **not active** on clone `#{clone_id}`.\n\n"
                f"Activating (GHS {CLONE_MONETIZATION_FEE_GHS}/month) unlocks:\n"
                f"• Connecting your own Paystack key\n"
                f"• Setting your own prices for this bot's paid features\n\n"
                f"Until activated, this clone's payments go through the main bot's account at default prices.\n"
                f"Run `/clonemonetize activate clone_id:{clone_id}` to start.",
                ephemeral=True,
            )

    async def _resolve_clone_id(self, interaction: discord.Interaction, clone_id: Optional[int]) -> Optional[int]:
        """Auto-detect clone_id when not given: prefer the clone whose bot is
        running the current server (interaction.client.clone_id), else fall
        back to the owner's only clone. Returns None (and replies) if it
        can't be resolved unambiguously."""
        await interaction.response.defer()
        if clone_id is not None:
            return clone_id

        running_clone_id = getattr(interaction.client, "clone_id", None)
        if running_clone_id is not None:
            return running_clone_id

        owned = await db.get_discord_clones_by_owner(interaction.user.id)
        if len(owned) == 1:
            return owned[0]["clone_id"]

        if not owned:
            await interaction.followup.send(
                "You don't own any clones yet — DM me `/registerclone` with a bot token first.", ephemeral=True
            )
        else:
            await interaction.followup.send(
                "You own multiple clones — pass `clone_id` explicitly (see `/myclones`).", ephemeral=True
            )
        return None

    @clonemonetize.command(name="activate", description="Start (or renew) monetization on a clone you own")
    @app_commands.describe(clone_id="The id shown by /myclones (auto-detected if omitted and unambiguous)")
    async def monetize_activate(self, interaction: discord.Interaction, clone_id: int = None):
        await interaction.response.defer()
        clone_id = await self._resolve_clone_id(interaction, clone_id)
        if clone_id is None:
            return  # _resolve_clone_id already responded

        # Owner bypass — mirrors discord_bot/views.py's "You're the bot
        # owner — granted without payment" pattern. Skips both the ownership
        # gate (an admin manages ANY clone, not just their own) and the
        # Paystack flow entirely.
        if _is_clone_admin(interaction.user.id):
            await db.activate_discord_monetization_subscription(clone_id, days=CLONE_MONETIZATION_DAYS)
            await interaction.followup.send(
                f"✅ You're the bot owner — monetization force-activated on clone `#{clone_id}` for "
                f"{CLONE_MONETIZATION_DAYS} days, no payment needed.",
                ephemeral=True,
            )
            return

        if await self._owned_clone_or_deny(interaction, clone_id) is None:
            return

        from config import PAYMENT_MODE
        if PAYMENT_MODE == "manual":
            from payments_manual import _reference_for, _prefilled_selar_link, BuyerConfirmView
            reference = _reference_for("discord_clone_monetization", interaction.user.id)
            await db.start_discord_monetization_payment(clone_id, interaction.user.id, reference)
            await db.log_payment(
                interaction.user.id, 0.0, reference, status="pending",
                payment_type="discord_clone_monetization", provider="selar",
            )
            link = _prefilled_selar_link("discord_clone_monetization", interaction.user.id)
            if not link:
                await interaction.followup.send(
                    "❌ Manual payments aren't set up for monetization yet — please try again later.",
                    ephemeral=True,
                )
                return

            confirm_view = BuyerConfirmView(
                reference, "discord_clone_monetization", interaction.user.id, None, None,
                f"GHS {CLONE_MONETIZATION_FEE_GHS}",
            )
            confirm_view.add_item(discord.ui.Button(label="💳 Pay on Selar", url=link, style=discord.ButtonStyle.link))
            await interaction.followup.send(
                f"**Activate Monetization — GHS {CLONE_MONETIZATION_FEE_GHS}/month** for clone `#{clone_id}`.\n\n"
                f"Tap **Pay on Selar**, complete checkout, then tap **I've Paid** below so it gets reviewed.",
                view=confirm_view, ephemeral=True,
            )
            return

        email = f"discorduser_{interaction.user.id}@animebot.com"
        payment_result = await asyncio.to_thread(
            paystack.initialize_payment,
            email, CLONE_MONETIZATION_FEE_GHS * 100, interaction.user.id,
            f"DiscordCloneMonetize_{clone_id}",
            payment_type="discord_clone_monetization",
            extra_metadata={"clone_id": clone_id},
        )
        if not payment_result or payment_result.get("status") != "success":
            await interaction.followup.send("❌ Couldn't start payment. Please try again.", ephemeral=True)
            return

        reference = payment_result["reference"]
        await db.start_discord_monetization_payment(clone_id, interaction.user.id, reference)
        self._pending_monetize[interaction.user.id] = {"clone_id": clone_id, "reference": reference}

        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="💳 Pay Now", url=payment_result["authorization_url"], style=discord.ButtonStyle.link))
        view.add_item(ActionButton("✅ Verify Payment", discord.ButtonStyle.success, self, "monetize_verify_button"))
        await interaction.followup.send(
            f"**Activate Monetization — GHS {CLONE_MONETIZATION_FEE_GHS}/month** for clone `#{clone_id}`.\n\n"
            f"Pay via the link, then tap **Verify Payment**.",
            view=view, ephemeral=True,
        )

    async def monetize_verify_button(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        pending = self._pending_monetize.get(interaction.user.id)
        if not pending:
            await interaction.followup.send("No pending activation payment found. Run `/clonemonetize activate` again.", ephemeral=True)
            return
        result = await asyncio.to_thread(paystack.verify_payment, pending["reference"])
        if result.get("status") == "success":
            self._pending_monetize.pop(interaction.user.id, None)
            await db.activate_discord_monetization_subscription(pending["clone_id"], days=CLONE_MONETIZATION_DAYS)
            await interaction.followup.send(
                f"✅ Monetization activated on clone `#{pending['clone_id']}` for {CLONE_MONETIZATION_DAYS} days. "
                f"You can now use `/clonemonetize setprice` and `/clonemonetize setpayment`.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send("Payment not confirmed yet. Complete checkout, then tap Verify again.", ephemeral=True)

    @clonemonetize.command(name="prices", description="View this clone's current prices for paid features")
    @app_commands.describe(clone_id="The id shown by /myclones")
    async def monetize_prices(self, interaction: discord.Interaction, clone_id: int):
        await interaction.response.defer(ephemeral=True)
        if await self._owned_clone_or_deny(interaction, clone_id) is None:
            return
        prices = await db.get_discord_clone_prices(clone_id)
        active = await db.is_discord_monetization_active(clone_id)
        embed = discord.Embed(title=f"Prices for clone #{clone_id}", color=discord.Color.gold())
        for k, v in prices.items():
            embed.add_field(name=PRICE_REGISTRY[k]["label"], value=f"GHS {v:g}", inline=True)
        if not active:
            embed.set_footer(text="These are registry defaults — activate monetization to set your own.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @clonemonetize.command(name="setprice", description="Set a custom price for one of this clone's paid features")
    @app_commands.describe(clone_id="The id shown by /myclones", feature="Which feature's price to change", amount_ghs="New price in GHS (e.g. 15 or 15.50)")
    @app_commands.choices(feature=[
        app_commands.Choice(name=v["label"], value=k) for k, v in PRICE_REGISTRY.items()
    ])
    async def monetize_setprice(self, interaction: discord.Interaction, clone_id: int, feature: app_commands.Choice[str], amount_ghs: float):
        await interaction.response.defer(ephemeral=True)
        if await self._owned_clone_or_deny(interaction, clone_id) is None:
            return
        if not await db.is_discord_monetization_active(clone_id):
            await interaction.followup.send("Activate monetization first with `/clonemonetize activate`.", ephemeral=True)
            return
        if amount_ghs <= 0 or amount_ghs > 100000:
            await interaction.followup.send("That doesn't look like a valid price.", ephemeral=True)
            return
        ok = await db.set_discord_clone_price(clone_id, interaction.user.id, feature.value, amount_ghs)
        if ok:
            await interaction.followup.send(f"✅ {feature.name} is now GHS {amount_ghs:g} on clone `#{clone_id}`.", ephemeral=True)
        else:
            await interaction.followup.send("⚠️ Couldn't update that price — try again.", ephemeral=True)

    @clonemonetize.command(name="payment", description="Check where this clone's payments currently go")
    @app_commands.describe(clone_id="The id shown by /myclones")
    async def monetize_payment(self, interaction: discord.Interaction, clone_id: int):
        await interaction.response.defer()
        if await self._owned_clone_or_deny(interaction, clone_id) is None:
            return
        cfg = await db.get_discord_clone_payment_config(clone_id)
        if cfg["provider"] == "paystack" and cfg["api_key"]:
            label = "your own connected Paystack key"
        elif cfg["provider"] == "stripe" and cfg["api_key"]:
            label = "your own connected Stripe key (charged in USD — GHS prices are converted live)"
        else:
            label = "the main bot's account (default)"
        await interaction.followup.send(
            f"💳 Payments for clone `#{clone_id}` currently go to **{label}**.\n\n"
            f"Use `/clonemonetize setpayment` to switch — this is optional.",
            ephemeral=True,
        )

    @clonemonetize.command(name="setpayment", description="Route this clone's payments to your own Paystack/Stripe key, or back to the main bot's")
    @app_commands.describe(clone_id="The id shown by /myclones", provider="Where payments should go",
                            secret_key="Your Paystack or Stripe secret key (not needed for 'main')")
    @app_commands.choices(provider=[
        app_commands.Choice(name="My own Paystack account", value="paystack"),
        app_commands.Choice(name="My own Stripe account", value="stripe"),
        app_commands.Choice(name="Main bot's account (default)", value="main"),
    ])
    async def monetize_setpayment(self, interaction: discord.Interaction, clone_id: int, provider: app_commands.Choice[str], secret_key: str = None):
        await interaction.response.defer(ephemeral=True)
        if await self._owned_clone_or_deny(interaction, clone_id) is None:
            return
        if not await db.is_discord_monetization_active(clone_id):
            await interaction.followup.send("Activate monetization first with `/clonemonetize activate`.", ephemeral=True)
            return

        if provider.value == "main":
            await db.set_discord_clone_payment_provider(clone_id, interaction.user.id, "main")
            await interaction.followup.send(f"✅ Clone `#{clone_id}` switched back to the main bot's account.", ephemeral=True)
            return

        if not secret_key or len(secret_key) < 10:
            await interaction.followup.send(
                "Provide your secret key in the `secret_key` option to switch to your own account.", ephemeral=True
            )
            return

        if provider.value == "stripe" and not secret_key.startswith(("sk_live_", "sk_test_")):
            await interaction.followup.send(
                "That doesn't look like a Stripe secret key (should start with `sk_live_` or `sk_test_`). "
                "Grab it from your Stripe Dashboard → Developers → API keys.", ephemeral=True
            )
            return
        if provider.value == "paystack" and not secret_key.startswith("sk_"):
            await interaction.followup.send(
                "That doesn't look like a Paystack secret key (should start with `sk_`).", ephemeral=True
            )
            return

        await db.set_discord_clone_payment_provider(clone_id, interaction.user.id, provider.value, api_key=secret_key)
        note = (
            "This clone's prices are set in GHS — since Stripe doesn't settle in GHS, each charge is "
            "converted to USD live at checkout time using current exchange rates."
            if provider.value == "stripe" else
            "This clone's payments will now go to your own account."
        )
        await interaction.followup.send(
            f"✅ {provider.name} connected for clone `#{clone_id}`. {note}\n"
            f"(Consider deleting your command-usage message from Discord's history since it contained the raw key — "
            f"slash command inputs aren't visible to other members, but they do stay in your own client history.)",
            ephemeral=True,
        )


    # ── /ownermonetize — one-shot owner shortcut ─────────────────────────
    # Suggested as "/admin monetize <clone_id>" but the existing top-level
    # "admin" group lives in discord_bot/cogs/admin.py (a separate cog) and
    # app_commands doesn't let two cogs share a group name, so this is a
    # standalone command instead. Same effect: force-activate without the
    # payment/verify button flow — for testing or comping specific clones.
    @app_commands.command(name="ownermonetize", description="[Owner] Force-activate monetization on any clone, no payment")
    @app_commands.describe(clone_id="The clone id to activate (see /myclones)")
    async def ownermonetize(self, interaction: discord.Interaction, clone_id: int):
        await interaction.response.defer(ephemeral=True)
        if not _is_clone_admin(interaction.user.id):
            await interaction.followup.send("This command is restricted to bot owners.", ephemeral=True)
            return
        clone = await db.get_discord_clone(clone_id)
        if not clone:
            await interaction.followup.send(f"No clone found with id `#{clone_id}`.", ephemeral=True)
            return
        await db.activate_discord_monetization_subscription(clone_id, days=CLONE_MONETIZATION_DAYS)
        await interaction.followup.send(
            f"✅ Force-activated monetization on clone `#{clone_id}` (**{clone['bot_username']}**) for "
            f"{CLONE_MONETIZATION_DAYS} days — no payment taken.",
            ephemeral=True,
        )


    # ── /ownerbroadcast — DM every user of the main bot + every clone ────
    # Fan-out only: this command just resolves recipients and queues the
    # job (fast, so the slash command can respond immediately). The actual
    # DMing happens out-of-band in api/cron_discord_owner_broadcast.py,
    # same split as the Telegram broadcast_jobs flow and Discord's own
    # scheduled-announcements cron — a live interaction response has to
    # return in seconds, but DMing every user across every clone can't.
    @app_commands.command(name="ownerbroadcast", description="[Owner] DM an announcement to bot users or clone admins")
    @app_commands.describe(
        message="The announcement text — sent as-is, signed with your configured brand name",
        target="Who receives this DM — regular bot users (default), clone admins/operators, or server owners",
        image="Optional image to attach — drag & drop or upload it here, sent alongside the text",
        payment_button="Optional — attach an 'I've Paid' button for this product (lets buyers claim straight from this DM)",
    )
    @app_commands.choices(payment_button=[
        app_commands.Choice(name=key.replace("_", " ").title(), value=key) for key in SELAR_PRODUCT_LINKS
    ])
    @app_commands.choices(target=[
        app_commands.Choice(name="Users — everyone across the main bot + clones", value="users"),
        app_commands.Choice(name="Admins — clone owners/operators only", value="admins"),
        app_commands.Choice(name="Server owners — owner of every server the bot/clones are in", value="servers"),
    ])
    async def ownerbroadcast(self, interaction: discord.Interaction, message: str,
                              target: Optional[app_commands.Choice[str]] = None,
                              image: Optional[discord.Attachment] = None,
                              payment_button: Optional[app_commands.Choice[str]] = None):
        if interaction.user.id not in DISCORD_OWNER_BROADCAST_IDS:
            await interaction.response.send_message("This command is restricted to bot owners.", ephemeral=True)
            return
        if interaction.guild_id is not None:
            await interaction.response.send_message(
                "Please run this one in a DM with me — it's a broadcast, not something to fire from a server by accident.",
                ephemeral=True,
            )
            return

        if image is not None and not (image.content_type or "").startswith("image/"):
            await interaction.response.send_message(
                f"`{image.filename}` doesn't look like an image (got `{image.content_type or 'unknown type'}`). "
                f"Attach a PNG/JPG/GIF/WebP, or drop the image and leave it off to send text-only.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        # We store Discord's own CDN URL for the attachment rather than
        # re-hosting it — simple, but that URL is a signed CDN link that
        # expires (currently ~24h). Broadcasts normally send within
        # minutes via the cron sender, so this is fine in practice; it
        # only bites if a broadcast sits queued for a long time (see
        # /broadcaststatus's "stuck" diagnosis below) with an image attached.
        image_url = image.url if image is not None else None

        target_value = target.value if target else "users"

        broadcast_id = await db.create_owner_broadcast(
            interaction.user.id, message, image_url,
            payment_button_type=payment_button.value if payment_button else None,
        )

        if target_value == "admins":
            # Clone owners/operators only — every distinct owner_id of a
            # currently-active clone, plus the fixed DISCORD_CLONE_ADMIN_IDS
            # allowlist. All of these people are known to the MAIN bot
            # (they DM'd it to run /registerclone, or are hardcoded admins),
            # so this always sends via clone_id=None regardless of which
            # clone(s) a given admin owns — no per-clone token juggling
            # needed like the "users" path below.
            admin_ids = set(await db.get_discord_clone_owner_ids())
            admin_ids.update(DISCORD_CLONE_ADMIN_IDS)
            admin_ids.discard(interaction.user.id)  # don't DM the sender themselves
            await db.add_owner_broadcast_recipients(broadcast_id, None, list(admin_ids))
            recipient_note = f"**{len(admin_ids)}** clone admin(s)"
        elif target_value == "servers":
            # Owner (guild.owner_id) of every currently-joined guild across
            # the main bot and every active clone — distinct from "admins"
            # above, since a server owner may never have DM'd the main bot
            # or run /registerclone at all (they may only have invited a
            # clone to their own server). That means, unlike the "admins"
            # path, we can't assume everyone is reachable through the main
            # bot's token — same per-clone token requirement as "users"
            # below, so this mirrors that loop rather than the "admins" one.
            seen_owner_ids = set()

            main_owner_ids = await db.get_discord_guild_owner_ids(None)
            main_owner_ids = [uid for uid in main_owner_ids if uid != interaction.user.id]
            seen_owner_ids.update(main_owner_ids)
            await db.add_owner_broadcast_recipients(broadcast_id, None, main_owner_ids)

            clones = await db.list_active_discord_clones()
            for clone in clones:
                clone_owner_ids = await db.get_discord_guild_owner_ids(clone["clone_id"])
                new_owner_ids = [uid for uid in clone_owner_ids if uid not in seen_owner_ids and uid != interaction.user.id]
                seen_owner_ids.update(new_owner_ids)
                await db.add_owner_broadcast_recipients(broadcast_id, clone["clone_id"], new_owner_ids)

            recipient_note = f"**{len(seen_owner_ids)}** server owner(s) across the main bot and {len(clones)} clone(s)"
        else:
            # Main bot's own users (clone_id=None), plus every currently-active
            # clone's users. An inactive/removed clone is skipped since there's
            # no live token to DM through even if we queued its users.
            #
            # IMPORTANT: get_discord_bot_user_ids only dedupes WITHIN one
            # clone_id — it does nothing about the same real Discord user
            # showing up under several clone_ids (e.g. someone who's used the
            # main bot AND a support-server clone). Without a global dedupe
            # here, that user gets one recipient row — and therefore one DM —
            # per bot/clone they've touched. seen_user_ids fixes that: each
            # user_id is only ever queued once for this broadcast, via
            # whichever bot we saw them on first (main bot wins ties since
            # it's resolved before the clone loop).
            seen_user_ids = set()

            main_user_ids = await db.get_discord_bot_user_ids(None)
            seen_user_ids.update(main_user_ids)
            await db.add_owner_broadcast_recipients(broadcast_id, None, main_user_ids)

            clones = await db.list_active_discord_clones()
            for clone in clones:
                clone_user_ids = await db.get_discord_bot_user_ids(clone["clone_id"])
                new_user_ids = [uid for uid in clone_user_ids if uid not in seen_user_ids]
                seen_user_ids.update(new_user_ids)
                await db.add_owner_broadcast_recipients(broadcast_id, clone["clone_id"], new_user_ids)

            recipient_note = f"**{len(seen_user_ids)}** recipient(s) across the main bot and {len(clones)} clone(s)"

        broadcast_row = await db.get_owner_broadcast(broadcast_id)
        total = broadcast_row["total_recipients"] if broadcast_row else None

        button_note = f" with an I've Paid button for `{payment_button.value}`" if payment_button else ""
        await interaction.followup.send(
            f"📢 Broadcast `#{broadcast_id}` queued for {recipient_note}"
            f"{' (' + str(total) + ' total)' if total is not None else ''}"
            f"{' with an image attached' if image_url else ''}"
            f"{button_note}.\n"
            f"It'll go out shortly via the broadcast sender — DMs trickle out gradually to stay well under "
            f"Discord's rate limits, so a large broadcast can take a while to fully land.\n"
            f"Check progress any time with `/broadcaststatus id:{broadcast_id}`.",
            ephemeral=True,
        )

    # ── /broadcaststatus — did /ownerbroadcast actually go out? ──────────
    # Exists because the cron sender (api/cron_discord_owner_broadcast.py)
    # runs completely out-of-band from the slash command — /ownerbroadcast
    # only ever confirms the job was QUEUED, never that DMs actually sent.
    # If nothing's hitting that cron endpoint (not wired to a scheduler
    # yet, wrong CRON_SECRET, scheduler paused, etc.) a broadcast can sit
    # at 0 sent forever with no visible error anywhere else.
    @app_commands.command(name="broadcaststatus", description="[Owner] Check whether an /ownerbroadcast actually went out")
    @app_commands.describe(id="Broadcast id shown when you ran /ownerbroadcast. Leave blank for the most recent one.")
    async def broadcaststatus(self, interaction: discord.Interaction, id: Optional[int] = None):
        await interaction.response.defer(ephemeral=True)
        if interaction.user.id not in DISCORD_OWNER_BROADCAST_IDS:
            await interaction.followup.send("This command is restricted to bot owners.", ephemeral=True)
            return

        if id is None:
            broadcast = await db.get_latest_owner_broadcast(interaction.user.id)
            if not broadcast:
                await interaction.followup.send("You haven't run `/ownerbroadcast` yet.", ephemeral=True)
                return
        else:
            broadcast = await db.get_owner_broadcast(id)
            if not broadcast:
                await interaction.followup.send(f"No broadcast found with id `#{id}`.", ephemeral=True)
                return

        total = broadcast["total_recipients"] or 0
        sent = broadcast["sent_count"] or 0
        failed = broadcast["failed_count"] or 0
        attempted = sent + failed
        pending = max(total - attempted, 0)

        if attempted == 0 and broadcast["status"] == "pending":
            diagnosis = (
                "⚠️ **Nothing has gone out yet, and it's likely stuck.** `/ownerbroadcast` only queues the job — "
                "actually sending it is api/cron_discord_owner_broadcast.py, which has to be hit by an external "
                "scheduler (Vercel Cron, cron-job.org, etc.) every minute or so. Zero attempts after a while "
                "usually means: that endpoint isn't wired to a scheduler yet, the scheduler is hitting the wrong "
                "URL, or its `Authorization: Bearer <CRON_SECRET>` doesn't match your `CRON_SECRET` env var."
            )
        elif broadcast["status"] == "completed" and failed > 0 and sent == 0:
            diagnosis = (
                "❌ **Every single DM failed.** That usually means every recipient's clone token is invalid/expired, "
                "or (less likely) every recipient has DMs closed. Check the individual recipient errors in "
                "`discord_owner_broadcast_recipients` for the real reason."
            )
        elif broadcast["status"] == "completed":
            diagnosis = "✅ Finished sending."
        else:
            diagnosis = "🔄 Still in progress — the cron sender processes it in small batches."

        await interaction.followup.send(
            f"**Broadcast `#{broadcast['id']}`** — status: `{broadcast['status']}`\n"
            f"Sent: **{sent}** · Failed: **{failed}** · Still pending: **{pending}** · Total: **{total}**\n\n"
            f"{diagnosis}",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(CloneAdminCog(bot))
