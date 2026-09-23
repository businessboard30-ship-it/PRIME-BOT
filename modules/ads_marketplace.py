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
                     ad_description: str, target_url: str, budget_usd: float,
                     image_channel_id: Optional[int] = None,
                     image_message_id: Optional[int] = None) -> Optional[int]:
    """Submit an ad for owner approval. Returns the new ad's id, or None on failure."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO ad_submissions
                    (user_id, company_name, ad_title, ad_description, target_url, budget_usd, status,
                     image_channel_id, image_message_id)
                VALUES ($1, $2, $3, $4, $5, $6, 'pending', $7, $8)
                RETURNING id
            """, user_id, company_name, ad_title, ad_description, target_url, budget_usd,
                image_channel_id, image_message_id)
        ad_id = row["id"] if row else None
        if ad_id is not None:
            # Tracked-only referral commission — see modules/referrals.py docstring.
            await record_referral_earning(user_id, "ad", ad_id, budget_usd)
        return ad_id
    except Exception as e:
        print(f"[v0] Error submitting ad: {e}")
        return None


async def set_ad_image(ad_id: int, user_id: Optional[int], image_channel_id: int, image_message_id: int) -> bool:
    """Attach/replace the image on an ad. With a user_id, only that user's own
    non-rejected ad qualifies; user_id=None is the bot-owner path (any ad)."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            if user_id is None:
                result = await conn.execute("""
                    UPDATE ad_submissions SET image_channel_id = $2, image_message_id = $3
                    WHERE id = $1
                """, ad_id, image_channel_id, image_message_id)
            else:
                result = await conn.execute("""
                    UPDATE ad_submissions SET image_channel_id = $3, image_message_id = $4
                    WHERE id = $1 AND user_id = $2 AND status <> 'rejected'
                """, ad_id, user_id, image_channel_id, image_message_id)
        return result.endswith("1")
    except Exception as e:
        print(f"[v0] Error setting ad image: {e}")
        return False


_EDITABLE_AD_FIELDS = ("company_name", "ad_title", "ad_description", "target_url")


async def update_ad_fields(ad_id: int, **fields) -> bool:
    """Bot-owner edit of any ad's text fields, regardless of status. Only the
    whitelisted columns are ever touched; None values are ignored."""
    updates = {k: v for k, v in fields.items() if k in _EDITABLE_AD_FIELDS and v is not None}
    if not updates:
        return False
    try:
        pool = await get_pool()
        sets = ", ".join(f"{col} = ${i}" for i, col in enumerate(updates, start=2))
        async with pool.acquire() as conn:
            result = await conn.execute(
                f"UPDATE ad_submissions SET {sets} WHERE id = $1", ad_id, *updates.values()
            )
        return result.endswith("1")
    except Exception as e:
        print(f"[v0] Error updating ad: {e}")
        return False


async def get_pending_ads(limit: int = 10) -> List[Dict]:
    """List ads awaiting owner approval, oldest first."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id, user_id, company_name, ad_title, ad_description, target_url, budget_usd, submitted_at,
                       image_channel_id, image_message_id
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
                SELECT id, company_name, ad_title, ad_description, target_url,
                       image_channel_id, image_message_id
                FROM ad_submissions WHERE status = 'approved'
                ORDER BY approved_at DESC LIMIT $1
            """, limit)
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[v0] Error fetching active ads: {e}")
        return []


async def deactivate_ad(ad_id: int) -> bool:
    """Take a live ad off every surface without deleting it. get_active_ads
    (join DM, bump channels, /ad active) only ever returns status='approved',
    so flipping to 'deactivated' is all it takes — no schema change, and the
    ad (image, budget, history) stays intact for reactivate_ad."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE ad_submissions SET status = 'deactivated'
                WHERE id = $1 AND status = 'approved'
            """, ad_id)
        return result.endswith("1")
    except Exception as e:
        print(f"[v0] Error deactivating ad: {e}")
        return False


async def reactivate_ad(ad_id: int) -> bool:
    """Put a deactivated ad back live. Only 'deactivated' ads qualify, so this
    can never be used to sneak a pending/rejected ad past approve_ad."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE ad_submissions SET status = 'approved'
                WHERE id = $1 AND status = 'deactivated'
            """, ad_id)
        return result.endswith("1")
    except Exception as e:
        print(f"[v0] Error reactivating ad: {e}")
        return False


async def search_ads(user_id: Optional[int] = None, query: str = "",
                     statuses: Optional[tuple] = None, limit: int = 25) -> List[Dict]:
    """Ads for Discord autocomplete so nobody has to type an ad id. user_id=None
    is the bot-owner view (every ad); otherwise only that user's own ads.
    query matches company/title text, or the exact id if the caller happens to
    know it. statuses optionally narrows to e.g. ('pending',)."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id, company_name, ad_title, status
                FROM ad_submissions
                WHERE ($1::bigint IS NULL OR user_id = $1)
                  AND ($2::text[] IS NULL OR status = ANY($2::text[]))
                  AND ($3 = '' OR company_name ILIKE '%' || $3 || '%'
                       OR ad_title ILIKE '%' || $3 || '%' OR id::text = $3)
                ORDER BY submitted_at DESC
                LIMIT $4
            """, user_id, list(statuses) if statuses else None, query.strip(), limit)
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[v0] Error searching ads: {e}")
        return []


async def get_pending_ads_awaiting_payment_reminder(min_age_seconds: int = 1800, limit: int = 25) -> List[Dict]:
    """Pending ads that are old enough to have plausibly stalled at
    checkout and haven't been auto-nudged yet (payment_reminder_sent_at
    IS NULL) — this is the "old ones" that never got a payment link
    reminder DM. See claim_ad_payment_reminder for the actual once-only
    guarantee."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, user_id, company_name, ad_title, budget_usd
                FROM ad_submissions
                WHERE status = 'pending'
                  AND payment_reminder_sent_at IS NULL
                  AND submitted_at <= NOW() - ($1 * INTERVAL '1 second')
                ORDER BY submitted_at ASC
                LIMIT $2
                """,
                min_age_seconds, limit,
            )
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[v0] Error fetching ads awaiting payment reminder: {e}")
        return []


async def claim_ad_payment_reminder(ad_id: int) -> bool:
    """Atomic once-only claim: True only for the single caller that wins
    the race to send this ad's reminder DM (matters because this loop
    runs in every bot process — main bot and every clone — so more than
    one process can see the same due ad in the same tick)."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE ad_submissions SET payment_reminder_sent_at = NOW()
                WHERE id = $1 AND payment_reminder_sent_at IS NULL AND status = 'pending'
                """,
                ad_id,
            )
        return result.endswith("1")
    except Exception as e:
        print(f"[v0] Error claiming ad payment reminder for ad {ad_id}: {e}")
        return False


async def unclaim_ad_payment_reminder(ad_id: int) -> None:
    """Rolls back a claim that didn't actually result in a sent DM (e.g.
    this process doesn't share a server with the submitter) so a
    different process gets a chance on the next hourly pass instead of
    the ad silently never being followed up on."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE ad_submissions SET payment_reminder_sent_at = NULL WHERE id = $1", ad_id,
            )
    except Exception as e:
        print(f"[v0] Error unclaiming ad payment reminder for ad {ad_id}: {e}")


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
