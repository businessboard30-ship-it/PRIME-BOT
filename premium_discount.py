# path: premium_discount.py

"""50% off the Yearly Premium plan, first payment only.

How it can't be stolen or shared:
  * One code per person, ever (UNIQUE (user_id, payment_type)), recorded in
    premium_discount_codes.
  * The code is never shown in chat — it only travels inside that buyer's own
    pay link (the chat only ever shows a masked tail).
  * On Gumroad it is a single-use offer code (max_purchase_count=1), created
    for that one person.
  * The bot only accepts the discounted price for the exact order reference the
    code was reserved for AND the same Discord user (see discount_for_reference).
    A leaked code used on any other order pays the 50% at Gumroad but is rejected
    here as underpaid, so it never unlocks anything and the owner gets an alert.
  * "First payment" = the person has no completed Premium payment of any plan.
"""

import logging
import secrets
from typing import Optional, Tuple

import aiohttp

import config
from database import db, get_pool

logger = logging.getLogger(__name__)

PLAN = "premium_yearly"
PERCENT_OFF = 50
PREMIUM_TYPES = ("premium", "premium_yearly", "premium_lifetime")
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I
OFFER_API = "https://api.gumroad.com/v2/products/{pid}/offer_codes"


def discounted_usd() -> float:
    return round(config.PREMIUM_YEARLY_FEE_USD * (100 - PERCENT_OFF) / 100, 2)


def mask(code: str) -> str:
    return f"PB50-••••{code[-4:]}" if code else ""


def _new_code() -> str:
    return "PB50" + "".join(secrets.choice(_ALPHABET) for _ in range(10))


async def has_prior_premium_payment(user_id: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM payment_logs WHERE user_id = $1 AND payment_type = ANY($2::text[]) "
            "AND status = 'completed' LIMIT 1",
            user_id, list(PREMIUM_TYPES),
        )
    return row is not None


async def get_code(user_id: int) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM premium_discount_codes WHERE user_id = $1 AND payment_type = $2", user_id, PLAN)
    return dict(row) if row else None


async def eligible(user_id: int) -> Tuple[bool, str]:
    """(ok, reason-if-not). Redeemed/revoked codes and earlier Premium payments both disqualify."""
    existing = await get_code(user_id)
    if existing and existing["status"] in ("redeemed", "revoked"):
        return False, "You've already used your first-payment discount."
    if await has_prior_premium_payment(user_id):
        return False, "The 50% discount is for a first Premium payment only."
    return True, ""


async def issue(user_id: int) -> dict:
    """Create the person's one code (idempotent — returns the existing row if there is one)."""
    existing = await get_code(user_id)
    if existing:
        return existing
    pool = await get_pool()
    for _ in range(5):  # code collisions are astronomically unlikely, but the column is UNIQUE
        code = _new_code()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO premium_discount_codes (code, user_id, payment_type, percent_off) "
                "VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING RETURNING *",
                code, user_id, PLAN, PERCENT_OFF,
            )
        if row:
            return dict(row)
        existing = await get_code(user_id)
        if existing:
            return existing
    raise RuntimeError("couldn't allocate a discount code")


async def ensure_gumroad_offer(row: dict) -> bool:
    """Create the single-use Gumroad offer code for this person (once). False if Gumroad refused."""
    if row.get("gumroad_offer_id"):
        return True
    token = config.GUMROAD_ACCESS_TOKEN
    import gumroad_autocreate as _auto
    await _auto.load_runtime()
    pid = _auto.id_for(PLAN)
    if not token or not pid:
        logger.warning("[discount] missing GUMROAD_ACCESS_TOKEN or %s product id", PLAN)
        return False
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.post(OFFER_API.format(pid=pid), data={
                "access_token": token, "name": row["code"], "amount_off": PERCENT_OFF,
                "offer_type": "percent", "max_purchase_count": 1,
            }) as r:
                data = await r.json(content_type=None)
    except Exception:
        logger.exception("[discount] offer-code request failed")
        return False
    offer = (data or {}).get("offer_code") if isinstance(data, dict) else None
    if not (isinstance(data, dict) and data.get("success") and offer):
        logger.warning("[discount] Gumroad refused offer code: %s", str(data)[:200])
        return False
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE premium_discount_codes SET gumroad_offer_id = $2 WHERE id = $1", row["id"], str(offer.get("id") or "1"))
    row["gumroad_offer_id"] = str(offer.get("id") or "1")
    return True


async def reserve(user_id: int, reference: str, guild_id: int) -> bool:
    """Pin the code to ONE order reference + server. Only an unused code can be reserved."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE premium_discount_codes SET status = 'reserved', reserved_reference = $3, guild_id = $4 "
            "WHERE user_id = $1 AND payment_type = $2 AND status IN ('issued', 'reserved') RETURNING id",
            user_id, PLAN, reference, guild_id,
        )
    return row is not None


async def discount_for_reference(reference: str, user_id: int) -> Optional[dict]:
    """The reserved code for exactly this order + this Discord user, else None."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM premium_discount_codes WHERE reserved_reference = $1 AND user_id = $2 "
            "AND payment_type = $3 AND status = 'reserved'", reference, user_id, PLAN,
        )
    return dict(row) if row else None


async def mark_redeemed(reference: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE premium_discount_codes SET status = 'redeemed', redeemed_at = NOW() "
            "WHERE reserved_reference = $1 AND status = 'reserved'", reference)


async def start_discount_checkout(interaction, guild, clone_id) -> None:
    """Call AFTER interaction.response.defer(ephemeral=True). Gumroad checkout at 50% off."""
    from gumroad_payments import start_gumroad_payment, new_reference
    ok, why = await eligible(interaction.user.id)
    if not ok:
        await interaction.followup.send(f"🎟️ {why}", ephemeral=True)
        return
    row = await issue(interaction.user.id)
    if not await ensure_gumroad_offer(row):
        await interaction.followup.send(
            "🎟️ I couldn't set up your discount just now — please try again in a few minutes.", ephemeral=True)
        return
    reference = new_reference(PLAN, interaction.user.id)
    if not await reserve(interaction.user.id, reference, guild.id):
        await interaction.followup.send("🎟️ Your discount isn't available any more.", ephemeral=True)
        return
    price = discounted_usd()
    await start_gumroad_payment(
        interaction, PLAN, f"${price:g} (50% off Yearly — first payment)",
        guild_id=guild.id, reference=reference, amount_usd=price, discount_code=row["code"],
        intro=f"🎟️ **Your personal 50% discount is applied** — `{mask(row['code'])}`. "
              f"It only works for you, once, on the button below.",
    )
