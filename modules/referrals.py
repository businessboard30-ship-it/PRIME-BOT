"""
Ad/Marketplace Referral System (Phase 4)

Layers a referral-code system on top of modules/ads_marketplace.py:
  - Any user can generate a personal code (/referral mycode).
  - A user redeems someone else's code once (/referral use <code>), which
    sets users.ad_referred_by permanently (first code wins — redeeming
    again does nothing, and a code can't be used on yourself).
  - When a referred user submits an ad or lists a marketplace service,
    ads_marketplace.py calls record_referral_earning() to log a ledger row.

IMPORTANT — this is a tracked ledger, not real money. ad_submissions.budget_usd
and services_listings.price_usd were never wired to actual payment collection
(see ads_marketplace.py's module docstring), so "earnings" here are a proposed
commission figure only, computed at COMMISSION_RATE of the submitted budget/
price. /referral stats labels this clearly so it isn't mistaken for a payout
balance. Wiring a real payout would need its own design (who pays, when,
dispute handling) — out of scope for this phase.
"""

import random
import string
from typing import Optional, Dict

from database import get_pool

COMMISSION_RATE = 0.10  # proposed/tracked-only commission on referred activity


def _generate_code(length: int = 8) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


async def get_or_create_referral_code(user_id: int) -> Optional[str]:
    """Return the user's existing code, or generate + store a new one."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT code FROM ad_referral_codes WHERE user_id = $1", user_id
            )
            if row:
                return row["code"]
            # Small retry loop in case of a random collision (unique constraint).
            for _ in range(5):
                code = _generate_code()
                try:
                    await conn.execute(
                        "INSERT INTO ad_referral_codes (user_id, code) VALUES ($1, $2)",
                        user_id, code,
                    )
                    return code
                except Exception:
                    continue
        return None
    except Exception as e:
        print(f"[v0] Error creating referral code: {e}")
        return None


async def use_referral_code(user_id: int, code: str) -> Dict:
    """Redeem a code. Returns {"ok": bool, "reason": str}. reason is one of:
    'applied', 'already_set', 'self', 'not_found'."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                existing = await conn.fetchval(
                    "SELECT ad_referred_by FROM users WHERE user_id = $1", user_id
                )
                if existing is not None:
                    return {"ok": False, "reason": "already_set"}

                owner = await conn.fetchrow(
                    "SELECT user_id FROM ad_referral_codes WHERE code = $1", code.strip().upper()
                )
                if not owner:
                    return {"ok": False, "reason": "not_found"}
                if owner["user_id"] == user_id:
                    return {"ok": False, "reason": "self"}

                await conn.execute(
                    "UPDATE users SET ad_referred_by = $1 WHERE user_id = $2",
                    owner["user_id"], user_id,
                )
                return {"ok": True, "reason": "applied"}
    except Exception as e:
        print(f"[v0] Error using referral code: {e}")
        return {"ok": False, "reason": "error"}


async def get_referrer(user_id: int) -> Optional[int]:
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT ad_referred_by FROM users WHERE user_id = $1", user_id
            )
    except Exception as e:
        print(f"[v0] Error fetching referrer: {e}")
        return None


async def record_referral_earning(referred_user_id: int, source_type: str,
                                   source_id: str, base_amount_usd: float) -> bool:
    """Called by ads_marketplace.py after a referred user submits an ad or
    lists a service. No-op if the user has no referrer. Logs
    COMMISSION_RATE * base_amount_usd as a tracked (not real) figure."""
    referrer_id = await get_referrer(referred_user_id)
    if not referrer_id:
        return False
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO ad_referral_ledger
                    (referrer_id, referred_user_id, source_type, source_id, tracked_amount_usd)
                VALUES ($1, $2, $3, $4, $5)
            """, referrer_id, referred_user_id, source_type, str(source_id),
                round(base_amount_usd * COMMISSION_RATE, 2))
        return True
    except Exception as e:
        print(f"[v0] Error recording referral earning: {e}")
        return False


async def get_referral_stats(user_id: int) -> Dict:
    """Returns {code, referred_count, tracked_total_usd, recent: [rows]}."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            code_row = await conn.fetchrow(
                "SELECT code FROM ad_referral_codes WHERE user_id = $1", user_id
            )
            referred_count = await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE ad_referred_by = $1", user_id
            )
            total = await conn.fetchval(
                "SELECT COALESCE(SUM(tracked_amount_usd), 0) FROM ad_referral_ledger WHERE referrer_id = $1",
                user_id,
            )
            recent = await conn.fetch("""
                SELECT source_type, source_id, tracked_amount_usd, created_at
                FROM ad_referral_ledger WHERE referrer_id = $1
                ORDER BY created_at DESC LIMIT 5
            """, user_id)
        return {
            "code": code_row["code"] if code_row else None,
            "referred_count": referred_count or 0,
            "tracked_total_usd": float(total or 0),
            "recent": [dict(r) for r in recent],
        }
    except Exception as e:
        print(f"[v0] Error fetching referral stats: {e}")
        return {"code": None, "referred_count": 0, "tracked_total_usd": 0.0, "recent": []}
