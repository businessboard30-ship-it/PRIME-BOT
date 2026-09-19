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
                                 reference: Optional[str] = None) -> str:
    """Same calling convention as payments_manual.start_manual_payment
    (call after interaction.response.defer). Returns the reference."""
    user = interaction.user
    reference = reference or new_reference(payment_type, user.id)
    clone_id = getattr(interaction.client, "clone_id", None)
    link = build_link(payment_type, user.id, reference)
    if not link:
        await interaction.followup.send("Checkout isn't set up for this yet — please try again later.", ephemeral=True)
        logger.error(f"[gumroad] no GUMROAD_PRODUCT_LINKS entry for {payment_type}")
        return reference

    await db.log_payment(
        user.id, expected_price_usd(payment_type) or 0.0, reference, status="pending",
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


async def process_gumroad_ping(fields: dict) -> tuple:
    """Returns (http_status, message). 200 = handled/ignored (don't retry);
    500 = unlock failed after claim (claim reverted, Gumroad may retry)."""
    if str(fields.get("test", "")).lower() == "true":
        return 200, "test ping ignored"
    if str(fields.get("refunded", "")).lower() == "true" or str(fields.get("disputed", "")).lower() == "true":
        logger.warning(f"[gumroad] refund/dispute ping for sale {fields.get('sale_id')} — manual review needed")
        return 200, "refund/dispute noted"

    reference = fields.get("url_params[reference]") or fields.get("reference")
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

    if not await _sale_is_valid(fields.get("sale_id", ""), payment_type):
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

    logger.info(f"[gumroad] {reference} confirmed and unlocked ({payment_type}, user {row['user_id']})")
    await _dm(row["user_id"], row.get("clone_id"),
              f"✅ Your Gumroad payment for **{payment_type}** was confirmed and applied — enjoy!")
    return 200, "ok"
