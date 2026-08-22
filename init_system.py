"""System initialization - runs on first startup"""
import os
import json
import asyncio
from pathlib import Path
from datetime import datetime
from database import get_pool


# Define all data directories
DATA_DIRS = {
    "botstore": ["data/botstore"],
    "superbot": ["data/superbot"],
}

# Default configurations
DEFAULT_BOTSTORE_CONFIG = {
    "listing_categories": ["Bots", "Groups", "Channels", "Tools", "Games", "News", "Utilities", "Entertainment", "Education", "Community", "Business"],
    "listing_price_featured_ghs": 50,
    "listing_duration_days": 30,
    "max_listings_per_user": 5,
    "max_featured_listings": 10,
    "click_tracking_enabled": True,
    "rating_system_enabled": True,
    "last_updated": datetime.now().isoformat()
}

DEFAULT_SUPERBOT_CONFIG = {
    "tiers": {
        "basic": {"name": "Basic", "price": 0, "features": ["leaderboard", "stats"]},
        "pro": {"name": "Pro", "price": 100, "features": ["leaderboard", "stats", "alerts", "referrals"]},
        "elite": {"name": "Elite", "price": 300, "features": ["leaderboard", "stats", "alerts", "referrals", "analytics"]}
    },
    "referral_reward_coins": 100,
    "alert_check_interval_hours": 1,
    "max_alerts_per_user_pro": 5,
    "max_alerts_per_user_elite": 20,
    "leaderboard_update_interval_hours": 6,
    "featured_coins_multiplier": 1.5,
    "last_updated": datetime.now().isoformat()
}

# Default data structures
DEFAULT_BOTSTORE_DATA = {
    "listings": [],
    "ratings": [],
    "featured_listings": [],
    "config": DEFAULT_BOTSTORE_CONFIG
}

DEFAULT_SUPERBOT_DATA = {
    "user_tiers": {},
    "referrals": [],
    "crypto_alerts": [],
    "user_points": {},
    "leaderboard_cache": [],
    "config": DEFAULT_SUPERBOT_CONFIG
}

DEFAULT_ADMIN_CONFIG = {
    "botstore": DEFAULT_BOTSTORE_CONFIG,
    "superbot": DEFAULT_SUPERBOT_CONFIG,
    "last_initialized": datetime.now().isoformat()
}


def create_directories():
    """Create all required data directories"""
    for module, dirs in DATA_DIRS.items():
        for dir_path in dirs:
            Path(dir_path).mkdir(parents=True, exist_ok=True)
            print(f"[v0] ✓ Directory ready: {dir_path}")


def initialize_json_files():
    """Initialize JSON data files with defaults"""
    files_config = {
        "data/botstore/listings.json": DEFAULT_BOTSTORE_DATA,
        "data/superbot/users.json": DEFAULT_SUPERBOT_DATA,
        "data/admin_config.json": DEFAULT_ADMIN_CONFIG
    }
    
    for file_path, default_data in files_config.items():
        if not os.path.exists(file_path):
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, "w") as f:
                json.dump(default_data, f, indent=2)
            print(f"[v0] ✓ Initialized: {file_path}")
        else:
            print(f"[v0] ✓ Already exists: {file_path}")


async def initialize_database_tables():
    """Create database tables for asyncpg (when migrating from JSON)"""
    try:
        pool = await get_pool()
        
        async with pool.acquire() as conn:
            # BotStore tables
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS botstore_listings (
                    id SERIAL PRIMARY KEY,
                    owner_id BIGINT NOT NULL,
                    category TEXT NOT NULL,
                    title VARCHAR(255) NOT NULL,
                    description TEXT,
                    link TEXT,
                    status VARCHAR(20) DEFAULT 'pending',
                    rating DECIMAL(3,2) DEFAULT 0,
                    clicks INTEGER DEFAULT 0,
                    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            print("[v0] ✓ Table ready: botstore_listings")
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS botstore_ratings (
                    id SERIAL PRIMARY KEY,
                    listing_id INTEGER NOT NULL REFERENCES botstore_listings(id),
                    user_id BIGINT NOT NULL,
                    stars INTEGER NOT NULL,
                    review TEXT,
                    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            print("[v0] ✓ Table ready: botstore_ratings")
            
            # SuperBot tables
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS superbot_user_tiers (
                    user_id BIGINT PRIMARY KEY,
                    tier_level VARCHAR(20) DEFAULT 'basic',
                    joined_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    tier_expiry TIMESTAMP
                )
            """)
            print("[v0] ✓ Table ready: superbot_user_tiers")
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS superbot_referrals (
                    id SERIAL PRIMARY KEY,
                    referrer_id BIGINT NOT NULL,
                    referee_id BIGINT NOT NULL,
                    status VARCHAR(20) DEFAULT 'pending',
                    reward_given BOOLEAN DEFAULT FALSE,
                    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            print("[v0] ✓ Table ready: superbot_referrals")
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS superbot_crypto_alerts (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    coin VARCHAR(20) NOT NULL,
                    price_threshold DECIMAL(15,8) NOT NULL,
                    alert_type VARCHAR(10),
                    active BOOLEAN DEFAULT TRUE,
                    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            print("[v0] ✓ Table ready: superbot_crypto_alerts")
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS superbot_user_points (
                    user_id BIGINT PRIMARY KEY,
                    total_points INTEGER DEFAULT 0,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            print("[v0] ✓ Table ready: superbot_user_points")
            
    except Exception as e:
        print(f"[v0] Note: Database tables status: {e}")


async def initialize_system():
    """Run all initialization steps"""
    print("[v0] System initialization starting...")
    
    create_directories()
    initialize_json_files()
    
    # Only try database initialization if asyncpg is available
    try:
        await initialize_database_tables()
    except Exception as e:
        print(f"[v0] Database tables skipped (JSON mode): {str(e)[:50]}")
    
    print("[v0] ✓ System initialization complete!")


# Run on import if this is main
if __name__ == "__main__":
    asyncio.run(initialize_system())
