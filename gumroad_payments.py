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

import logging
import secrets
from typing import Optional
from urllib.parse import urlencode

import aiohttp
import discord

import config
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
    "hardcore_roast": "HARDCORE_ROAST_FEE_USD",
    "ad_placement": "AD_PLACEMENT_FEE_USD",
}


def expected_price_usd(payment_type: str) -> Optional[float]:
    if payment_type in _PRICE_ATTRS:
        return float(getattr(config, _PRICE_ATTRS[payment_type]))
    tier = config.XP_SERVER_BOOST_TIERS.get(payment_type)
    return float(tier["fee_usd"]) if tier else None


def new_reference(payment_type: str, user_id: int) -> str:
    return f"gum_{payment_type}_{user_id}_{secrets.token_hex(4)}"


def build_link(payment_type: str, user_id: int, reference: str) -> Optional[str]:
    base = config.GUMROAD_PRODUCT_LINKS.get(payment_type)
    if not base:
        return None
    params = {"wanted": "true", "reference": reference}
    return f"{base}{'&' if '?' in base else '?'}{urlencode(params)}"


async def start_gumroad_payment(interaction: discord.Interaction, payment_type: str,
                                 amount_display: str, guild_id: Optional[int] = None,
                                 reference: Optional[str] = None,
                                 amount_usd: Optional[float] = None) -> str:
    """Same calling convention as payments_manual.start_manual_payment
    (call after interaction.response.defer). Returns the reference.

    amount_usd: logs this instead of expected_price_usd(payment_type) — for
    variable-price items (e.g. ad_placement, where the buyer names a budget
    at/above the product's minimum) so payment_logs reflects what they
    actually agreed to pay, not just the floor price."""
    user = interaction.user
    reference = reference or new_reference(payment_type, user.id)
    clone_id = getattr(interaction.client, "clone_id", None)
    link = build_link(payment_type, user.id, reference)
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
    await interaction.followup.send(
        f"Pay **{amount_display}** on Gumroad using the button below. "
        f"Use this exact button — it carries your order reference. "
        f"Your purchase is confirmed and unlocked automatically within a few seconds of paying; "
        f"you'll get a DM when it's done.",
        view=view, ephemeral=True,
    )
    return reference


def _matches_product(payment_type: str, fields: dict) -> bool:
    url = config.GUMROAD_PRODUCT_LINKS.get(payment_type, "")
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    pid = config.GUMROAD_PRODUCT_IDS.get(payment_type)
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


async def process_gumroad_ping(fields: dict) -> tuple:
    """Returns (http_status, message). 200 = handled/ignored (don't retry);
    500 = unlock failed after claim (claim reverted, Gumroad may retry)."""
    import os
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

    if payment_type == "premium" and subscription_id and row.get("chat_id"):
        try:
            await db.set_premium_subscription_id(int(row["chat_id"]), row.get("clone_id"), subscription_id)
        except Exception:
            logger.exception(f"[gumroad] couldn't store subscription id for {reference}")

    logger.info(f"[gumroad] {reference} confirmed and unlocked ({payment_type}, user {row['user_id']})")
    await _dm(row["user_id"], row.get("clone_id"),
              f"✅ Your Gumroad payment for **{payment_type}** was confirmed and applied — enjoy!")
    return 200, "ok"
