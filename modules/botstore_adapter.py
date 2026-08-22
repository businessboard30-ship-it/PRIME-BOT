"""
BotStore Adapter — Listings directory (bots, groups, channels)
Uses PostgreSQL for durable persistence (not JSON files)
- Search, browse, trending listings
- Admin-configurable pricing and durations
"""

import uuid
from typing import Dict, List, Optional
from database import get_pool


# Admin configs stored in database (loaded at startup)
class ConfigCache:
    FEATURED_PRICE_GHS = 20
    FEATURED_DAYS = 30
    PREMIUM_PRICE_GHS = 500
    FREE_BOT_LIMIT = 2
    
    @classmethod
    async def load(cls):
        """Load config from database on startup"""
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                config = await conn.fetchrow(
                    "SELECT * FROM botstore_config LIMIT 1"
                )
                if config:
                    cls.FEATURED_PRICE_GHS = config.get('featured_price', 20)
                    cls.FEATURED_DAYS = config.get('featured_days', 30)
                    cls.PREMIUM_PRICE_GHS = config.get('premium_price', 500)
                    cls.FREE_BOT_LIMIT = config.get('free_bot_limit', 2)
        except Exception as e:
            print(f"[v0] Warning loading BotStore config: {e}")

# Access config values
@property
def FEATURED_PRICE_GHS():
    return ConfigCache.FEATURED_PRICE_GHS

@property
def FEATURED_DAYS():
    return ConfigCache.FEATURED_DAYS

@property
def PREMIUM_PRICE_GHS():
    return ConfigCache.PREMIUM_PRICE_GHS

@property
def FREE_BOT_LIMIT():
    return ConfigCache.FREE_BOT_LIMIT

LISTING_TYPES = ["bot", "group", "channel"]
CATEGORIES = [
    "Finance", "Games", "Utility", "AI & Productivity", "Education",
    "Entertainment", "Crypto", "News", "Shopping", "Community", "Other"
]

async def get_listing(listing_id: str) -> Optional[Dict]:
    """Get a listing from database"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM botstore_listings WHERE id = $1", listing_id
        )
        return dict(row) if row else None

async def save_listing(listing: Dict) -> bool:
    """Save or update listing in database"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO botstore_listings (id, owner_id, type, category, title, description, link, status)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (id) DO UPDATE SET
                title = EXCLUDED.title,
                description = EXCLUDED.description,
                status = EXCLUDED.status
        """, listing.get('id'), listing.get('owner_id'), listing.get('type'),
            listing.get('category'), listing.get('title'), listing.get('description'),
            listing.get('link'), listing.get('status', 'pending'))
    return True

def new_listing_id() -> str:
    return uuid.uuid4().hex[:10]

def to_url(identifier: str) -> str:
    """Convert @username or link to tappable https://t.me/ URL"""
    ident = (identifier or "").strip()
    if ident.startswith("http://") or ident.startswith("https://"):
        return ident
    if ident.startswith("t.me/"):
        return f"https://{ident}"
    if ident.startswith("@"):
        ident = ident[1:]
    return f"https://t.me/{ident}"

async def add_listing(owner_id: int, listing_type: str, identifier: str, title: str,
                      description: str, category: str) -> Optional[str]:
    """Add a new listing to database"""
    listing_id = new_listing_id()
    try:
        await save_listing({
            "id": listing_id,
            "owner_id": owner_id,
            "type": listing_type,
            "category": category,
            "title": title,
            "description": description,
            "link": to_url(identifier),
            "status": "live"
        })
        return listing_id
    except Exception as e:
        print(f"[v0] Error adding listing: {e}")
        return None

async def update_listing(listing_id: str, **fields) -> bool:
    """Update listing fields in database"""
    try:
        listing = await get_listing(listing_id)
        if not listing:
            return False
        listing.update(fields)
        await save_listing(listing)
        return True
    except Exception as e:
        print(f"[v0] Error updating listing: {e}")
        return False

async def search_listings(query: str, listing_type: Optional[str] = None) -> List[Dict]:
    """Search listings by title/description"""
    try:
        pool = await get_pool()
        q = f"%{query.lower()}%"
        async with pool.acquire() as conn:
            if listing_type:
                rows = await conn.fetch("""
                    SELECT * FROM botstore_listings 
                    WHERE status = 'live' AND type = $1
                    AND (LOWER(title) LIKE $2 OR LOWER(description) LIKE $2)
                """, listing_type, q)
            else:
                rows = await conn.fetch("""
                    SELECT * FROM botstore_listings 
                    WHERE status = 'live'
                    AND (LOWER(title) LIKE $1 OR LOWER(description) LIKE $1)
                """, q)
            return [dict(row) for row in rows]
    except Exception as e:
        print(f"[v0] Error searching listings: {e}")
        return []

async def list_by_type(listing_type: str, category: Optional[str] = None) -> List[Dict]:
    """Get all listings of a type, optionally filtered by category"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            if category:
                rows = await conn.fetch("""
                    SELECT * FROM botstore_listings 
                    WHERE type = $1 AND category = $2 AND status = 'live'
                """, listing_type, category)
            else:
                rows = await conn.fetch("""
                    SELECT * FROM botstore_listings 
                    WHERE type = $1 AND status = 'live'
                """, listing_type)
            return [dict(row) for row in rows]
    except Exception as e:
        print(f"[v0] Error listing by type: {e}")
        return []

async def owner_listings(owner_id: int) -> List[Dict]:
    """Get all listings owned by a user"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM botstore_listings WHERE owner_id = $1",
                owner_id
            )
            return [dict(row) for row in rows]
    except Exception as e:
        print(f"[v0] Error getting owner listings: {e}")
        return []

async def count_active_bots(owner_id: int) -> int:
    """Count active bot listings for free tier check"""
    listings = await owner_listings(owner_id)
    return len([l for l in listings if l.get("type") == "bot" and l.get("status") != "removed"])

async def bot_limit_reached(uid: int, is_premium: bool) -> bool:
    """Check if user hit free listing cap"""
    if is_premium:
        return False
    count = await count_active_bots(uid)
    return count >= ConfigCache.FREE_BOT_LIMIT

async def add_rating(listing_id: str, user_id: int, stars: int) -> bool:
    """Add a rating to a listing"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO botstore_ratings (listing_id, user_id, stars)
                VALUES ($1, $2, $3)
                ON CONFLICT (listing_id, user_id) DO UPDATE SET
                    stars = EXCLUDED.stars
            """, listing_id, user_id, stars)
        return True
    except Exception as e:
        print(f"[v0] Error adding rating: {e}")
        return False

async def get_avg_rating(listing_id: str) -> Optional[float]:
    """Get average rating for a listing"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            avg = await conn.fetchval(
                "SELECT AVG(stars) FROM botstore_ratings WHERE listing_id = $1",
                listing_id
            )
            return round(float(avg), 1) if avg else None
    except Exception as e:
        print(f"[v0] Error getting rating: {e}")
        return None

async def record_click(listing_id: str) -> bool:
    """Record a click on a listing"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE botstore_listings SET clicks = clicks + 1 WHERE id = $1
            """, listing_id)
        return True
    except Exception as e:
        print(f"[v0] Error recording click: {e}")
        return False

async def get_clicks(listing_id: str) -> int:
    """Get click count for a listing"""
    try:
        listing = await get_listing(listing_id)
        return listing.get("clicks", 0) if listing else 0
    except Exception as e:
        print(f"[v0] Error getting clicks: {e}")
        return 0

async def trending(listing_type: str, limit: int = 10) -> List[Dict]:
    """Get trending listings by click count"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM botstore_listings 
                WHERE type = $1 AND status = 'live'
                ORDER BY clicks DESC LIMIT $2
            """, listing_type, limit)
            return [dict(row) for row in rows]
    except Exception as e:
        print(f"[v0] Error getting trending: {e}")
        return []

async def top_rated(listing_type: str, limit: int = 10) -> List[Dict]:
    """Get top rated listings"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT DISTINCT l.* FROM botstore_listings l
                LEFT JOIN botstore_ratings r ON l.id = r.listing_id
                WHERE l.type = $1 AND l.status = 'live'
                GROUP BY l.id
                ORDER BY AVG(r.stars) DESC LIMIT $2
            """, listing_type, limit)
            return [dict(row) for row in rows]
    except Exception as e:
        print(f"[v0] Error getting top rated: {e}")
        return []

async def featured(listing_type: str) -> List[Dict]:
    """Get featured listings"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM botstore_listings 
                WHERE type = $1 AND status = 'featured' AND status = 'live'
            """, listing_type)
            return [dict(row) for row in rows]
    except Exception as e:
        print(f"[v0] Error getting featured: {e}")
        return []

async def set_featured(listing_id: str, days: Optional[int] = None) -> bool:
    """Mark listing as featured"""
    return await update_listing(listing_id, status="featured")

async def report_listing(listing_id: str) -> bool:
    """Mark listing as reported"""
    return await update_listing(listing_id, status="reported")
