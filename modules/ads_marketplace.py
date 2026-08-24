"""
Ads & Marketplace Adapter
Ported from SUPER-BOT's submit_ad/approve_ad/reject_ad/get_active_ads and
list_service/get_marketplace_listings/get_my_listings.

Uses the ad_submissions and services_listings tables that already existed in
database.py's schema but had no code using them until now. Row-id based
approve/reject (not list-index) since a list index is fragile against
concurrent admin actions.
"""

import time
from typing import List, Optional, Dict
from database import get_pool
from modules.referrals import record_referral_earning


# ── Ads (owner-approved) ──────────────────────────────────────────────

async def submit_ad(user_id: int, company_name: str, ad_title: str,
                     ad_description: str, target_url: str, budget_usd: float) -> Optional[int]:
    """Submit an ad for owner approval. Returns the new ad's id, or None on failure."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO ad_submissions
                    (user_id, company_name, ad_title, ad_description, target_url, budget_usd, status)
                VALUES ($1, $2, $3, $4, $5, $6, 'pending')
                RETURNING id
            """, user_id, company_name, ad_title, ad_description, target_url, budget_usd)
        ad_id = row["id"] if row else None
        if ad_id is not None:
            # Tracked-only referral commission — see modules/referrals.py docstring.
            await record_referral_earning(user_id, "ad", ad_id, budget_usd)
        return ad_id
    except Exception as e:
        print(f"[v0] Error submitting ad: {e}")
        return None


async def get_pending_ads(limit: int = 10) -> List[Dict]:
    """List ads awaiting owner approval, oldest first."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id, user_id, company_name, ad_title, ad_description, target_url, budget_usd, submitted_at
                FROM ad_submissions WHERE status = 'pending'
                ORDER BY submitted_at ASC LIMIT $1
            """, limit)
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[v0] Error fetching pending ads: {e}")
        return []


async def get_ad(ad_id: int) -> Optional[Dict]:
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM ad_submissions WHERE id = $1", ad_id)
        return dict(row) if row else None
    except Exception as e:
        print(f"[v0] Error fetching ad: {e}")
        return None


async def approve_ad(ad_id: int) -> bool:
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE ad_submissions SET status = 'approved', approved_at = NOW()
                WHERE id = $1 AND status = 'pending'
            """, ad_id)
        return result.endswith("1")
    except Exception as e:
        print(f"[v0] Error approving ad: {e}")
        return False


async def reject_ad(ad_id: int, reason: str) -> bool:
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE ad_submissions SET status = 'rejected', rejection_reason = $2
                WHERE id = $1 AND status = 'pending'
            """, ad_id, reason)
        return result.endswith("1")
    except Exception as e:
        print(f"[v0] Error rejecting ad: {e}")
        return False


async def get_active_ads(limit: int = 5) -> List[Dict]:
    """Approved ads, most recently approved first — for user-facing display."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id, company_name, ad_title, ad_description, target_url
                FROM ad_submissions WHERE status = 'approved'
                ORDER BY approved_at DESC LIMIT $1
            """, limit)
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[v0] Error fetching active ads: {e}")
        return []


# ── Services Marketplace ──────────────────────────────────────────────

async def list_service(user_id: int, service_name: str, service_title: str,
                        description: str, price_usd: float, category: str = "general") -> Optional[str]:
    """User lists a service for sale. Returns the new listing's id."""
    try:
        listing_id = f"{user_id}_{int(time.time())}"
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO services_listings
                    (id, user_id, service_name, service_title, description, price_usd, category, status)
                VALUES ($1, $2, $3, $4, $5, $6, $7, 'active')
            """, listing_id, user_id, service_name, service_title, description, price_usd, category)
        # Tracked-only referral commission — see modules/referrals.py docstring.
        await record_referral_earning(user_id, "listing", listing_id, price_usd)
        return listing_id
    except Exception as e:
        print(f"[v0] Error listing service: {e}")
        return None


async def get_marketplace_listings(limit: int = 10, offset: int = 0) -> List[Dict]:
    """Browse active listings, most recent first."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM services_listings WHERE status = 'active'
                ORDER BY created_at DESC LIMIT $1 OFFSET $2
            """, limit, offset)
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[v0] Error fetching marketplace listings: {e}")
        return []


async def get_my_listings(user_id: int) -> List[Dict]:
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM services_listings WHERE user_id = $1 AND status = 'active'
                ORDER BY created_at DESC
            """, user_id)
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[v0] Error fetching user listings: {e}")
        return []


async def get_listing(listing_id: str) -> Optional[Dict]:
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM services_listings WHERE id = $1", listing_id)
        return dict(row) if row else None
    except Exception as e:
        print(f"[v0] Error fetching listing: {e}")
        return None


async def deactivate_listing(user_id: int, listing_id: str) -> bool:
    """Owner-only removal of their own listing."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE services_listings SET status = 'removed'
                WHERE id = $1 AND user_id = $2
            """, listing_id, user_id)
        return result.endswith("1")
    except Exception as e:
        print(f"[v0] Error removing listing: {e}")
        return False


async def record_listing_click(listing_id: str) -> bool:
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE services_listings SET clicks = clicks + 1 WHERE id = $1", listing_id
            )
        return True
    except Exception as e:
        print(f"[v0] Error recording listing click: {e}")
        return False
