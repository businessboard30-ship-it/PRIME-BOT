# path: gumroad_payments.py

"""Automatic Gumroad payments (payment mode "gumroad").

Flow: the buyer taps Pay -> we log a pending payment_logs row with a unique
reference and hand them the product's Gumroad link with ?reference=<ref>
appended. Gumroad forwards extra URL params to the seller's Ping (webhook)
as url_params[reference]. When a sale completes, Gumroad POSTs to
/api/gumroad_webhook (api/gumroad_webhook.py), which calls
process_gumroad_ping() here: it validates the sale, atomically claims the
pending row, runs the SAME UNLOCK_HANDLERS the other modes use, and DMs the
buyer. No admin approval needed.

Safety layers (Gumroad pings are unsigned): shared secret in the ping URL,
reference must match a pending 'gumroad' row, the product must match the
row's payment_type, price must be >= expected, and (if GUMROAD_ACCESS_TOKEN
is set) the sale is re-fetched from Gumroad's API and must exist and not be
refunded. The claim is a conditional UPDATE, so duplicate pings are no-ops.
"""

import json
import logging
import secrets
from typing import Optional
from urllib.parse import urlencode

import aiohttp
import discord

import config
import gumroad_autocreate as _auto
from database import db, get_pool

logger = logging.getLogger(__name__)

PROVIDER = "gumroad"

_PRICE_ATTRS = {
    "welcome_card_pack": "WELCOME_CARD_PACK_FEE_USD",
    "ultra_welcome_pack": "ULTRA_PACK_FEE_USD",
    "custom_role": "CUSTOM_ROLE_FEE_USD",
    "music_pro": "MUSIC_PRO_FEE_USD",
    "discord_clone": "DISCORD_CLONE_ACTIVATION_FEE_USD",
    "discord_clone_monetization": "CLONE_MONETIZATION_FEE_USD",
    "xp_boost": "XP_BOOST_FEE_USD",
    "premium": "PREMIUM_FEE_USD",
    "premium_yearly": "PREMIUM_YEARLY_FEE_USD",
    "premium_lifetime": "PREMIUM_LIFETIME_FEE_USD",
    "hardcore_roast": "HARDCORE_ROAST_FEE_USD",
    "ad_placement": "AD_PLACEMENT_FEE_USD",
}


# Products that unlock something for one specific server; their unlock handler
# needs the guild id stored in payment_logs.chat_id.
_GUILD_SCOPED_TYPES = {"welcome_card_pack", "ultra_welcome_pack", "custom_role", "music_pro", "premium", "premium_yearly", "premium_lifetime"}


def expected_price_usd(payment_type: str) -> Optional[float]:
    from modules import user_subs
    if user_subs.is_plan(payment_type):
        return user_subs.price_usd(payment_type)
    if payment_type in _PRICE_ATTRS:
        return float(getattr(config, _PRICE_ATTRS[payment_type]))
    tier = config.XP_SERVER_BOOST_TIERS.get(payment_type)
    if tier:
        return float(tier["fee_usd"])
    bundle = config.XP_BOOST_BUNDLES.get(payment_type)
    return float(bundle["fee_usd"]) if bundle else None


def new_reference(payment_type: str, user_id: int) -> str:
    return f"gum_{payment_type}_{user_id}_{secrets.token_hex(4)}"


def build_link(payment_type: str, user_id: int, reference: str, discount_code: Optional[str] = None) -> Optional[str]:
    base = _auto.link_for(payment_type)
    if not base:
        return None
    params = {"wanted": "true", "reference": reference}
    if discount_code:
        # Gumroad applies an offer code from the path (…/l/<product>/<CODE>) and from ?code=.
        base = f"{base.rstrip('/')}/{discount_code}"
        params["code"] = discount_code
    return f"{base}{'&' if '?' in base else '?'}{urlencode(params)}"


async def start_gumroad_payment(interaction: discord.Interaction, payment_type: str,
                                 amount_display: str, guild_id: Optional[int] = None,
                                 reference: Optional[str] = None,
                                 amount_usd: Optional[float] = None,
                                 intro: Optional[str] = None,
                                 discount_code: Optional[str] = None) -> str:
    """Same calling convention as payments_manual.start_manual_payment
    (call after interaction.response.defer). Returns the reference.

    amount_usd: logs this instead of expected_price_usd(payment_type) — for
    variable-price items (e.g. ad_placement, where the buyer names a budget
    at/above the product's minimum) so payment_logs reflects what they
    actually agreed to pay, not just the floor price."""
    user = interaction.user
    if guild_id is None and payment_type in _GUILD_SCOPED_TYPES:
        # Would log a paid-but-unassignable payment (chat_id NULL). Ask which
        # server FIRST, then continue this same checkout for it.
        from discord_bot.cogs._views_gumroad_claim import prompt_server_for_checkout
        await prompt_server_for_checkout(
            interaction, payment_type,
            lambda i, gid: start_gumroad_payment(
                i, payment_type, amount_display, guild_id=gid, reference=reference,
                amount_usd=amount_usd, intro=intro, discount_code=discount_code),
        )
        return reference or ""
    reference = reference or new_reference(payment_type, user.id)
    clone_id = getattr(interaction.client, "clone_id", None)
    await _auto.load_runtime()
    link = build_link(payment_type, user.id, reference, discount_code=discount_code)
    if not link:
        await interaction.followup.send("Checkout isn't set up for this yet — please try again later.", ephemeral=True)
        logger.error(f"[gumroad] no GUMROAD_PRODUCT_LINKS entry for {payment_type}")
        return reference

    logged_amount = amount_usd if amount_usd is not None else (expected_price_usd(payment_type) or 0.0)
    await db.log_payment(
        user.id, logged_amount, reference, status="pending",
        payment_type=payment_type, chat_id=guild_id, provider=PROVIDER, clone_id=clone_id,
    )
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="💳 Pay on Gumroad", url=link, style=discord.ButtonStyle.link))
    from discord_bot.cogs._views_payment_card import (
        order_summary, auto_unlock_note, add_check_status_button,
    )
    add_check_status_button(view, user.id, payment_type, guild_id)
    from discord_bot.cogs._views_premium import add_go_premium_option
    add_go_premium_option(view, interaction, payment_type, guild_id)
    prefix = f"{intro}\n\n" if intro else ""
    await interaction.followup.send(
        f"{prefix}{order_summary(interaction, payment_type, amount_display, guild_id, reference)}\n\n"
        f"Pay with the button below — use this exact one, it carries your order reference.\n"
        f"{auto_unlock_note(guild_id, payment_type)}",
        view=view, ephemeral=True,
    )
    return reference


def _matches_product(payment_type: str, fields: dict) -> bool:
    url = _auto.link_for(payment_type)
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    pid = _auto.id_for(payment_type)
    permalink = fields.get("permalink") or ""
    product_permalink = fields.get("product_permalink") or ""
    if slug and (permalink == slug or product_permalink.rstrip("/").endswith("/" + slug)):
        return True
    return bool(pid and fields.get("product_id") == pid)


async def _sale_is_valid(sale_id: str, payment_type: str) -> bool:
    token = config.GUMROAD_ACCESS_TOKEN
    if not token:
        logger.warning("[gumroad] GUMROAD_ACCESS_TOKEN not set — skipping API sale verification")
        return True
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.get(f"https://api.gumroad.com/v2/sales/{sale_id}", params={"access_token": token}) as r:
                data = await r.json(content_type=None)
    except Exception:
        logger.exception("[gumroad] sale verification request failed")
        return False
    sale = (data or {}).get("sale")
    if not (data or {}).get("success") or not sale:
        return False
    if sale.get("refunded") or sale.get("chargebacked"):
        return False
    return True


async def _dm(user_id: int, clone_id, text: str) -> None:
    try:
        from discord_bot.dm_send import dm_user
        token = config.DISCORD_BOT_TOKEN
        if clone_id:
            clone = await db.get_discord_clone(int(clone_id))
            if clone:
                from utils.crypto import secret_manager
                token = secret_manager.decrypt(clone["bot_token_encrypted"])
        if token:
            await dm_user(int(user_id), text, token)
    except Exception:
        logger.exception("[gumroad] buyer DM failed (payment already applied)")


async def _dm_with_button(user_id: int, clone_id, text: str, button: dict) -> bool:
    """Like _dm but attaches one persistent button. Uses the clone's own
    token for clone-scoped payments so the click reaches the right bot."""
    try:
        from discord_bot.dm_send import dm_user_with_buttons
        token = config.DISCORD_BOT_TOKEN
        if clone_id:
            clone = await db.get_discord_clone(int(clone_id))
            if clone:
                from utils.crypto import secret_manager
                token = secret_manager.decrypt(clone["bot_token_encrypted"])
        if token:
            return await dm_user_with_buttons(int(user_id), text, [button], token)
    except Exception:
        logger.exception("[gumroad] buyer DM (with button) failed")
    return False


async def _bot_token_for(clone_id) -> Optional[str]:
    """Token of the bot that carried out the checkout: the clone's own when the
    payment belongs to a clone, else the main bot's."""
    token = config.DISCORD_BOT_TOKEN
    if clone_id:
        clone = await db.get_discord_clone(int(clone_id))
        if clone:
            from utils.crypto import secret_manager
            token = secret_manager.decrypt(clone["bot_token_encrypted"])
    return token


async def _post_claim_in_server(row: dict, button: dict, text: str) -> Optional[int]:
    """Fallback for a buyer whose DMs are closed: post the same message + button
    in a server, as the same bot (clone token for clone payments). Uses the
    server on the payment when there is one; otherwise, if the buyer owns exactly
    one server this bot is in, uses that. Anything less certain returns None
    rather than posting in a server we're guessing at. Returns the guild id
    posted in, or None."""
    try:
        clone_id = row.get("clone_id")
        guild_id = row.get("chat_id")
        if not guild_id:
            owned = await db.get_owned_guild_ids(int(row["user_id"]), int(clone_id) if clone_id else None)
            if len(owned) != 1:
                return None
            guild_id = owned[0]
        token = await _bot_token_for(clone_id)
        if not token:
            return None
        from discord_bot.dm_send import post_in_guild_with_buttons
        ok = await post_in_guild_with_buttons(
            int(guild_id), text, [button], token, mention_user_id=int(row["user_id"])
        )
        return int(guild_id) if ok else None
    except Exception:
        logger.exception("[gumroad] server fallback post failed")
        return None


async def _try_auto_apply(row: dict, reference: str, subscription_id) -> Optional[int]:
    """A verified sale arrived with no server. If the buyer owns EXACTLY ONE
    server this bot is in (and it doesn't already have the product), unlock
    it there straight away instead of making them tap Choose-server. Returns
    the guild id on success, None if it wasn't unambiguous or failed (the
    caller then falls back to the Choose-server DM, claim untouched). Only
    ownership counts — anyone with just Manage Server still goes through the
    picker, so nothing is ever applied to a server on a guess."""
    try:
        clone_id = row.get("clone_id")
        owned = await db.get_owned_guild_ids(int(row["user_id"]), int(clone_id) if clone_id else None)
        if len(owned) != 1:
            return None
        guild_id = int(owned[0])
        from discord_bot.cogs._views_gumroad_claim import _already_owned, _take_claim, _put_claim_back
        payment_type = row["payment_type"]
        if await _already_owned(payment_type, guild_id, clone_id):
            return None
        from payments_manual import UNLOCK_HANDLERS
        handler = UNLOCK_HANDLERS.get(payment_type)
        if handler is None:
            return None
        record = await _take_claim(reference)  # atomic: the buyer's picker can't also fire
        if not record:
            return None
        try:
            await handler(reference, int(row["user_id"]), guild_id, clone_id)
            await db.set_payment_chat_id(reference, guild_id)
            if payment_type == "premium" and subscription_id:
                await db.set_premium_subscription_id(guild_id, clone_id, subscription_id)
        except Exception:
            logger.exception(f"[gumroad] auto-apply failed for {reference}; falling back to the picker")
            await _put_claim_back(reference, record)
            return None
        return guild_id
    except Exception:
        logger.exception(f"[gumroad] auto-apply check failed for {reference}")
        return None


async def _hold_for_server_choice(row: dict, reference: str, paid_cents: int,
                                  subscription_id, fields: dict) -> tuple:
    """Verified sale, guild-scoped product, but payment_logs.chat_id is NULL.
    Claim the row (-> completed, so revenue counts it and Gumroad stops
    retrying), remember what to unlock under `gumguild:<reference>`, and DM
    the buyer a persistent Choose-server button
    (discord_bot/cogs/_views_gumroad_claim.py handles the click)."""
    payment_type = row["payment_type"]
    pool = await get_pool()
    async with pool.acquire() as conn:
        claimed = await conn.fetchrow(
            "UPDATE payment_logs SET status = 'completed', amount = $2 "
            "WHERE paystack_reference = $1 AND status = 'pending' RETURNING *",
            reference, paid_cents / 100.0,
        )
    if not claimed:
        return 200, "already processed"

    record = {
        "user_id": int(row["user_id"]), "payment_type": payment_type,
        "clone_id": row.get("clone_id"), "subscription_id": subscription_id,
    }
    try:
        await db.set_global_setting(f"gumguild:{reference}", json.dumps(record))
    except Exception:
        logger.exception(f"[gumroad] couldn't store server-claim for {reference}; reverting")
        async with pool.acquire() as conn:
            await conn.execute("UPDATE payment_logs SET status = 'pending' WHERE paystack_reference = $1", reference)
        return 500, "unlock failed"

    from discord_bot.cogs._views_gumroad_claim import custom_id_for, _PRODUCT_LABELS
    label = _PRODUCT_LABELS.get(payment_type, payment_type)
    button = {"label": "Choose server", "style": 3, "custom_id": custom_id_for(reference)}

    applied_to = await _try_auto_apply(row, reference, subscription_id)
    if applied_to:
        logger.info(f"[gumroad] {reference} auto-applied to the buyer's only server ({applied_to})")
        await _dm(row["user_id"], row.get("clone_id"),
                  f"✅ Payment received for **{label}** — it's now unlocked on your server. Enjoy!")
        await _alert_owner(
            "\U0001F4B0 **Gumroad sale auto-applied**\n"
            "Sale: `%s` \u2022 Product: %s \u2022 Price: %s cents \u2022 Buyer: <@%s> \u2022 Server: `%s` \u2022 Reference: `%s`" % (
                fields.get("sale_id", "?"), label, fields.get("price", "?"), row["user_id"], applied_to, reference))
        return 200, "auto-applied"

    sent = await _dm_with_button(
        row["user_id"], row.get("clone_id"),
        f"✅ Payment received for **{label}** — thank you!\n"
        f"Tap the button below to choose which server to unlock it on. "
        f"You can do this any time; the button keeps working.",
        button,
    )
    posted_in = None
    if not sent:
        # DMs closed: post the picker in the server instead, as the same bot.
        posted_in = await _post_claim_in_server(
            row, button,
            f"<@{row['user_id']}> ✅ Payment received for **{label}** — thank you! "
            f"I couldn't DM you, so tap the button below to choose which server to unlock it on. "
            f"Only you can use it, and it keeps working.",
        )
    logger.info(f"[gumroad] {reference} paid with no guild; picker DM sent={sent}, server fallback={posted_in}")
    await _alert_owner(
        "\U0001F4B0 **Gumroad sale received - waiting for buyer to pick a server**\n"
        "Sale: `%s` \u2022 Product: %s \u2022 Price: %s cents \u2022 Buyer: <@%s> (%s) \u2022 Reference: `%s`\n%s" % (
            fields.get("sale_id", "?"), label, fields.get("price", "?"), row["user_id"],
            fields.get("email", "?"), reference,
            "The buyer was DMed a server picker." if sent else
            (f"Couldn't DM the buyer (DMs closed), so the server picker was posted in server {posted_in} instead."
             if posted_in else
             "\u26a0\ufe0f Couldn't DM the buyer (DMs closed?) and couldn't work out which server to post in - they can't pick a server until you reach them."),
        )
    )
    return 200, "awaiting server choice"


async def _process_premium_renewal(fields: dict, subscription_id: str, accept_test: bool) -> tuple:
    """A recurring Premium charge. Same safety layers as a first purchase
    (product match, price >= $5, sale re-verified via the API), plus
    idempotency keyed on the sale id so a retried ping can't double-extend."""
    import config
    sub = await db.get_guild_premium_by_subscription(subscription_id)
    if not sub:
        logger.warning(f"[gumroad] renewal for unknown subscription {subscription_id!r}")
        return 200, "unknown subscription"
    if not _matches_product("premium", fields):
        logger.warning(f"[gumroad] renewal product mismatch for subscription {subscription_id!r}")
        return 200, "product mismatch"
    try:
        paid_cents = int(float(fields.get("price", 0)))
    except (TypeError, ValueError):
        paid_cents = 0
    if paid_cents < round(config.PREMIUM_FEE_USD * 100):
        logger.warning(f"[gumroad] premium renewal underpaid: {paid_cents}c")
        return 200, "underpaid"
    sale_id = fields.get("sale_id", "")
    if not accept_test and not await _sale_is_valid(sale_id, "premium"):
        logger.warning(f"[gumroad] premium renewal sale verification failed ({sale_id})")
        return 200, "sale not verified"

    reference = f"gum_renew_{sale_id}" if sale_id else f"gum_renew_{subscription_id}_{secrets.token_hex(4)}"
    pool = await get_pool()
    async with pool.acquire() as conn:
        claimed = await conn.fetchval(
            "INSERT INTO payment_logs (user_id, amount, status, paystack_reference, payment_type, chat_id, provider, clone_id) "
            "VALUES ($1, $2, 'completed', $3, 'premium', $4, $5, $6) "
            "ON CONFLICT (paystack_reference) DO NOTHING RETURNING paystack_reference",
            sub["activated_by"], paid_cents / 100.0, reference, sub["guild_id"], PROVIDER, sub["clone_id"],
        )
    if not claimed:
        return 200, "already processed"
    row = await db.renew_guild_premium_subscription(subscription_id, config.PREMIUM_SUB_DAYS)
    logger.info(f"[gumroad] premium renewed for guild {sub['guild_id']} (subscription {subscription_id})")
    if row and row.get("activated_by"):
        await _dm(row["activated_by"], row.get("clone_id"),
                  "✅ Your **Premium** subscription just renewed — thanks for supporting the bot!")
    return 200, "ok"


def _user_plan_event_id(fields: dict, kind: str, subscription_id: str) -> str:
    """Stable per gateway event, so a retried ping can never apply twice."""
    sale = fields.get("sale_id") or ""
    if kind == "charge" and sale:
        return f"gum:sale:{sale}"
    return f"gum:{kind}:{subscription_id}:{fields.get('cancelled_at') or fields.get('ended_at') or fields.get('resource_name') or ''}"


def _user_plan_kind(fields: dict) -> str:
    """charge | cancel | ended for a Gumroad subscription ping."""
    resource = str(fields.get("resource_name") or "").lower()
    if resource in ("cancellation", "subscription_cancelled") or str(fields.get("cancelled", "")).lower() == "true":
        return "cancel"
    if resource in ("subscription_ended", "subscription_ended_notification") or str(fields.get("ended", "")).lower() == "true":
        return "ended"
    return "charge"


async def _process_user_plan_subscription(fields: dict, subscription_id: str, reference, accept_test: bool):
    """Per-user plans (card_plan / dev_*): first sale, renewal, cancel, ended.

    Returns None when this ping is not for a per-user plan (the caller carries on with the
    existing guild-premium logic), else (http_status, message). The webhook, never the browser,
    writes user_entitlements, and every event is claimed by its gateway id first."""
    from modules import user_billing, user_subs
    kind = _user_plan_kind(fields)
    owner = await db.entitlement_by_subscription(subscription_id)
    product = owner["product"] if owner else None
    user_id = owner["user_id"] if owner else None
    if not owner and reference:
        row = await db.get_payment_by_reference(reference)
        if row and row.get("provider") == PROVIDER and user_subs.is_plan(row.get("payment_type")):
            if row.get("status") != "pending":
                return 200, "already processed"
            product, user_id = row["payment_type"], str(row["user_id"])
    if not (product and user_id):
        return None
    if kind == "charge":
        if not _matches_product(product, fields):
            return 200, "product mismatch"
        if not user_subs.paid_enough(product, fields.get("price", 0)):
            return 200, "underpaid"
        if not accept_test and not await _sale_is_valid(fields.get("sale_id", ""), product):
            return 200, "sale not verified"
    try:
        result = await user_billing.apply_event(
            db, event_id=_user_plan_event_id(fields, kind, subscription_id), user_id=user_id, product=product,
            kind=kind, provider=PROVIDER, subscription_id=subscription_id)
    except Exception:
        logger.exception("[gumroad] user plan event failed (%s, user %s)", kind, user_id)
        return 500, "unlock failed"
    if kind == "charge" and result in ("applied", "duplicate") and reference:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE payment_logs SET status = 'completed' WHERE paystack_reference = $1 AND status = 'pending'",
                reference)
    if kind == "charge" and result == "applied":
        await _dm(int(user_id), None, f"✅ Your **{user_subs.PLANS[product]['label']}** payment was received. "
                                      "You can see the next renewal date under My account on the dashboard.")
    return 200, "ok"


async def _process_gumroad_ping_inner(fields: dict) -> tuple:
    """Returns (http_status, message). 200 = handled/ignored (don't retry);
    500 = unlock failed after claim (claim reverted, Gumroad may retry)."""
    import os
    await _auto.load_runtime()
    is_test = str(fields.get("test", "")).lower() == "true"
    accept_test = os.getenv("GUMROAD_ACCEPT_TEST_PINGS", "").strip().lower() in ("1", "true", "yes")
    if is_test and not accept_test:
        logger.info("[gumroad] test ping ignored (set GUMROAD_ACCEPT_TEST_PINGS=1 to unlock on your own test purchases)")
        return 200, "test ping ignored"
    if str(fields.get("refunded", "")).lower() == "true" or str(fields.get("disputed", "")).lower() == "true":
        logger.warning(f"[gumroad] refund/dispute ping for sale {fields.get('sale_id')} — manual review needed")
        return 200, "refund/dispute noted"

    reference = fields.get("url_params[reference]") or fields.get("reference")

    # Premium membership renewal charges: Gumroad only repeats the URL params
    # on the first charge, so later ones arrive with a subscription_id and no
    # order reference. Match them to the guild via that id.
    subscription_id = fields.get("subscription_id")
    if subscription_id:
        handled = await _process_user_plan_subscription(fields, subscription_id, reference, is_test and accept_test)
        if handled is not None:
            return handled
    if subscription_id and (str(fields.get("is_recurring_charge", "")).lower() == "true" or not reference):
        return await _process_premium_renewal(fields, subscription_id, is_test and accept_test)

    if not reference:
        logger.warning(f"[gumroad] sale {fields.get('sale_id')} has no reference (bought without the bot's link?)")
        return 200, "no reference"

    row = await db.get_payment_by_reference(reference)
    if not row or row.get("provider") != PROVIDER:
        logger.warning(f"[gumroad] unknown reference {reference!r}")
        return 200, "unknown reference"
    if row.get("status") != "pending":
        return 200, "already processed"

    payment_type = row["payment_type"]
    if not _matches_product(payment_type, fields):
        logger.warning(f"[gumroad] product mismatch for {reference}: {payment_type} vs {fields.get('product_name')!r}")
        return 200, "product mismatch"

    expected = expected_price_usd(payment_type)
    if payment_type == "premium_yearly":
        # 50% first-payment code: only the order it was reserved for, by the person it belongs to.
        from premium_discount import discount_for_reference, discounted_usd
        if await discount_for_reference(reference, row["user_id"]):
            expected = discounted_usd()
    try:
        paid_cents = int(float(fields.get("price", 0)))
    except (TypeError, ValueError):
        paid_cents = 0
    if expected is not None and paid_cents < round(expected * 100):
        logger.warning(f"[gumroad] underpaid {reference}: {paid_cents}c < {round(expected * 100)}c")
        return 200, "underpaid"

    if not (is_test and accept_test) and not await _sale_is_valid(fields.get("sale_id", ""), payment_type):
        logger.warning(f"[gumroad] sale verification failed for {reference}")
        return 200, "sale not verified"

    if payment_type in _GUILD_SCOPED_TYPES and not row.get("chat_id"):
        # Paid + verified but no server attached: don't strand the sale. Mark
        # it completed and DM the buyer a persistent server picker.
        return await _hold_for_server_choice(row, reference, paid_cents, subscription_id, fields)

    pool = await get_pool()
    async with pool.acquire() as conn:
        claimed = await conn.fetchrow(
            "UPDATE payment_logs SET status = 'completed', amount = $2 "
            "WHERE paystack_reference = $1 AND status = 'pending' RETURNING *",
            reference, paid_cents / 100.0,
        )
    if not claimed:
        return 200, "already processed"

    from payments_manual import UNLOCK_HANDLERS
    handler = UNLOCK_HANDLERS.get(payment_type)
    try:
        if handler is None:
            raise RuntimeError(f"no unlock handler for {payment_type}")
        await handler(reference, row["user_id"], row.get("chat_id"), row.get("clone_id"))
    except Exception:
        logger.exception(f"[gumroad] unlock failed for {reference}; reverting claim")
        async with pool.acquire() as conn:
            await conn.execute("UPDATE payment_logs SET status = 'pending' WHERE paystack_reference = $1", reference)
        return 500, "unlock failed"

    if payment_type == "premium_yearly":
        try:
            from premium_discount import mark_redeemed
            await mark_redeemed(reference)
        except Exception:
            logger.exception(f"[gumroad] couldn't mark discount redeemed for {reference}")

    if payment_type == "premium" and subscription_id and row.get("chat_id"):
        try:
            await db.set_premium_subscription_id(int(row["chat_id"]), row.get("clone_id"), subscription_id)
        except Exception:
            logger.exception(f"[gumroad] couldn't store subscription id for {reference}")

    logger.info(f"[gumroad] {reference} confirmed and unlocked ({payment_type}, user {row['user_id']})")
    await _dm(row["user_id"], row.get("clone_id"),
              f"✅ Your Gumroad payment for **{payment_type}** was confirmed and applied — enjoy!")
    return 200, "ok"


# Outcomes that mean "a real sale may have been paid but NOT unlocked" —
# every one of these used to just log a warning and return 200, so the owner
# only found out when a buyer complained. Now each one DMs the owner.
_ALERT_MESSAGES = {
    "no reference": "The buyer paid WITHOUT the bot's checkout button (no order reference), so nothing can be matched. Unlock them manually.",
    "unknown reference": "The order reference on this sale isn't in payment_logs as a pending Gumroad row (already handled, wrong provider, or stale).",
    "product mismatch": "The Gumroad product bought doesn't match the product for that order. Check GUMROAD_*_LINK / *_ID settings.",
    "underpaid": "The buyer paid less than the expected price.",
    "sale not verified": "Gumroad's API could not confirm this sale (bad/missing GUMROAD_ACCESS_TOKEN, refunded, or API error).",
    "no guild": "The order has no server (guild) attached, so a per-server unlock can't be applied. It was NOT unlocked and NOT retried. Find the server ID and unlock it by hand.",
    "unlock failed": "Payment was valid but the unlock handler crashed. Gumroad will retry; check the api logs.",
    "unknown subscription": "A recurring Premium charge arrived for a subscription the bot has no record of.",
    "refund/dispute noted": "A refund or dispute ping arrived. Review manually.",
}


async def _alert_owner(text: str) -> None:
    try:
        from discord_bot.dm_send import dm_user
        token = config.DISCORD_BOT_TOKEN
        if not token:
            return
        for owner_id in config.DISCORD_OWNER_BROADCAST_IDS:
            await dm_user(int(owner_id), text, token)
    except Exception:
        logger.exception("[gumroad] owner alert failed")


async def process_gumroad_ping(fields: dict) -> tuple:
    code, msg = await _process_gumroad_ping_inner(fields)
    if code >= 500 or msg in _ALERT_MESSAGES:
        detail = _ALERT_MESSAGES.get(msg, "Webhook processing failed.")
        await _alert_owner(
            "\u26a0\ufe0f **Gumroad sale needs attention** (result: `%s`)\n"
            "Sale: `%s` \u2022 Product: %s \u2022 Price: %s cents \u2022 Buyer: %s \u2022 Reference: `%s`\n%s" % (
                msg,
                fields.get("sale_id", "?"),
                fields.get("product_name", "?"),
                fields.get("price", "?"),
                fields.get("email", "?"),
                fields.get("url_params[reference]") or fields.get("reference") or "none",
                detail,
            )
        )
    return code, msg
