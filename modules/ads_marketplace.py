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


# ── Ad auto-bump settings (owner-editable via /ad autobump) ────────────
# Stored in admin_config so the serverless placement cron
# (api/cron_ad_placement.py) and the gateway wizard read the same values.

AUTOBUMP_ENABLED_KEY = "ad_autobump_enabled"
AUTOBUMP_INTERVAL_KEY = "ad_autobump_interval_seconds"
AUTOBUMP_DEFAULT_INTERVAL = 6 * 60 * 60      # the old hardcoded 6h
AUTOBUMP_MIN_INTERVAL = 15 * 60              # floor so a typo can't spam bump channels
AUTOBUMP_MAX_INTERVAL = 7 * 24 * 60 * 60


def format_interval(seconds: int) -> str:
    seconds = int(seconds)
    if seconds % 3600 == 0:
        h = seconds // 3600
        return f"{h} hour" + ("" if h == 1 else "s")
    if seconds % 60 == 0 and seconds < 3600:
        return f"{seconds // 60} min"
    return f"{seconds / 3600:.1f} hours"


async def _config_get(key: str) -> Optional[str]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT value FROM admin_config WHERE key = $1", key)


async def _config_set(key: str, value) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO admin_config (key, value, updated_at) VALUES ($1, $2, NOW())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
            """,
            key, str(value),
        )


async def get_autobump_settings() -> Dict:
    """{'enabled': bool, 'interval_seconds': int}. Defaults (on, 6h) match the
    behaviour before this was configurable, so nothing changes until edited."""
    enabled, interval = True, AUTOBUMP_DEFAULT_INTERVAL
    try:
        raw_enabled = await _config_get(AUTOBUMP_ENABLED_KEY)
        if raw_enabled is not None:
            enabled = str(raw_enabled).strip().lower() not in ("0", "false", "off", "no")
        raw_interval = await _config_get(AUTOBUMP_INTERVAL_KEY)
        if raw_interval is not None:
            interval = max(AUTOBUMP_MIN_INTERVAL, min(AUTOBUMP_MAX_INTERVAL, int(raw_interval)))
    except Exception as e:
        print(f"[v0] Error reading ad auto-bump settings: {e}")
    return {"enabled": enabled, "interval_seconds": interval}


async def set_autobump_enabled(enabled: bool) -> bool:
    try:
        await _config_set(AUTOBUMP_ENABLED_KEY, "1" if enabled else "0")
        return True
    except Exception as e:
        print(f"[v0] Error saving ad auto-bump on/off: {e}")
        return False


async def set_autobump_interval(seconds: int) -> Optional[int]:
    """Saves the repeat interval (clamped to 15 min - 7 days). Returns the
    value actually stored, or None on failure."""
    seconds = max(AUTOBUMP_MIN_INTERVAL, min(AUTOBUMP_MAX_INTERVAL, int(seconds)))
    try:
        await _config_set(AUTOBUMP_INTERVAL_KEY, seconds)
        return seconds
    except Exception as e:
        print(f"[v0] Error saving ad auto-bump interval: {e}")
        return None


async def get_autobump_stats() -> Dict:
    """{'last_posted_at': datetime|None, 'posted_24h': int} from ad_placements."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT MAX(posted_at) AS last_posted_at,
                       COUNT(*) FILTER (WHERE posted_at > NOW() - INTERVAL '24 hours') AS posted_24h
                FROM ad_placements
                """
            )
        return {"last_posted_at": row["last_posted_at"], "posted_24h": int(row["posted_24h"] or 0)}
    except Exception as e:
        print(f"[v0] Error reading ad auto-bump stats: {e}")
        return {"last_posted_at": None, "posted_24h": 0}


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


async def list_ads_for_manager(limit: int = 25) -> List[Dict]:
    """Ads for the owner's /ad manage picker: pending ones first (they're the
    ones that need action), then the most recently submitted. Rejected ads are
    left out — they're finished business (the row is kept, so the submitter's
    /ad status still shows the rejection reason). 25 = Discord's select-menu
    option cap."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id, company_name, ad_title, status
                FROM ad_submissions
                WHERE status <> 'rejected'
                ORDER BY (status = 'pending') DESC, submitted_at DESC
                LIMIT $1
            """, limit)
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[v0] Error listing ads for manager: {e}")
        return []


async def count_ads_by_status() -> Dict[str, int]:
    """{'pending': n, 'approved': n, 'deactivated': n, 'rejected': n} (missing = 0)."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT status, COUNT(*) AS n FROM ad_submissions GROUP BY status")
        return {r["status"]: int(r["n"]) for r in rows}
    except Exception as e:
        print(f"[v0] Error counting ads: {e}")
        return {}


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
