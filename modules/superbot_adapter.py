"""
SuperBot Adapter — Premium tiers, referrals, crypto alerts, analytics, leaderboard
Uses PostgreSQL for durable persistence (not JSON files)
Admin configs stored in database
"""

from typing import List, Optional
from database import get_pool
from utils import is_founder


# Admin-configurable settings cached at startup
class ConfigCache:
    TIER_BASIC = {"name": "Basic", "price": 0, "features": ["basic_access"]}
    TIER_PRO = {"name": "Pro", "price": 20, "features": ["pro_access", "alerts", "analytics"]}
    TIER_ELITE = {"name": "Elite", "price": 50, "features": ["elite_access", "priority", "custom_alerts"]}
    
    REFERRAL_REWARD_AI_USES = 1
    REFERRAL_REWARD_COINS = 100
    CRYPTO_ALERT_CHECK_INTERVAL_MINUTES = 5
    LEADERBOARD_UPDATE_INTERVAL_HOURS = 1
    
    @classmethod
    async def load(cls):
        """Load all config from database on startup"""
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                config = await conn.fetchrow(
                    "SELECT * FROM superbot_config LIMIT 1"
                )
                if config:
                    cls.REFERRAL_REWARD_COINS = config.get('referral_reward_coins', 100)
                    cls.REFERRAL_REWARD_AI_USES = config.get('referral_reward_uses', 1)
                    cls.CRYPTO_ALERT_CHECK_INTERVAL_MINUTES = config.get('alert_check_mins', 5)
                    cls.LEADERBOARD_UPDATE_INTERVAL_HOURS = config.get('leaderboard_update_hrs', 1)
        except Exception as e:
            print(f"[v0] Warning loading SuperBot config: {e}")

# ═══════════════════════════════════════════════════════════════════════════
# PREMIUM TIER SYSTEM (DATABASE)
# ═══════════════════════════════════════════════════════════════════════════

async def get_user_tier(uid: int) -> str:
    """Get user's subscription tier from database. Founder (main bot ADMIN_ID,
    legacy Telegram concept) or a Discord clone admin (DISCORD_CLONE_ADMIN_IDS)
    always resolves to elite so no tier-gated feature ever blocks the owner."""
    if is_founder(uid):
        return "elite"
    try:
        from config import DISCORD_CLONE_ADMIN_IDS
        if uid in DISCORD_CLONE_ADMIN_IDS:
            return "elite"
    except Exception:
        pass
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchval(
                "SELECT tier FROM superbot_user_tiers WHERE user_id = $1", uid
            )
        return row or "basic"
    except Exception as e:
        print(f"[v0] Error getting user tier: {e}")
        return "basic"

async def set_user_tier(uid: int, tier: str) -> bool:
    """Upgrade user to tier (basic, pro, elite) in database"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO superbot_user_tiers (user_id, tier, tier_updated)
                VALUES ($1, $2, NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                    tier = EXCLUDED.tier,
                    tier_updated = NOW()
            """, uid, tier)
        return True
    except Exception as e:
        print(f"[v0] Error setting user tier: {e}")
        return False

async def has_feature(uid: int, feature: str) -> bool:
    """Check if user's tier has a feature"""
    try:
        tier = await get_user_tier(uid)
        tier_config = {
            "basic": ConfigCache.TIER_BASIC,
            "pro": ConfigCache.TIER_PRO,
            "elite": ConfigCache.TIER_ELITE
        }.get(tier, ConfigCache.TIER_BASIC)
        return feature in tier_config.get("features", [])
    except Exception as e:
        print(f"[v0] Error checking feature: {e}")
        return False

# ═══════════════════════════════════════════════════════════════════════════
# REFERRAL SYSTEM (DATABASE)
# ═══════════════════════════════════════════════════════════════════════════

async def add_referral(referrer_uid: int, new_uid: int) -> bool:
    """Record a referral and reward the referrer"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            # Check if already referred
            exists = await conn.fetchval("""
                SELECT 1 FROM superbot_referrals 
                WHERE referrer_id = $1 AND referred_id = $2
            """, referrer_uid, new_uid)
            
            if not exists:
                await conn.execute("""
                    INSERT INTO superbot_referrals (referrer_id, referred_id, reward_given)
                    VALUES ($1, $2, $3)
                """, referrer_uid, new_uid, ConfigCache.REFERRAL_REWARD_COINS)
                
                # Add bonus points
                await add_points(referrer_uid, "referral_bonus", ConfigCache.REFERRAL_REWARD_COINS)
                return True
        return False
    except Exception as e:
        print(f"[v0] Error adding referral: {e}")
        return False

async def get_referral_count(uid: int) -> int:
    """Get referral count for user"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM superbot_referrals WHERE referrer_id = $1", uid
            )
        return count or 0
    except Exception as e:
        print(f"[v0] Error getting referral count: {e}")
        return 0

async def get_referral_reward(uid: int) -> int:
    """Get total reward coins from referrals"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            total = await conn.fetchval(
                "SELECT SUM(reward_given) FROM superbot_referrals WHERE referrer_id = $1", uid
            )
        return total or 0
    except Exception as e:
        print(f"[v0] Error getting referral reward: {e}")
        return 0

# ═══════════════════════════════════════════════════════════════════════════
# CRYPTO PRICE ALERTS (DATABASE)
# ═══════════════════════════════════════════════════════════════════════════

async def set_alert(uid: int, coin: str, target_usd: float, direction: str) -> bool:
    """Set price alert (direction: 'above' or 'below')"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO superbot_crypto_alerts 
                (user_id, coin, price_threshold, alert_type, active)
                VALUES ($1, $2, $3, $4, TRUE)
            """, uid, coin, target_usd, direction)
        return True
    except Exception as e:
        print(f"[v0] Error setting alert: {e}")
        return False

async def get_user_alerts(uid: int) -> List[dict]:
    """Get all alerts for a user"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM superbot_crypto_alerts WHERE user_id = $1 AND active = TRUE", uid
            )
        return [dict(row) for row in rows] if rows else []
    except Exception as e:
        print(f"[v0] Error getting user alerts: {e}")
        return []

async def clear_alerts(uid: int) -> bool:
    """Clear all alerts for user"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE superbot_crypto_alerts SET active = FALSE WHERE user_id = $1", uid
            )
        return True
    except Exception as e:
        print(f"[v0] Error clearing alerts: {e}")
        return False

async def remove_alert(uid: int, coin: str) -> bool:
    """Remove specific alert for coin"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE superbot_crypto_alerts SET active = FALSE 
                WHERE user_id = $1 AND coin = $2
            """, uid, coin)
        return True
    except Exception as e:
        print(f"[v0] Error removing alert: {e}")
        return False

# ═══════════════════════════════════════════════════════════════════════════
# LEADERBOARD & POINTS SYSTEM (DATABASE)
# ═══════════════════════════════════════════════════════════════════════════

async def add_points(uid: int, action: str, pts: int) -> bool:
    """Add points to user's score"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO superbot_user_points (user_id, points, action, recorded_at)
                VALUES ($1, $2, $3, NOW())
            """, uid, pts, action)
        return True
    except Exception as e:
        print(f"[v0] Error adding points: {e}")
        return False

async def get_top_users(n: int = 10) -> List[dict]:
    """Get top N users by points"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT user_id, SUM(points) as total_points, COUNT(*) as action_count
                FROM superbot_user_points
                GROUP BY user_id
                ORDER BY total_points DESC
                LIMIT $1
            """, n)
        return [{"uid": row["user_id"], "points": row["total_points"]} for row in rows] if rows else []
    except Exception as e:
        print(f"[v0] Error getting top users: {e}")
        return []

async def get_user_rank(uid: int) -> Optional[int]:
    """Get user's leaderboard rank"""
    try:
        top = await get_top_users(1000)
        for i, user in enumerate(top, 1):
            if user["uid"] == uid:
                return i
        return None
    except Exception as e:
        print(f"[v0] Error getting user rank: {e}")
        return None

async def get_user_points(uid: int) -> int:
    """Get user's current points"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            total = await conn.fetchval(
                "SELECT SUM(points) FROM superbot_user_points WHERE user_id = $1", uid
            )
        return total or 0
    except Exception as e:
        print(f"[v0] Error getting user points: {e}")
        return 0

# ═══════════════════════════════════════════════════════════════════════════
# ANALYTICS TRACKING (DATABASE)
# ═══════════════════════════════════════════════════════════════════════════

async def track_analytics(uid: int, action: str, amount: int = 0) -> bool:
    """Track user action for analytics: ai_use, download, purchase, or referral.
    Uses a dedicated user_analytics table (upsert) — previously this wrote
    into superbot_user_points, the points/leaderboard table, which would have
    corrupted point totals with arbitrary "action" rows. Not yet called from
    any AI/download/purchase call site in this repo (none currently track
    per-action analytics) — this fixes the storage so it's safe to wire up
    wherever those features start needing it."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO user_analytics (user_id, ai_uses, downloads, purchases, referrals,
                                             total_spent, total_earned, last_action)
                VALUES ($1, 0, 0, 0, 0, 0, 0, CURRENT_DATE)
                ON CONFLICT (user_id) DO NOTHING
            """, uid)

            if action == "ai_use":
                await conn.execute(
                    "UPDATE user_analytics SET ai_uses = ai_uses + 1, last_action = CURRENT_DATE WHERE user_id = $1", uid
                )
            elif action == "download":
                await conn.execute(
                    "UPDATE user_analytics SET downloads = downloads + 1, last_action = CURRENT_DATE WHERE user_id = $1", uid
                )
            elif action == "purchase":
                await conn.execute(
                    "UPDATE user_analytics SET purchases = purchases + 1, total_spent = total_spent + $2, "
                    "last_action = CURRENT_DATE WHERE user_id = $1", uid, amount
                )
            elif action == "referral":
                await conn.execute(
                    "UPDATE user_analytics SET referrals = referrals + 1, total_earned = total_earned + $2, "
                    "last_action = CURRENT_DATE WHERE user_id = $1", uid, amount
                )
        return True
    except Exception as e:
        print(f"[v0] Error tracking analytics: {e}")
        return False

async def get_user_action_analytics(uid: int) -> dict:
    """Per-action analytics (ai_uses/downloads/purchases/referrals) — distinct
    from get_user_stats() below, which covers tier/points/rank instead."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM user_analytics WHERE user_id = $1", uid)
        if row:
            return dict(row)
        return {"ai_uses": 0, "downloads": 0, "purchases": 0, "referrals": 0,
                "total_spent": 0, "total_earned": 0, "last_action": None}
    except Exception as e:
        print(f"[v0] Error getting user action analytics: {e}")
        return {"ai_uses": 0, "downloads": 0, "purchases": 0, "referrals": 0,
                "total_spent": 0, "total_earned": 0, "last_action": None}

async def get_user_stats(uid: int) -> dict:
    """Get user's analytics stats"""
    try:
        tier = await get_user_tier(uid)
        rank = await get_user_rank(uid)
        points = await get_user_points(uid)
        refs = await get_referral_count(uid)
        
        return {
            "tier": tier,
            "points": points,
            "rank": rank,
            "referrals": refs,
            "total_interactions": points  # Simplified: points = interactions
        }
    except Exception as e:
        print(f"[v0] Error getting user stats: {e}")
        return {"tier": "basic", "points": 0, "rank": None, "referrals": 0}

async def get_global_stats() -> dict:
    """Get bot-wide analytics"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            # Total users with tier
            total_users = await conn.fetchval(
                "SELECT COUNT(DISTINCT user_id) FROM superbot_user_tiers"
            )
            
            # Total interactions (points)
            total_interactions = await conn.fetchval(
                "SELECT SUM(points) FROM superbot_user_points"
            )
            
            # Top 5 users
            top_users = await get_top_users(5)
            
            # Tier distribution
            tier_dist = await conn.fetch("""
                SELECT tier, COUNT(*) as count FROM superbot_user_tiers
                GROUP BY tier
            """)
            
            tier_distribution = {
                "basic": next((r["count"] for r in tier_dist if r["tier"] == "basic"), 0),
                "pro": next((r["count"] for r in tier_dist if r["tier"] == "pro"), 0),
                "elite": next((r["count"] for r in tier_dist if r["tier"] == "elite"), 0),
            }
        
        return {
            "total_users": total_users or 0,
            "total_interactions": total_interactions or 0,
            "top_users": top_users,
            "tier_distribution": tier_distribution
        }
    except Exception as e:
        print(f"[v0] Error getting global stats: {e}")
        return {"total_users": 0, "total_interactions": 0, "top_users": [], "tier_distribution": {}}

# ═══════════════════════════════════════════════════════════════════════════
# ADMIN CONFIG HELPERS
# ═══════════════════════════════════════════════════════════════════════════

async def get_admin_config() -> dict:
    """Get all admin-configurable settings"""
    try:
        return {
            "tier_pro_price": ConfigCache.TIER_PRO["price"],
            "tier_elite_price": ConfigCache.TIER_ELITE["price"],
            "referral_reward": ConfigCache.REFERRAL_REWARD_COINS,
            "crypto_alert_check_interval": ConfigCache.CRYPTO_ALERT_CHECK_INTERVAL_MINUTES,
            "leaderboard_update_interval": ConfigCache.LEADERBOARD_UPDATE_INTERVAL_HOURS,
        }
    except Exception as e:
        print(f"[v0] Error getting admin config: {e}")
        return {}

async def update_admin_config(config: dict) -> bool:
    """Admin: Update time-based and price configs"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO superbot_config (
                    referral_reward_coins, referral_reward_uses,
                    alert_check_mins, leaderboard_update_hrs
                ) VALUES ($1, $2, $3, $4)
                ON CONFLICT DO UPDATE SET
                    referral_reward_coins = EXCLUDED.referral_reward_coins,
                    referral_reward_uses = EXCLUDED.referral_reward_uses,
                    alert_check_mins = EXCLUDED.alert_check_mins,
                    leaderboard_update_hrs = EXCLUDED.leaderboard_update_hrs
            """,
                config.get("referral_reward", ConfigCache.REFERRAL_REWARD_COINS),
                config.get("referral_reward_uses", ConfigCache.REFERRAL_REWARD_AI_USES),
                config.get("crypto_alert_check_interval", ConfigCache.CRYPTO_ALERT_CHECK_INTERVAL_MINUTES),
                config.get("leaderboard_update_interval", ConfigCache.LEADERBOARD_UPDATE_INTERVAL_HOURS)
            )
        
        # Update cache
        if "referral_reward" in config:
            ConfigCache.REFERRAL_REWARD_COINS = config["referral_reward"]
        if "referral_reward_uses" in config:
            ConfigCache.REFERRAL_REWARD_AI_USES = config["referral_reward_uses"]
        if "crypto_alert_check_interval" in config:
            ConfigCache.CRYPTO_ALERT_CHECK_INTERVAL_MINUTES = config["crypto_alert_check_interval"]
        if "leaderboard_update_interval" in config:
            ConfigCache.LEADERBOARD_UPDATE_INTERVAL_HOURS = config["leaderboard_update_interval"]
        
        return True
    except Exception as e:
        print(f"[v0] Error updating admin config: {e}")
        return False
