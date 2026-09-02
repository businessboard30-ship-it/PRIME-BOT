# FULL PATH: PRIME-BOT-main/database.py

# path: database.py

# path: database.py

import asyncio
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any, Tuple

import asyncpg

from config import DATABASE_URL
from utils.crypto import secret_manager

logger = logging.getLogger(__name__)

# Starter library for discord_autopost_content (see _create_tables) — one-time
# seed only, bot owner can add/remove more via /autopostcontent. Keep each
# body short (fits comfortably in a Discord embed description) and each
# example_command a real, copy-pasteable slash command.
DEFAULT_AUTOPOST_CONTENT = [
    ("moderation", "Keep your server clean",
     "Auto-moderation can filter bad words, spam invites, and mass mentions "
     "automatically — no manual policing needed.",
     "/automod bannedword add"),
    ("moderation", "Warn system built in",
     "Warn members who break the rules and let the bot auto-timeout repeat "
     "offenders after 3 warns.",
     "/warn"),
    ("economy", "Server economy & shop",
     "Members can earn currency with /daily, /work, and /beg, then spend it "
     "in a shop you configure — including role rewards.",
     "/ecoconfig currency"),
    ("leveling", "XP & level roles",
     "Members earn XP from chatting and can be auto-granted roles as they "
     "level up — a lightweight way to reward activity.",
     "/levelrole add"),
    ("ai_tools", "AI chat, right in Discord",
     "Ask questions, get recommendations, or just chat — powered by AI, "
     "available in this server or by DM.",
     "/aichat"),
    ("ai_store", "Browse the AI Store",
     "Discover and chat with community-made AI assistant personas, or list "
     "your own.",
     "/aistore browse"),
    ("welcome", "Custom welcome cards",
     "New members get a personalized welcome card automatically — configure "
     "the look and message to match your server.",
     "/welcome setup"),
    ("reaction_roles", "Self-assignable roles",
     "Let members pick their own roles from a reaction panel — no manual "
     "role assignment required.",
     "/reactionrole create"),
    ("utility", "Quick utility commands",
     "Currency conversion, stock and crypto prices, and news headlines — "
     "all one command away, in-server or by DM.",
     "/convert"),
    ("premium", "Unlock a premium tier",
     "Set up a paid role members can unlock in this server — pricing and "
     "perks are entirely up to you.",
     "/createpremium"),
    ("automod", "Auto-moderation filters",
     "Block invite links and mass-mention spam automatically, on top of the "
     "banned word filter — set it once and forget it.",
     "/automod"),
    ("crypto_alerts", "Crypto price alerts",
     "Get pinged the moment a coin crosses a price you set — no need to "
     "keep checking charts yourself.",
     "/alert"),
    ("image_search", "Reverse image search",
     "Not sure where a picture came from? Find its likely source in "
     "seconds.",
     "/imagesearch"),
    ("discover", "Discover anime by category",
     "Browse or search anime straight from Discord, and save your favorite "
     "categories for quick access later.",
     "/discover"),
    ("media_connect", "Connect your media library",
     "Link a Plex, Jellyfin, or cloud folder you own and search/play from "
     "it right in Discord.",
     "/connect"),
    ("ads_marketplace", "Ads & marketplace",
     "Buy or sell services with other members, or run a sponsored ad — all "
     "owner-approved before it goes live.",
     "/marketplace"),
    ("referrals", "Referral program",
     "Invite others through your referral link and get tracked credit for "
     "every signup.",
     "/referral"),
    ("language", "Pick your reply language",
     "Choose the language I reply to you in — your setting, your choice, "
     "any time.",
     "/language"),
    ("clone_admin", "Run your own bot clone",
     "Register your own Discord bot token and this bot's features run "
     "under your own branding.",
     "/myclones"),
    ("feedback", "Send feedback anytime",
     "Spotted a bug or have an idea? Send it straight to the bot owner "
     "from any server or DM.",
     "/feedback"),
    ("voice_xp", "Voice-channel XP",
     "Members now earn XP just for hanging out in voice, on top of chat XP "
     "— it all counts toward the same /rank and /leaderboard.",
     "/voicexp settings"),
    ("suggestions", "Suggestion box",
     "Let members pitch ideas and vote on them — staff approve or deny with "
     "a reaction and the embed updates live.",
     "/suggest"),
    ("ticket", "Need support? Open a ticket",
     "A private channel between you and staff, one click away — claim, "
     "close, and get a full transcript when it's done.",
     "/ticket setup"),
    ("starboard", "Starboard for the best messages",
     "Messages that earn enough ⭐ reactions get reposted to a highlights "
     "channel automatically — set your own threshold.",
     "/starboard setup"),
    ("giveaways", "Run a giveaway in seconds",
     "Pick a prize, a duration, and a winner count — entries are one button "
     "click and winners are picked automatically.",
     "/giveaway start"),
    ("schedule", "Schedule your own announcements",
     "Post a message once, on a repeating interval, or daily at a set time "
     "— no need to be online when it goes out.",
     "/schedule once"),
]

_pool = None
_pool_loop = None  # the asyncio event loop _pool's connections belong to


async def get_pool():
    """
    Get or create the asyncpg connection pool for serverless (Issue 1.4).
    
    On serverless deployments (Vercel), each cold start and concurrent instance
    creates its own pool. If not using PgBouncer, this leads to connection exhaustion.
    
    Fix: Use PgBouncer if available (Supabase built-in, Neon, etc.) and reduce
    pool size to 1 connection per instance (sufficient for single async request flow).

    Loop safety: the api/*.py serverless-style handlers (see api_server.py)
    each call `asyncio.run(...)` per request, which spins up a brand-new
    event loop and destroys it afterward. asyncpg pools are bound to the
    loop they were created on — reusing one from a now-closed loop raises
    "Event loop is closed" / "another operation is in progress". The
    persistent bot process (discord_bot/bot.py) only ever has one loop for
    its whole lifetime, so this check is a no-op there; it only matters for
    the per-request callers.
    """
    global _pool, _pool_loop
    current_loop = asyncio.get_running_loop()
    if _pool is not None and _pool_loop is not current_loop:
        # Stale pool from a previous (now-closed) event loop — its
        # connections are unusable on this loop. Don't try to close it
        # (that would itself need the dead loop); just drop the reference
        # and create a fresh pool on the current loop.
        _pool = None
    if _pool is None:
        # Auto-detect PgBouncer endpoint (Supabase pooler: port 6543, transaction mode)
        # If using pooler, reduce pool size since PgBouncer handles connection multiplexing
        url = DATABASE_URL
        is_using_pooler = "pooler" in url or ":6543" in url
        
        # For serverless: 1 connection per instance is sufficient
        # (a single serverless invocation doesn't run concurrent queries)
        # If using a pooler, it's even safer (pooler handles connection fan-in)
        min_pool_size = 1
        # Was max_pool_size=2 for the non-pooler case. That's dangerously
        # low for the persistent bot process specifically: this single
        # pool is shared by ~25+ background `@tasks.loop` pollers
        # (automod reminders, starboard, crypto_alerts, schedule, giveaway
        # timers, voice_xp, bump, heist, ...) AND every concurrent slash
        # command/component interaction across every guild the bot is in.
        # With only 2 connections, any brief overlap (e.g. a background
        # loop tick landing mid-interaction) makes pool.acquire() block
        # for other callers. Most buttons survive this because they
        # defer() first (which buys ~15 minutes), but a few — e.g.
        # _WelcomeEditButton in _views_join_dm.py — MUST call send_modal()
        # as their literal first response and can't defer around a DB
        # wait, so this contention surfaced there as a hard, repeatable
        # "The application didn't respond in time." Bumped to match the
        # pooler case; a real (non-serverless) Postgres provider handles
        # 5 connections from one process without issue.
        max_pool_size = 5 if is_using_pooler else 5
        # NOTE: 5 (not 1) when a pooler is present — the persistent bot
        # process (discord_bot/bot.py) fields many concurrent interactions
        # (slash commands, vote clicks, autopost/crypto_alerts loops) on one
        # shared pool. Capping at 1 here serializes ALL of them behind
        # whichever query happens to be running, causing unrelated commands
        # to time out. A transaction-mode pooler (PgBouncer/Supabase) is
        # built to safely multiplex a handful of client-side connections
        # like this — 1 only makes sense for the true serverless api/*.py
        # handlers, which don't go through this persistent-process path.
        
        logger.info(
            "[v0] Creating connection pool: min=%s, max=%s, pooler=%s",
            min_pool_size, max_pool_size,
            "yes" if is_using_pooler else "no (consider using PgBouncer)"
        )
        
        _pool = await asyncpg.create_pool(
            url,
            min_size=min_pool_size,
            max_size=max_pool_size,
            statement_cache_size=0,  # Required for PgBouncer compatibility
            command_timeout=10
        )
        _pool_loop = current_loop
    return _pool


class Database:
    """Database handler backed by Postgres (Supabase / Neon / Railway Postgres)."""

    def __init__(self):
        # asyncpg wants postgres:// or postgresql://, both work
        self.dsn = DATABASE_URL

    async def init(self):
        """Create tables if they don't exist yet (safe to run on every cold start)."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await self._create_tables(conn)
            # NOTE: _migrate_stale_stripe_provider is no longer called here.
            # Stripe is now a fully supported clone payment provider (see
            # payments.StripePayment / payments.gateway_charge_amount and
            # discord_bot/cogs/clone_admin.py's /clonemonetize setpayment) —
            # running this on every cold start would silently revert any
            # clone owner's live Stripe connection back to 'main'. The
            # method is kept below only for reference/manual use.

    async def _migrate_stale_stripe_provider(self, conn):
        """Historical one-off cleanup from when Stripe was disabled — kept
        for reference only, NOT called automatically anymore (see the note
        in init() above). Do not wire this back into init() while Stripe
        is a supported provider, or it will undo clone owners' Stripe
        connections on every deploy.

        Original docstring: any clone that had already connected a Stripe
        key still has payment_provider='stripe' sitting in its custom_data.
        Payments for those clones were already silently falling back to
        the main bot's account (database.get_clone_payment_config only
        trusts a provider+key pair it recognizes), so this just makes that
        explicit in storage — resets provider to 'main' and drops the
        now-orphaned encrypted key. No-op once every row has been migrated
        once."""
        rows = await conn.fetch(
            "SELECT clone_id, custom_data FROM cloned_bots WHERE custom_data LIKE '%\"stripe\"%'"
        )
        for row in rows:
            try:
                cd = json.loads(row["custom_data"]) if row["custom_data"] else {}
            except (json.JSONDecodeError, TypeError):
                continue
            if cd.get("payment_provider") == "stripe":
                cd["payment_provider"] = "main"
                cd.pop("payment_key_encrypted", None)
                await conn.execute(
                    "UPDATE cloned_bots SET custom_data = $1 WHERE clone_id = $2",
                    json.dumps(cd), row["clone_id"]
                )
                logger.info(f"[v0] Migrated clone_id={row['clone_id']} off removed Stripe provider back to 'main'")

    # ─────────────────────────────────────────────────────────────────────
    # Generic query passthroughs. Several handlers (feature_handlers.py,
    # moderation.py) run one-off queries against tables that don't warrant
    # a dedicated method — these mirror asyncpg's Connection API so `db.`
    # can be used as a drop-in pool everywhere else already assumes it is.
    # ─────────────────────────────────────────────────────────────────────
    async def execute(self, query: str, *args):
        pool = await get_pool()
        async with pool.acquire() as conn:
            return await conn.execute(query, *args)

    async def fetchrow(self, query: str, *args):
        pool = await get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def fetch(self, query: str, *args):
        pool = await get_pool()
        async with pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def fetchval(self, query: str, *args):
        pool = await get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(query, *args)

    # --- Custom categories (table existed in schema but had zero CRUD around it) ---

    async def create_category(self, owner_id: int, category_name: str, emoji: str = "📁") -> int:
        """Create a new custom category for a user"""
        row = await self.fetchrow("""
            INSERT INTO categories (owner_id, category_name, emoji, anime_ids)
            VALUES ($1, $2, $3, '')
            RETURNING category_id
        """, owner_id, category_name, emoji)
        return row["category_id"]

    async def get_user_categories(self, owner_id: int) -> List[Dict]:
        """Get all categories a user has created"""
        rows = await self.fetch("""
            SELECT * FROM categories WHERE owner_id = $1 ORDER BY created_date DESC
        """, owner_id)
        return [{
            "category_id": row["category_id"],
            "category_name": row["category_name"],
            "emoji": row["emoji"],
            "anime_ids": [int(a) for a in row["anime_ids"].split(",") if a] if row["anime_ids"] else []
        } for row in rows]

    async def get_category(self, category_id: int) -> Optional[Dict]:
        """Get a single category by id"""
        row = await self.fetchrow("SELECT * FROM categories WHERE category_id = $1", category_id)
        if not row:
            return None
        return {
            "category_id": row["category_id"],
            "owner_id": row["owner_id"],
            "category_name": row["category_name"],
            "emoji": row["emoji"],
            "anime_ids": [int(a) for a in row["anime_ids"].split(",") if a] if row["anime_ids"] else []
        }

    async def add_anime_to_category(self, category_id: int, anime_id: int):
        """Add an anime id to a category's list (comma-separated string column)"""
        row = await self.fetchrow("SELECT anime_ids FROM categories WHERE category_id = $1", category_id)
        existing = [a for a in (row["anime_ids"] or "").split(",") if a]
        if str(anime_id) not in existing:
            existing.append(str(anime_id))
        await self.execute(
            "UPDATE categories SET anime_ids = $1 WHERE category_id = $2",
            ",".join(existing), category_id
        )

    async def _create_tables(self, conn):
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                joined_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                tier TEXT DEFAULT 'free',
                submissions_count INTEGER DEFAULT 0,
                is_admin BOOLEAN DEFAULT FALSE,
                subscription_status TEXT DEFAULT 'inactive',
                subscription_expiry TIMESTAMP,
                stripe_key TEXT
            )
        """)

        # Per-clone tier/quota/subscription status. `users` above stays a
        # single global identity row per Telegram user_id (needed because
        # `submissions.user_id` has a foreign key into it) — but tier,
        # premium status, free-use counters, and subscriptions must NOT be
        # global, since the same Telegram user_id talks to the main bot and
        # every clone. clone_id = 0 is the main bot; each clone gets its own
        # row per user, so a premium purchase on one bot never grants
        # premium on another.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_clone_status (
                user_id BIGINT NOT NULL REFERENCES users(user_id),
                clone_id BIGINT NOT NULL DEFAULT 0,
                tier TEXT DEFAULT 'free',
                tos_accepted BOOLEAN DEFAULT FALSE,
                subscription_status TEXT DEFAULT 'inactive',
                subscription_expiry TIMESTAMP,
                free_ai_chat_uses INTEGER DEFAULT 0,
                free_download_uses INTEGER DEFAULT 0,
                utility_sub_status TEXT DEFAULT 'inactive',
                utility_sub_expiry TIMESTAMP,
                free_image_search_used BOOLEAN DEFAULT FALSE,
                language TEXT DEFAULT 'en',
                PRIMARY KEY (user_id, clone_id)
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS submissions (
                submission_id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(user_id),
                anime_name TEXT NOT NULL,
                episodes INTEGER,
                genres TEXT,
                synopsis TEXT,
                image_url TEXT,
                status TEXT DEFAULT 'pending',
                created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                approved_date TIMESTAMP,
                rejection_reason TEXT
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS cloned_bots (
                clone_id SERIAL PRIMARY KEY,
                owner_id BIGINT NOT NULL REFERENCES users(user_id),
                bot_name TEXT NOT NULL,
                bot_token TEXT UNIQUE NOT NULL,
                webhook_url TEXT,
                custom_data TEXT,
                status TEXT DEFAULT 'active',
                payment_id TEXT,
                payment_status TEXT,
                created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # --- Migration: real clone-bot columns (Part 3.2 Step C) -------------
        # cloned_bots already existed before this change (with a fake bot_token
        # and a `status` column). These ALTERs are additive and safe to run on
        # every cold start against a table that may or may not have them yet.
        await conn.execute("""
            ALTER TABLE cloned_bots ADD COLUMN IF NOT EXISTS webhook_secret TEXT
        """)
        await conn.execute("""
            ALTER TABLE cloned_bots ADD COLUMN IF NOT EXISTS bot_username TEXT
        """)
        # --- Migration: referral tracking for the "Get your own bot" growth-loop
        # button (keyboards.py main_menu). Counts how many new signups a given
        # clone has sent to the main bot's /start=fromclone_<id> deep link.
        await conn.execute("""
            ALTER TABLE cloned_bots ADD COLUMN IF NOT EXISTS referral_count INTEGER NOT NULL DEFAULT 0
        """)
        # --- Migration: BotStore ToS acceptance flag ---------------------------
        await conn.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS tos_accepted BOOLEAN DEFAULT FALSE
        """)

        # --- Migration: shared AI Chat / Download paywall -----------------------
        # 2 free uses each (tracked separately), then ONE subscription (25 GHS /
        # 2 months) unlocks both features together. See handlers/utility_paywall.py.
        await conn.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS free_ai_chat_uses INTEGER DEFAULT 0
        """)
        await conn.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS free_download_uses INTEGER DEFAULT 0
        """)
        await conn.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS utility_sub_status TEXT DEFAULT 'inactive'
        """)
        await conn.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS utility_sub_expiry TIMESTAMP
        """)

        # --- Migration: Reverse Image Search paywall -----------------------------
        # 1 free source-link reveal per user, then GHS 10 via Paystack. See
        # handlers/image_search_handler.py.
        await conn.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS free_image_search_used BOOLEAN DEFAULT FALSE
        """)

        # --- Migration: user language preference (i18n) -------------------------
        # Two-letter code, must match a key in i18n.SUPPORTED_LANGUAGES. Static UI
        # strings are looked up via i18n.t(key, lang); AI-generated responses are
        # steered via i18n.language_instruction(lang). See i18n.py.
        await conn.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS language TEXT DEFAULT 'en'
        """)

        # --- Migration: ad/marketplace referral tracking (Phase 4). Distinct
        # from cloned_bots.referral_count above (that's the clone growth-loop
        # counter) — this is "who referred this user into submitting an ad or
        # marketplace listing", set once via /referral use and never changed.
        await conn.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS ad_referred_by BIGINT REFERENCES users(user_id)
        """)

        # --- Migration: backfill user_clone_status from the old shared
        # columns on `users`, into clone_id = 0 (the main bot) — so
        # existing tiers/subscriptions/quotas aren't lost when this
        # isolation fix ships. The `users` columns themselves stay in
        # place for now (harmless legacy) but are no longer read/written
        # by the app; user_clone_status is the source of truth going
        # forward.
        await conn.execute("""
            INSERT INTO user_clone_status (
                user_id, clone_id, tier, tos_accepted, subscription_status, subscription_expiry,
                free_ai_chat_uses, free_download_uses, utility_sub_status, utility_sub_expiry,
                free_image_search_used, language
            )
            SELECT user_id, 0, COALESCE(tier, 'free'), COALESCE(tos_accepted, FALSE),
                   COALESCE(subscription_status, 'inactive'), subscription_expiry,
                   COALESCE(free_ai_chat_uses, 0), COALESCE(free_download_uses, 0),
                   COALESCE(utility_sub_status, 'inactive'), utility_sub_expiry,
                   COALESCE(free_image_search_used, FALSE), COALESCE(language, 'en')
            FROM users
            ON CONFLICT (user_id, clone_id) DO NOTHING
        """)

        # --- Migration: BotFather-style Bot Manager -----------------------------
        # Lets a user register bots they already own (by token) so they can edit
        # name/description/commands via the Bot API without leaving this bot.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS managed_bots (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                token TEXT NOT NULL,
                username TEXT,
                name TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, token)
            )
        """)
        # `status` already exists in this inline schema (unlike what the original
        # audit found in sql/schema.sql's drifted version) — nothing to add there,
        # just documenting that the reconciliation was checked, not skipped.
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_cloned_bots_bot_token ON cloned_bots(bot_token)
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS anime_entries (
                anime_id SERIAL PRIMARY KEY,
                anilist_id INTEGER UNIQUE,
                mal_id INTEGER UNIQUE,
                title TEXT NOT NULL,
                episodes INTEGER,
                genres TEXT,
                rating REAL,
                status TEXT,
                synopsis TEXT,
                image_url TEXT,
                source_api TEXT,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                category_id SERIAL PRIMARY KEY,
                owner_id BIGINT NOT NULL REFERENCES users(user_id),
                category_name TEXT NOT NULL,
                emoji TEXT,
                anime_ids TEXT,
                created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS payment_logs (
                payment_id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(user_id),
                amount REAL NOT NULL,
                status TEXT DEFAULT 'pending',
                paystack_reference TEXT UNIQUE,
                payment_type TEXT,
                chat_id BIGINT,
                created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Upgrade path for deployments created before payment_type/chat_id existed.
        await conn.execute("ALTER TABLE payment_logs ADD COLUMN IF NOT EXISTS payment_type TEXT")
        await conn.execute("ALTER TABLE payment_logs ADD COLUMN IF NOT EXISTS chat_id BIGINT")
        # Records which gateway ('paystack'/'stripe') this specific charge was
        # started under, so verify-time can use the SAME provider even if the
        # clone owner switches payment settings while the payment is pending —
        # see get_discord_clone_provider_key(). NULL/absent (pre-migration
        # rows, or Telegram callers that don't pass it) defaults to 'paystack'
        # at read time, matching every charge before this column existed.
        await conn.execute("ALTER TABLE payment_logs ADD COLUMN IF NOT EXISTS provider TEXT")

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS commission_tracking (
                commission_id SERIAL PRIMARY KEY,
                cloned_bot_id INTEGER NOT NULL REFERENCES cloned_bots(clone_id),
                payment_amount INTEGER NOT NULL,
                main_commission INTEGER NOT NULL,
                owner_amount INTEGER NOT NULL,
                stripe_key_id TEXT,
                payment_intent_id TEXT UNIQUE,
                status TEXT DEFAULT 'completed',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS subscription_payments (
                subscription_id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(user_id),
                payment_amount INTEGER NOT NULL,
                subscription_month TEXT NOT NULL,
                payment_method TEXT,
                payment_reference TEXT UNIQUE,
                status TEXT DEFAULT 'completed',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS botstore_listings (
                id TEXT PRIMARY KEY,
                owner_id BIGINT NOT NULL REFERENCES users(user_id),
                type TEXT NOT NULL,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                link TEXT,
                status TEXT DEFAULT 'pending',
                rating FLOAT DEFAULT 0,
                clicks INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS botstore_ratings (
                id SERIAL PRIMARY KEY,
                listing_id TEXT NOT NULL REFERENCES botstore_listings(id),
                user_id BIGINT NOT NULL,
                stars INTEGER NOT NULL,
                review TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS superbot_user_tiers (
                user_id BIGINT PRIMARY KEY REFERENCES users(user_id),
                tier TEXT DEFAULT 'basic',
                tier_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS superbot_referrals (
                id SERIAL PRIMARY KEY,
                referrer_id BIGINT NOT NULL,
                referee_id BIGINT NOT NULL,
                status TEXT DEFAULT 'active',
                reward_given BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS superbot_crypto_alerts (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                coin TEXT NOT NULL,
                price_threshold FLOAT NOT NULL,
                alert_type TEXT,
                active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ═══════════════════════════════════════════════════════════════════════════
        # 20 FEATURES TABLES
        # ════���������════════════════════════════════════════════════════════════���═════════

        # Feature 1-2: Inline Query & Chosen Results
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS inline_searches (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                query TEXT NOT NULL,
                result_id TEXT NOT NULL,
                result_type TEXT,
                was_chosen BOOLEAN DEFAULT FALSE,
                search_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Feature 3: My Chat Member (bot added/removed)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_group_membership (
                id SERIAL PRIMARY KEY,
                group_id BIGINT NOT NULL,
                clone_id BIGINT NOT NULL DEFAULT 0,
                bot_status TEXT DEFAULT 'member',
                status_changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (group_id, clone_id)
            )
        """)

        # Feature 4: Chat Member (track user joins/leaves)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_group_events (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                group_id BIGINT NOT NULL,
                event_type TEXT,
                event_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Features 5-6: Payment & Pre-Checkout
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                amount_usd FLOAT NOT NULL,
                currency TEXT DEFAULT 'USD',
                payment_method TEXT,
                status TEXT DEFAULT 'pending',
                tier_unlocked TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            )
        """)

        # Feature 7: Shipping
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS shipping_orders (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                item_id TEXT,
                shipping_address TEXT,
                shipping_option TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Feature 8: User Profile Photos
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_profile_photos (
                user_id BIGINT PRIMARY KEY,
                file_id TEXT NOT NULL,
                photo_url TEXT,
                downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Feature 9: Edit Message Tracking
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS edited_messages (
                id SERIAL PRIMARY KEY,
                message_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                original_text TEXT,
                edited_text TEXT,
                edit_count INTEGER DEFAULT 1,
                first_edited TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_edited TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Feature 10: Message Reactions
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS message_reactions (
                id SERIAL PRIMARY KEY,
                message_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                emoji TEXT NOT NULL,
                reaction_type TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(message_id, user_id, emoji)
            )
        """)

        # Feature 11: Polls
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS polls (
                id SERIAL PRIMARY KEY,
                poll_id TEXT NOT NULL UNIQUE,
                creator_id BIGINT NOT NULL,
                question TEXT NOT NULL,
                options TEXT ARRAY,
                vote_counts INTEGER ARRAY,
                is_closed BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Feature 12: Dice/Lottery Rolls
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS dice_rolls (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                dice_type TEXT DEFAULT 'cube',
                result INTEGER,
                reward INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Feature 13: Games
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_games (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                game_name TEXT NOT NULL,
                high_score INTEGER DEFAULT 0,
                play_count INTEGER DEFAULT 0,
                last_played TIMESTAMP
            )
        """)

        # Feature 14: Web App Data
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS web_app_sessions (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                session_token TEXT UNIQUE,
                web_app_data TEXT,
                write_access_allowed BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP
            )
        """)

        # Feature 15: Passport (Identity Verification)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS passport_verifications (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                document_type TEXT,
                verification_status TEXT DEFAULT 'pending',
                age_verified BOOLEAN DEFAULT FALSE,
                verified_age INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Feature 16: Location & Geofencing
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_locations (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                latitude FLOAT NOT NULL,
                longitude FLOAT NOT NULL,
                location_name TEXT,
                location_type TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS proximity_alerts (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                event_name TEXT NOT NULL,
                event_latitude FLOAT NOT NULL,
                event_longitude FLOAT NOT NULL,
                alert_radius_km FLOAT DEFAULT 5,
                alert_sent BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Feature 17: Video Chat Members
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS group_video_calls (
                id SERIAL PRIMARY KEY,
                group_id BIGINT NOT NULL,
                call_started TIMESTAMP,
                call_ended TIMESTAMP,
                participant_count INTEGER,
                status TEXT DEFAULT 'active'
            )
        """)

        # Feature 18: User Shared (Referrals)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_shares (
                id SERIAL PRIMARY KEY,
                sharer_id BIGINT NOT NULL,
                shared_user_id BIGINT NOT NULL,
                share_type TEXT,
                bonus_points INTEGER DEFAULT 100,
                bonus_claimed BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Feature 19: Deep Links Tracking
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS deep_links (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                link_code TEXT UNIQUE,
                target_type TEXT,
                target_id TEXT,
                clicked_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Feature 20: Write Access Allowed
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_write_access (
                user_id BIGINT PRIMARY KEY,
                write_access_allowed BOOLEAN DEFAULT FALSE,
                permission_granted_at TIMESTAMP,
                last_message_sent TIMESTAMP
            )
        """)

        # Clone Bot Payments - tracks pending/verified payments for bot cloning (Task 1)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS clone_payments (
                reference TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(user_id),
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Clone monetization subscription (20 GHS/month gate on connecting a
        # clone owner's own Paystack/Stripe key AND on setting custom prices
        # for their clone's paywalled features — see PRICE_REGISTRY in
        # config.py and get_clone_price()/set_clone_price() below).
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS clone_monetization_subscriptions (
                clone_id INTEGER PRIMARY KEY REFERENCES cloned_bots(clone_id) ON DELETE CASCADE,
                owner_id BIGINT NOT NULL,
                status VARCHAR(20) DEFAULT 'inactive',
                payment_reference VARCHAR(255),
                activated_at TIMESTAMP NULL,
                expires_at TIMESTAMP NULL
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_clone_monetization_status
            ON clone_monetization_subscriptions(status)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_clone_monetization_expires
            ON clone_monetization_subscriptions(expires_at)
        """)

        # Per-user Yandex direct-search subscription (config.IMAGE_SEARCH_YANDEX_FEE_GHS
        # /month). Scoped by (user_id, clone_id) since it's a user-level perk,
        # not a clone-level one — a user's subscription on one clone doesn't
        # carry over to another clone or the main bot.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS image_search_yandex_subscriptions (
                user_id BIGINT NOT NULL,
                clone_id INTEGER NOT NULL DEFAULT 0,
                status VARCHAR(20) DEFAULT 'inactive',
                payment_reference VARCHAR(255),
                authorization_code VARCHAR(255),
                activated_at TIMESTAMP NULL,
                expires_at TIMESTAMP NULL,
                PRIMARY KEY (user_id, clone_id)
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_image_search_yandex_expires
            ON image_search_yandex_subscriptions(expires_at)
        """)
        await conn.execute("""
            ALTER TABLE image_search_yandex_subscriptions ADD COLUMN IF NOT EXISTS authorization_code VARCHAR(255)
        """)
        await conn.execute("""
            ALTER TABLE image_search_yandex_subscriptions ADD COLUMN IF NOT EXISTS provider VARCHAR(20) DEFAULT 'paystack'
        """)

        # Per-payment pending row for the one-off "unlock source links" charge
        # (config.PRICE_REGISTRY "image_search_unlock"). Previously tracked
        # only in an in-memory dict on the cog (discord_bot/cogs/image_search.py),
        # so a bot restart between payment and the user tapping "Verify" lost
        # the pending state entirely, and there was no webhook backstop at
        # all — see api/paystack_webhook.py's 'image_search_unlock' case,
        # which now completes this row server-to-server. results_json holds
        # the search results (url/title pairs) so a webhook-driven DM (or a
        # user re-tapping Verify after a restart) can still deliver the
        # links that were paid for.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_image_search_unlock_payments (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                clone_id INTEGER NOT NULL DEFAULT 0,
                provider VARCHAR(20) DEFAULT 'paystack',
                payment_reference VARCHAR(255) UNIQUE NOT NULL,
                status VARCHAR(20) DEFAULT 'pending',
                results_json TEXT,
                created_at TIMESTAMP DEFAULT NOW(),
                completed_at TIMESTAMP NULL
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_discord_image_search_unlock_reference
            ON discord_image_search_unlock_payments(payment_reference)
        """)

        # ═══════════════════════════════════════════════════════════════════════════
        # PERSONAL MEDIA SERVER CONNECTIONS
        # A user connects a media server / cloud folder THEY own and control.
        # We only ever store a URL + an encrypted credential to talk to it —
        # never the media itself. See modules/jellyfin_client.py and
        # modules/gdrive_client.py.
        # ═══════════════════════════════════════════════════════════════════════════

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS jellyfin_connections (
                user_id BIGINT PRIMARY KEY REFERENCES users(user_id),
                server_url TEXT NOT NULL,
                encrypted_api_key TEXT NOT NULL,
                jellyfin_user_id TEXT NOT NULL,
                connected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS gdrive_connections (
                user_id BIGINT PRIMARY KEY REFERENCES users(user_id),
                encrypted_access_token TEXT NOT NULL,
                encrypted_refresh_token TEXT NOT NULL,
                token_expires_at TIMESTAMP NOT NULL,
                folder_id TEXT NOT NULL,
                folder_name TEXT,
                connected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Short-lived state tokens for the Google OAuth redirect round-trip
        # (CSRF protection + maps the callback back to the right Discord user)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS gdrive_oauth_states (
                state TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS plex_connections (
                user_id BIGINT PRIMARY KEY REFERENCES users(user_id),
                encrypted_access_token TEXT NOT NULL,
                server_name TEXT NOT NULL,
                base_url TEXT NOT NULL,
                connected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Short-lived Plex PIN-login sessions (id from plex.tv, per user)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS plex_pin_sessions (
                user_id BIGINT PRIMARY KEY,
                pin_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Media Connect subscription (Jellyfin/Plex/Drive movie search) —
        # per-user, $2/month, works across any server/DM (not guild-scoped
        # like premium_groups above).
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS media_connect_subscriptions (
                user_id BIGINT PRIMARY KEY REFERENCES users(user_id),
                status TEXT DEFAULT 'inactive',
                payment_reference TEXT,
                activated_at TIMESTAMP NULL,
                expires_at TIMESTAMP NULL
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_media_connect_sub_expires
            ON media_connect_subscriptions(expires_at)
        """)


        # ═══════════════════════════════════════════════════════════════════════════
        # NEW FEATURES TABLES (Task 1-14)
        # ═══════════════════════════════════════════════════════════════════════════

        # AI Chat Sessions — a persistent "active conversation" per user
        # (started with /newchat, continued with /aichat or a reply, ended
        # with /endchat) instead of every /aichat call being one-shot.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_chat_sessions (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(user_id),
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ended_at TIMESTAMP NULL,
                last_bot_message_id BIGINT NULL
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ai_chat_sessions_active
            ON ai_chat_sessions(user_id) WHERE ended_at IS NULL
        """)

        # AI Chat Usage & History
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_chat_usage (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(user_id),
                prompt TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Auto-migration for pre-existing rows/deployments: response text and
        # session linkage were added after ai_chat_usage first shipped.
        # ADD COLUMN IF NOT EXISTS is safe to re-run against an already
        # up-to-date DB (matches the pattern used throughout this file).
        await conn.execute("ALTER TABLE ai_chat_usage ADD COLUMN IF NOT EXISTS response TEXT")
        await conn.execute(
            "ALTER TABLE ai_chat_usage ADD COLUMN IF NOT EXISTS session_id INTEGER "
            "REFERENCES ai_chat_sessions(id) ON DELETE SET NULL"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_chat_usage_session_id ON ai_chat_usage(session_id)"
        )

        # AI Image Generation Usage
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_image_usage (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(user_id),
                prompt TEXT,
                image_url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Sponsored Posts
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sponsored_posts (
                id SERIAL PRIMARY KEY,
                admin_id BIGINT NOT NULL REFERENCES users(user_id),
                content TEXT NOT NULL,
                button_label TEXT,
                button_url TEXT,
                runs_remaining INTEGER NOT NULL,
                runs_total INTEGER NOT NULL,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Ad Submissions
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ad_submissions (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(user_id),
                company_name TEXT NOT NULL,
                ad_title TEXT NOT NULL,
                ad_description TEXT NOT NULL,
                target_url TEXT NOT NULL,
                budget_usd FLOAT NOT NULL,
                status TEXT DEFAULT 'pending',
                rejection_reason TEXT,
                submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                approved_at TIMESTAMP
            )
        """)

        # Services Marketplace
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS services_listings (
                id TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(user_id),
                service_name TEXT NOT NULL,
                service_title TEXT NOT NULL,
                description TEXT,
                price_usd FLOAT NOT NULL,
                category TEXT,
                rating FLOAT DEFAULT 0,
                reviews_count INTEGER DEFAULT 0,
                clicks INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Ad/Marketplace Referral Codes — one per user, generated on first
        # /referral mycode call. Kept separate from cloned_bots' clone-growth
        # referral system; this one is scoped to ads_marketplace.py activity.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ad_referral_codes (
                user_id BIGINT PRIMARY KEY REFERENCES users(user_id),
                code TEXT UNIQUE NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Ad/Marketplace Referral Ledger — a TRACKED figure only. Neither
        # ad_submissions.budget_usd nor services_listings.price_usd is ever
        # actually collected (see ads_marketplace.py's own docstring), so a
        # ledger row here is a proposed/would-be commission, not money that
        # has moved. Surfaced as such by /referral stats and get_revenue_by_type
        # callers should not treat this table as real revenue.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ad_referral_ledger (
                id SERIAL PRIMARY KEY,
                referrer_id BIGINT NOT NULL REFERENCES users(user_id),
                referred_user_id BIGINT NOT NULL REFERENCES users(user_id),
                source_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                tracked_amount_usd FLOAT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Managed Bot Tokens (user-registered bots)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS managed_bot_tokens (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(user_id),
                bot_name TEXT NOT NULL,
                bot_token TEXT UNIQUE NOT NULL,
                bot_username TEXT,
                is_valid BOOLEAN DEFAULT TRUE,
                last_verified TIMESTAMP,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Chat Lifecycle & Membership
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_memberships (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                bot_is_member BOOLEAN DEFAULT TRUE,
                autopost_link TEXT,
                autopost_label TEXT,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                left_at TIMESTAMP
            )
        """)

        # Recurring Posts (scheduled messages in chats)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS recurring_posts (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                admin_id BIGINT NOT NULL REFERENCES users(user_id),
                content TEXT NOT NULL,
                interval_hours INTEGER NOT NULL,
                last_posted TIMESTAMP,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Group Moderation Settings & Filters
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS group_moderation_settings (
                chat_id BIGINT PRIMARY KEY,
                admin_id BIGINT NOT NULL REFERENCES users(user_id),
                captcha_enabled BOOLEAN DEFAULT FALSE,
                captcha_timeout_seconds INTEGER DEFAULT 300,
                slow_mode_enabled BOOLEAN DEFAULT FALSE,
                slow_mode_interval_seconds INTEGER DEFAULT 5,
                night_mode_enabled BOOLEAN DEFAULT FALSE,
                night_mode_start_hour INTEGER,
                night_mode_end_hour INTEGER,
                word_filter_enabled BOOLEAN DEFAULT FALSE,
                anti_raid_enabled BOOLEAN DEFAULT FALSE,
                anti_raid_threshold INTEGER DEFAULT 5,
                anti_raid_window_minutes INTEGER DEFAULT 5,
                report_enabled BOOLEAN DEFAULT TRUE,
                logging_channel_id BIGINT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Blocked Words/Phrases
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS blocked_words (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                word_phrase TEXT NOT NULL,
                added_by BIGINT NOT NULL REFERENCES users(user_id),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, word_phrase)
            )
        """)

        # User Warns (for moderation)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_warns (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                chat_id BIGINT NOT NULL,
                reason TEXT,
                warned_by BIGINT NOT NULL REFERENCES users(user_id),
                warn_count INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Generic labeled-link buttons (feature #8) — reusable for "Pay Now",
        # "Join Our Channel", "Rules", or anything else an admin wants as a
        # one-tap button without needing new code per label.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS custom_link_buttons (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                label TEXT NOT NULL,
                url TEXT NOT NULL,
                position INTEGER DEFAULT 0,
                created_by BIGINT NOT NULL REFERENCES users(user_id),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, label)
            )
        """)

        # Custom Group Commands
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS custom_group_commands (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                command_name TEXT NOT NULL,
                response_text TEXT NOT NULL,
                created_by BIGINT NOT NULL REFERENCES users(user_id),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, command_name)
            )
        """)

        # Join Gate (mandatory subscribe link)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS join_gate_settings (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT,
                gate_link TEXT NOT NULL,
                gate_label TEXT DEFAULT 'Join Required',
                enabled BOOLEAN DEFAULT TRUE,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Mandatory Join Verifications
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS join_gate_verifications (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                verified BOOLEAN DEFAULT FALSE,
                verified_at TIMESTAMP,
                check_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Ad Analytics (click tracking for sponsored posts)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ad_analytics (
                id SERIAL PRIMARY KEY,
                ad_id INTEGER REFERENCES ad_submissions(id),
                impression_count INTEGER DEFAULT 0,
                click_count INTEGER DEFAULT 0,
                tracked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Moderation Logs
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS moderation_logs (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                action_type TEXT NOT NULL,
                target_user_id BIGINT,
                performed_by BIGINT NOT NULL REFERENCES users(user_id),
                reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # --- Autopost: bring recurring_posts up to date for minute-granularity
        # intervals and media support (originally text/hours-only). Uses
        # ALTER ... ADD COLUMN IF NOT EXISTS so this is safe to re-run against
        # a database that already has the older, narrower table.
        await conn.execute("ALTER TABLE recurring_posts ALTER COLUMN interval_hours DROP NOT NULL")
        await conn.execute("ALTER TABLE recurring_posts ADD COLUMN IF NOT EXISTS interval_minutes INTEGER")
        await conn.execute("ALTER TABLE recurring_posts ADD COLUMN IF NOT EXISTS media_file_id TEXT")
        await conn.execute("ALTER TABLE recurring_posts ADD COLUMN IF NOT EXISTS media_type TEXT")
        await conn.execute("ALTER TABLE recurring_posts ADD COLUMN IF NOT EXISTS failure_count INTEGER DEFAULT 0")

        # --- Migration: per-chat welcome message (Bot Manager / moderation extras) ---
        await conn.execute("ALTER TABLE chat_memberships ADD COLUMN IF NOT EXISTS welcome_message TEXT")

        # --- Migration: optional "Pay Now" button attached to the welcome message.
        # Generic — admin picks the label (e.g. "Pay Now", "Unlock VIP") and an
        # amount in GHS; not tied to any specific payment purpose.
        await conn.execute("ALTER TABLE chat_memberships ADD COLUMN IF NOT EXISTS pay_button_label TEXT")
        await conn.execute("ALTER TABLE chat_memberships ADD COLUMN IF NOT EXISTS pay_button_amount_ghs INTEGER")

        # --- Migration: bot_group_membership.group_id needs a UNIQUE
        # constraint — feature_handlers.handle_my_chat_member already used
        # "ON CONFLICT (group_id) DO UPDATE" without one, which Postgres
        # rejects at runtime with no matching unique/exclusion constraint.
        # This was a live bug before this migration existed. De-duplicate
        # first (keep the most recent row per group_id) since ON CONFLICT
        # having silently failed until now may have let duplicates accumulate.
        await conn.execute("""
            DELETE FROM bot_group_membership a USING bot_group_membership b
            WHERE a.group_id = b.group_id AND a.id < b.id
        """)
        await conn.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'bot_group_membership_group_id_key'
                ) THEN
                    ALTER TABLE bot_group_membership ADD CONSTRAINT bot_group_membership_group_id_key UNIQUE (group_id);
                END IF;
            END $$;
        """)

        # --- Migration: chat metadata for the admin panel's remote
        # group/channel picker (handlers/admin_remote.py) — previously this
        # table only tracked group_id + bot_status with no title/type/
        # username, so there was no way to show admins a readable list of
        # which chats the bot is actually in.
        await conn.execute("ALTER TABLE bot_group_membership ADD COLUMN IF NOT EXISTS chat_title TEXT")
        await conn.execute("ALTER TABLE bot_group_membership ADD COLUMN IF NOT EXISTS chat_type TEXT")
        await conn.execute("ALTER TABLE bot_group_membership ADD COLUMN IF NOT EXISTS chat_username TEXT")

        # --- Migration: tenant isolation. The main bot and every clone share
        # this one Postgres database, and this table previously had no way to
        # tell which bot a chat membership belonged to — so a clone's admin
        # panel / broadcast list could show groups that only the main bot (or
        # another clone entirely) was actually a member of. clone_id = 0 means
        # "the main bot"; a real clone_id means that specific clone.
        await conn.execute("ALTER TABLE bot_group_membership ADD COLUMN IF NOT EXISTS clone_id BIGINT NOT NULL DEFAULT 0")
        await conn.execute("""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'bot_group_membership_group_id_key'
                ) THEN
                    ALTER TABLE bot_group_membership DROP CONSTRAINT bot_group_membership_group_id_key;
                END IF;
            END $$;
        """)
        await conn.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'bot_group_membership_group_clone_key'
                ) THEN
                    ALTER TABLE bot_group_membership ADD CONSTRAINT bot_group_membership_group_clone_key UNIQUE (group_id, clone_id);
                END IF;
            END $$;
        """)

        # --- Migration: anti-flood tracking (Postgres-backed, not in-memory —
        # this bot is a stateless webhook, an in-process dict would reset
        # unpredictably between invocations) --------------------------------
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS flood_events (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                occurred_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_flood_events_lookup ON flood_events (chat_id, user_id, occurred_at)")

        # --- Migration: batch 1 of the auto-mod suite (feature #6) ---
        # message_text lets us detect "same message posted repeatedly", not
        # just "messages posted too fast" (those are different spam shapes).
        await conn.execute("ALTER TABLE flood_events ADD COLUMN IF NOT EXISTS message_text TEXT")

        # Auto Delete Links: now toggleable (was previously unconditional)
        # plus a per-group domain whitelist table below.
        await conn.execute("ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS auto_delete_links_enabled BOOLEAN DEFAULT TRUE")

        # Auto Ban on Spam: opt-in (default off, since banning is destructive
        # and this is a new behavior, unlike the pre-existing flood-mute).
        # Two independent triggers, both configurable:
        #   - spam_duplicate_threshold: same message text posted back-to-back
        #     this many times in a row
        #   - spam_flood_threshold / spam_flood_window_seconds: this many
        #     messages (any content) within this many seconds
        await conn.execute("ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS auto_ban_spam_enabled BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS spam_duplicate_threshold INTEGER DEFAULT 3")
        await conn.execute("ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS spam_flood_threshold INTEGER DEFAULT 10")
        await conn.execute("ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS spam_flood_window_seconds INTEGER DEFAULT 10")

        # --- Migration: batch 2 of the auto-mod suite (feature #6) ---
        # Auto Delete Service Messages: remove Telegram's own "X joined"/"X
        # left" notices. Opt-in since it changes visible chat behavior.
        await conn.execute("ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS auto_delete_service_messages BOOLEAN DEFAULT FALSE")

        # Auto Mute New Members: temporarily restrict posting for new joiners
        # to blunt join-and-spam bots. Skipped for a member if captcha is
        # already handling restriction for them (avoid double-gating).
        await conn.execute("ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS auto_mute_new_members_enabled BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS auto_mute_new_members_minutes INTEGER DEFAULT 10")

        # --- Migration: batch 3 of the auto-mod suite (feature #6) ---
        # Auto Warn -> Auto Ban Escalation: opt-in. When on, a user who
        # accumulates warn_ban_threshold total warns in a chat is banned
        # instead of just muted (the existing WARN_LIMIT_BEFORE_MUTE mute
        # still applies below that threshold).
        await conn.execute("ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS auto_ban_on_warns_enabled BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS warn_ban_threshold INTEGER DEFAULT 5")

        # Auto Pin Announcements: opt-in. When on, any admin message that
        # starts with auto_pin_tag (default "#pin") gets pinned automatically
        # — no need to reply-and-/pin.
        await conn.execute("ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS auto_pin_announcements_enabled BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS auto_pin_tag TEXT DEFAULT '#pin'")

        # Auto-DM on Join Request: opt-in. Requires the group's invite link to
        # be a "request to join" link (creates_join_request=True) — either
        # generated by /setjoinlink or created manually in Telegram's UI.
        # When on, the bot DMs the requester the main menu the instant their
        # join request comes in (this is allowed even for users who've never
        # messaged the bot before, since the join request itself counts as
        # the user initiating contact), then approves the request either way.
        await conn.execute("ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS auto_dm_on_join_enabled BOOLEAN DEFAULT FALSE")

        # --- Migration: Discord auto-mod additions (Phase 1 of the Discord --
        # expansion — see discord-bot-expansion-spec.md). SUPERSEDED — these
        # columns were the original approach (reusing group_moderation_settings)
        # but that table is Telegram-shared and had no clone_id; automod.py
        # now uses the dedicated discord_automod_config table instead (see
        # further down). Left here only because dropping columns another
        # process might still reference mid-deploy is riskier than a few
        # unused columns; nothing in the codebase reads these anymore.
        await conn.execute("ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS automod_action TEXT DEFAULT 'delete'")
        await conn.execute("ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS automod_timeout_minutes INTEGER DEFAULT 10")
        await conn.execute("ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS anti_invite_enabled BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS anti_mention_enabled BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS anti_mention_threshold INTEGER DEFAULT 5")
        await conn.execute("ALTER TABLE group_moderation_settings ADD COLUMN IF NOT EXISTS min_account_age_hours INTEGER DEFAULT 0")

        # --- Discord port: dedicated auto-mod config (Phase 1, replaces the
        # earlier group_moderation_settings-reuse approach) --------------------
        # The original implementation bolted Discord-specific columns onto
        # group_moderation_settings (see the migration comment further up,
        # kept for history) — but that table is Telegram-shared: its
        # admin_id column is a hard FK into `users(user_id)`, which is
        # Telegram's user table, so writing a Discord admin's id there was
        # one bad ensure_settings_row() call away from an FK violation. It
        # also had no clone_id, so two Discord bots in one guild shared
        # automod config. This table fixes both: it's Discord-only (no FK
        # into Telegram's users table) and (guild_id, clone_id) scoped like
        # every other new Discord table. banned_words is a JSONB array
        # rather than a separate table — a per-guild word list has no
        # timestamp/rate dimension, so a table+join bought nothing a JSONB
        # column doesn't already give us, and it keeps the whole filter
        # config collectable in one row read.
        #
        # Deliberately NOT migrating flood-event tracking (record/count/
        # clear_flood_events in modules/moderation_extra.py, backed by the
        # shared flood_events table) off the Telegram-shared table: it's a
        # transient per-user rate counter, not guild config, so two bots
        # tracking (and racing to clear) the same counter in a shared guild
        # is a minor, acceptable simplification rather than a data-leak —
        # unlike config, sharing a live spam counter doesn't let one bot's
        # settings silently override another's.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_automod_config (
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                action TEXT NOT NULL DEFAULT 'delete',
                timeout_minutes INTEGER NOT NULL DEFAULT 10,
                log_channel_id BIGINT,
                word_filter_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                banned_words JSONB NOT NULL DEFAULT '[]',
                anti_invite_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                anti_mention_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                anti_mention_threshold INTEGER NOT NULL DEFAULT 5,
                spam_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                spam_flood_threshold INTEGER NOT NULL DEFAULT 10,
                spam_flood_window_seconds INTEGER NOT NULL DEFAULT 10,
                min_account_age_hours INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS discord_automod_config_guild_clone_key
            ON discord_automod_config (guild_id, COALESCE(clone_id, -1))
        """)
        # log_channel_auto_created: true when the bot picked/created the
        # channel itself (auto-create-on-join flow) rather than an admin
        # setting it via /automod setlogchannel. Used to decide whether the
        # bot still owns follow-up "hey, here's your log channel" DMs to the
        # owner, and to stop those DMs the moment an admin takes over by
        # picking a channel manually.
        # log_channel_notice_count: how many of those owner DMs have gone
        # out (capped at 3 — see AutomodCog._collect_log_channel_item and
        # _send_combined_reminder).
        await conn.execute(
            "ALTER TABLE discord_automod_config ADD COLUMN IF NOT EXISTS log_channel_auto_created BOOLEAN NOT NULL DEFAULT FALSE"
        )
        await conn.execute(
            "ALTER TABLE discord_automod_config ADD COLUMN IF NOT EXISTS log_channel_notice_count INTEGER NOT NULL DEFAULT 0"
        )
        await conn.execute(
            "ALTER TABLE discord_automod_config ADD COLUMN IF NOT EXISTS log_channel_last_notice_at TIMESTAMPTZ"
        )
        # wordfilter_notice_count / wordfilter_last_notice_at: same capped-DM
        # pattern as the log-channel notices above, but nudging owners who
        # have never turned word_filter_enabled on at all (see
        # AutomodCog._collect_word_filter_item and _send_combined_reminder).
        # Stops permanently, for that guild, the moment word_filter_enabled
        # becomes true — see AutomodCog.toggle.
        await conn.execute(
            "ALTER TABLE discord_automod_config ADD COLUMN IF NOT EXISTS wordfilter_notice_count INTEGER NOT NULL DEFAULT 0"
        )
        await conn.execute(
            "ALTER TABLE discord_automod_config ADD COLUMN IF NOT EXISTS wordfilter_last_notice_at TIMESTAMPTZ"
        )
        # wizard_*: points at the most recently posted /automod setup wizard
        # message (if any and if it's still alive), same purpose and same
        # 3-column shape as discord_welcome_config's wizard_* columns —
        # lets the standalone /automod commands (toggle/action/
        # mentionthreshold/setlogchannel/bannedword add/remove/preset) push
        # a live refresh to it instead of leaving it showing stale info
        # until someone happens to click it. wizard_invoker_id mirrors the
        # invoker_id baked into that message's dynamic-item custom_ids
        # ("-"/NULL for the anyone-with-Manage-Server case) — refreshing
        # has to rebuild components with the SAME invoker_id or clicking
        # them afterward would enforce the wrong access rule.
        await conn.execute(
            "ALTER TABLE discord_automod_config ADD COLUMN IF NOT EXISTS wizard_channel_id BIGINT"
        )
        await conn.execute(
            "ALTER TABLE discord_automod_config ADD COLUMN IF NOT EXISTS wizard_message_id BIGINT"
        )
        await conn.execute(
            "ALTER TABLE discord_automod_config ADD COLUMN IF NOT EXISTS wizard_invoker_id BIGINT"
        )

        # discord_automod_reminder_batches: one row per COMBINED owner DM
        # sent by AutomodCog._reminder_loop. Previously each guild/type
        # (log-channel notice, word-filter notice) fired its own standalone
        # DM straight from _notify_owner_log_channel / _notify_owner_word_filter,
        # so an owner of two guilds could get up to 4 separate messages in
        # the same tick (and, after any downtime, the loop's startup/catch-up
        # pass could fire all of them for every guild whose cooldown had
        # elapsed while the bot was offline — see the reminder-burst
        # incident). _reminder_loop now collects every pending item per
        # owner first and sends ONE DM with a "Remind me later" / "Don't ask
        # again" button pair that applies to every item in that batch —
        # see _views_automod_reminders.py. `items` is a JSONB list of
        # {"type": "log_channel"|"word_filter", "guild_id": ..., ...} so the
        # button callbacks (and the /automod owner cleanupreminders command)
        # know which guild rows to update / which message to delete without
        # re-deriving anything from the (possibly stale) message content.
        # channel_id/message_id are filled in right after the DM send
        # succeeds (set_automod_reminder_batch_message) — a batch that never
        # got that far (DM failed / bot crashed mid-send) is harmless dead
        # weight, not a dangling reference anything else reads.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_automod_reminder_batches (
                id BIGSERIAL PRIMARY KEY,
                clone_id INTEGER,
                owner_id BIGINT NOT NULL,
                channel_id BIGINT,
                message_id BIGINT,
                items JSONB NOT NULL,
                resolved BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS discord_automod_reminder_batches_owner_idx "
            "ON discord_automod_reminder_batches (owner_id)"
        )

        # Registry of every server the bot (main or a clone) is currently
        # in. Nothing else doubled as this: every other guild_id column
        # only gets a row once that specific feature is configured/used,
        # so there was no single place to answer "what servers am I in"
        # or "when did I join/leave X" without scanning a dozen feature
        # tables and still missing servers that hadn't touched any of
        # them yet. left_at is nullable and set on_guild_remove instead of
        # deleting the row, so a re-join has history instead of looking
        # brand new, and so a kicked bot's guild data isn't silently lost.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_guilds (
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                guild_name TEXT,
                member_count INTEGER,
                invite_url TEXT,
                joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                left_at TIMESTAMPTZ
            )
        """)
        # Migration: invite_url added after initial release.
        await conn.execute("""
            ALTER TABLE discord_guilds ADD COLUMN IF NOT EXISTS invite_url TEXT
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS discord_guilds_guild_clone_key
            ON discord_guilds (guild_id, COALESCE(clone_id, -1))
        """)

        # roast.py: auto-triggered roast battles. Two tables —
        # discord_roast_activity tracks last-message time per (guild_id,
        # clone_id) so the inactivity poller can compute idle duration
        # without scanning message history, and discord_roast_battles is
        # one row per challenge from DM proposal through resolution
        # (awaiting_approval [member-requested only] -> pending ->
        # accepted/expired -> active -> ended), so a bot restart can
        # rehydrate in-flight challenges/battles instead of losing them. clone_id scoped like everything else Discord-side
        # since each clone is a separate bot instance.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_roast_activity (
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                last_message_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_roast_proposed_at TIMESTAMPTZ
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS discord_roast_activity_guild_clone_key
            ON discord_roast_activity (guild_id, COALESCE(clone_id, -1))
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_roast_battles (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                channel_id BIGINT NOT NULL,
                target_id BIGINT NOT NULL,
                proposed_by_admin_id BIGINT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                joined_ids BIGINT[] NOT NULL DEFAULT '{}',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at TIMESTAMPTZ NOT NULL,
                resolved_at TIMESTAMPTZ
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS discord_roast_battles_status_idx
            ON discord_roast_battles (status, expires_at)
        """)
        # Per-guild config for inactivity minutes + random-chance trigger,
        # stored generic key/value style like admin_config but scoped per
        # guild since it's a per-server tuning knob, not bot-wide.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_roast_config (
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                inactivity_minutes INTEGER NOT NULL DEFAULT 60,
                random_chance_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                random_check_minutes INTEGER NOT NULL DEFAULT 30,
                random_chance_percent INTEGER NOT NULL DEFAULT 10,
                enabled BOOLEAN NOT NULL DEFAULT TRUE
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS discord_roast_config_guild_clone_key
            ON discord_roast_config (guild_id, COALESCE(clone_id, -1))
        """)

        # Ship feature: bot randomly pairs two currently-active members and
        # posts an Accept/Reject prompt. discord_ship_config is per-guild
        # tuning (interval + odds), same generic key style as roast config.
        # discord_ship_history exists purely to avoid re-shipping the same
        # pair back-to-back and to rate-limit how often the poller fires
        # per guild — no long-term stats table was asked for.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_ship_config (
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                channel_id BIGINT,
                check_interval_minutes INTEGER NOT NULL DEFAULT 30,
                chance_percent INTEGER NOT NULL DEFAULT 15,
                enabled BOOLEAN NOT NULL DEFAULT FALSE,
                onboarding_dm_sent BOOLEAN NOT NULL DEFAULT FALSE
            )
        """)
        await conn.execute("""
            ALTER TABLE discord_ship_config
            ADD COLUMN IF NOT EXISTS onboarding_dm_sent BOOLEAN NOT NULL DEFAULT FALSE
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS discord_ship_config_guild_clone_key
            ON discord_ship_config (guild_id, COALESCE(clone_id, -1))
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_ship_history (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                user_a_id BIGINT NOT NULL,
                user_b_id BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS discord_ship_history_guild_idx
            ON discord_ship_history (guild_id, created_at)
        """)


        # Carl-bot-style self-assignable roles. Implemented with persistent
        # discord.ui.Button components (custom_id = f"rr:{role_id}") rather
        # than actual emoji reactions — buttons survive a bot restart via
        # bot.add_view(..., message_id=...) without needing the (privileged)
        # message_content or reaction-tracking intents that classic
        # emoji-reaction role bots need. discord_bot/cogs/reaction_roles.py
        # rebuilds one persistent view per panel message from this table on
        # cog load. message_id identifies the panel post; a panel can carry
        # up to 25 roles (Discord's per-view component limit).
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_reaction_roles (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                channel_id BIGINT NOT NULL,
                message_id BIGINT NOT NULL,
                role_id BIGINT NOT NULL,
                label TEXT NOT NULL,
                emoji TEXT,
                created_by BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE(message_id, role_id)
            )
        """)
        # Migration: add clone_id to a table that originally shipped without
        # it (see discord-bot-expansion-spec.md — every guild-scoped table
        # must be (guild_id, clone_id) scoped so a clone and the main bot
        # in the same guild don't share panels). NULL = main bot, matching
        # discord_premium_groups' convention. No FK here (unlike
        # discord_premium_groups) because discord_cloned_bots is created
        # later in this same _create_tables() pass — a FK would fail on a
        # cold-start DB. Referential integrity is enforced at the
        # application layer (clone_id always comes from an existing
        # discord_cloned_bots row via bot.clone_id).
        await conn.execute("""
            ALTER TABLE discord_reaction_roles ADD COLUMN IF NOT EXISTS clone_id INTEGER
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_discord_reaction_roles_message
            ON discord_reaction_roles (message_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_discord_reaction_roles_guild
            ON discord_reaction_roles (guild_id, clone_id)
        """)

        # --- Discord port: leveling / XP (Phase 2, ProBot parity) ---------------
        # Deliberately its own table, NOT sharing storage with the economy
        # game (Phase 3) — see discord-bot-expansion-spec.md's decision log
        # for why the two point systems are kept separate. total_xp is
        # cumulative (never decreases); `level` is a cached derived value
        # (modules/leveling.compute_level) stored alongside it purely so
        # /leaderboard can ORDER BY without recomputing for every row.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_xp (
                guild_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                clone_id INTEGER,
                total_xp INTEGER NOT NULL DEFAULT 0,
                level INTEGER NOT NULL DEFAULT 0,
                last_xp_at TIMESTAMPTZ
            )
        """)
        # Migration: this table originally had PRIMARY KEY (guild_id,
        # user_id) with no clone_id at all, which meant a clone and the
        # main bot in the same guild would add XP to (and read/reset) the
        # exact same row. Widen the key to include clone_id. Two-step
        # (add column, then swap the constraint) so it's safe to run
        # against a table that already has rows from before this fix.
        await conn.execute("""
            ALTER TABLE discord_xp ADD COLUMN IF NOT EXISTS clone_id INTEGER
        """)
        await conn.execute("""
            ALTER TABLE discord_xp DROP CONSTRAINT IF EXISTS discord_xp_pkey
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS discord_xp_guild_clone_user_key
            ON discord_xp (guild_id, COALESCE(clone_id, -1), user_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_discord_xp_leaderboard
            ON discord_xp (guild_id, clone_id, total_xp DESC)
        """)

        # Level-up role rewards. Rewards STACK (a member keeps every role
        # for every level they've passed) rather than replacing the
        # previous one — simpler to reason about for both admins configuring
        # it and members losing a role unexpectedly; a guild that wants
        # "highest role only" can still get that by only configuring one
        # level-role, or an admin can prune manually.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_level_roles (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                level INTEGER NOT NULL,
                role_id BIGINT NOT NULL
            )
        """)
        # Migration: add clone_id (see discord_xp migration above for why —
        # same bug, same fix). UNIQUE(guild_id, level) previously meant a
        # clone couldn't configure its own level-role ladder in a guild the
        # main bot is also running level rewards in.
        await conn.execute("""
            ALTER TABLE discord_level_roles ADD COLUMN IF NOT EXISTS clone_id INTEGER
        """)
        await conn.execute("""
            ALTER TABLE discord_level_roles DROP CONSTRAINT IF EXISTS discord_level_roles_guild_id_level_key
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS discord_level_roles_guild_clone_level_key
            ON discord_level_roles (guild_id, COALESCE(clone_id, -1), level)
        """)

        # --- Guild-level leveling config (new) ------------------------------
        # Previously there was no per-guild leveling settings row at all —
        # _send_level_up_card just posted in whatever channel the message
        # that triggered the level-up was sent in. This adds an optional
        # dedicated announce channel (e.g. #level-ups from /setup channels)
        # without changing that fallback behavior: announce_channel_id NULL
        # still means "post in the triggering channel", it's opt-in.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_leveling_config (
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                announce_channel_id BIGINT,
                announce_auto_created BOOLEAN NOT NULL DEFAULT FALSE,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS discord_leveling_config_guild_clone_key
            ON discord_leveling_config (guild_id, COALESCE(clone_id, -1))
        """)
        # xp_rate: Slow/Default/Fast multiplier applied to the random 15-25
        # XP awarded per message in LevelingCog.on_message — this setting
        # did not exist anywhere before (the leveling_setup_wizard.html
        # mockup's Step 1 had nothing behind it); added for real here, not
        # just as a UI label, so picking "Fast" in the wizard actually
        # changes XP gain. 'default' = 1.0x, matches existing unmultiplied
        # behavior exactly so nothing changes for guilds that never touch it.
        await conn.execute(
            "ALTER TABLE discord_leveling_config ADD COLUMN IF NOT EXISTS xp_rate TEXT NOT NULL DEFAULT 'default'"
        )
        # card_style: users kept asking for a way to turn the image card off
        # (some hosts throttle/behave oddly with frequent PIL-rendered image
        # uploads, others just prefer a plain text ping) — 'card' preserves
        # exact existing behavior for anyone who never touches this setting.
        await conn.execute(
            "ALTER TABLE discord_leveling_config ADD COLUMN IF NOT EXISTS card_style TEXT NOT NULL DEFAULT 'card'"
        )
        await conn.execute(
            "ALTER TABLE discord_leveling_config ADD COLUMN IF NOT EXISTS wizard_channel_id BIGINT"
        )
        await conn.execute(
            "ALTER TABLE discord_leveling_config ADD COLUMN IF NOT EXISTS wizard_message_id BIGINT"
        )
        await conn.execute(
            "ALTER TABLE discord_leveling_config ADD COLUMN IF NOT EXISTS wizard_invoker_id BIGINT"
        )

        # --- Leaderboard server links (feature expansion) -------------------
        # /leadership shows the top-10 XP earners with a "join server" button
        # next to any member who has submitted (and had approved) their own
        # server's invite link. One row per (guild_id, clone_id, user_id) —
        # status starts 'pending' after the member submits via the DM-modal
        # prompt (see _views_leaderboard_links.py) and only flips to
        # 'approved' once a Manage Server admin in that guild approves it via
        # the review card. 'denied' is kept (not deleted) so we don't
        # re-prompt a member whose link was already rejected.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_leader_links (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                user_id BIGINT NOT NULL,
                invite_url TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                reviewed_at TIMESTAMPTZ,
                reviewed_by BIGINT
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS discord_leader_links_guild_clone_user_key
            ON discord_leader_links (guild_id, COALESCE(clone_id, -1), user_id)
        """)

        # --- Download channel (feature expansion) ---------------------------
        # /download setup — a per-guild "downloads" channel where anyone can
        # submit a music/video link via a modal and the bot fetches + reposts
        # the actual file natively (not just the raw link). channel_id NULL
        # means not configured yet; panel_channel_id/panel_message_id track
        # the persistent Submit-Download panel posted IN that channel so it
        # can be re-attached (DynamicItem custom_ids) after a bot restart,
        # same convention as every other wizard's wizard_channel_id/message_id.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_download_config (
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                channel_id BIGINT,
                channel_auto_created BOOLEAN NOT NULL DEFAULT FALSE,
                panel_channel_id BIGINT,
                panel_message_id BIGINT,
                wizard_channel_id BIGINT,
                wizard_message_id BIGINT,
                wizard_invoker_id BIGINT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS discord_download_config_guild_clone_key
            ON discord_download_config (guild_id, COALESCE(clone_id, -1))
        """)

        # --- Discord port: invite tracker (feature expansion) -------------------
        # Tracks which invite (and which inviter) is responsible for each new
        # member — Discord equivalent of ProBot/MEE6's invite tracker. config
        # is per-guild announce channel + on/off switch + wizard pointer, same
        # shape as every other on-join wizard here (discord_download_config
        # right above is the closest template).
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_invite_tracker_config (
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                channel_id BIGINT,
                channel_auto_created BOOLEAN NOT NULL DEFAULT FALSE,
                wizard_channel_id BIGINT,
                wizard_message_id BIGINT,
                wizard_invoker_id BIGINT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS discord_invite_tracker_config_guild_clone_key
            ON discord_invite_tracker_config (guild_id, COALESCE(clone_id, -1))
        """)
        # wizard_due_at: when the auto-posted setup wizard is scheduled to
        # go out — set to now()+1h on guild join rather than posting right
        # away (InvitesCog._scheduler_loop is what actually posts it once
        # due). NULL once posted (remember_wizard_message doesn't touch
        # it, but get_due_invite_wizard_guilds excludes anything with a
        # wizard_message_id already set) or if the wizard was instead
        # brought up manually via /invites setup before the delay elapsed.
        await conn.execute(
            "ALTER TABLE discord_invite_tracker_config ADD COLUMN IF NOT EXISTS wizard_due_at TIMESTAMPTZ"
        )

        # Live cache of each active invite's use-count/inviter, refreshed on
        # every on_member_join diff and rebuilt wholesale on bot startup and
        # guild join (InvitesCog._snapshot_invites). Persisted rather than
        # kept in memory only, so a bot restart doesn't lose the baseline
        # needed to correctly attribute the very next join afterward.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_invite_cache (
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                invite_code TEXT NOT NULL,
                uses INTEGER NOT NULL DEFAULT 0,
                inviter_id BIGINT,
                is_vanity BOOLEAN NOT NULL DEFAULT FALSE,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS discord_invite_cache_guild_clone_code_key
            ON discord_invite_cache (guild_id, COALESCE(clone_id, -1), invite_code)
        """)

        # One row per join, closed off (left_at set) on member leave — the
        # "net" count (joins minus leaves still open) is what's actually
        # shown, so an alt account joining and immediately leaving doesn't
        # inflate an inviter's real count.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_invite_joins (
                id BIGSERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                member_id BIGINT NOT NULL,
                inviter_id BIGINT,
                invite_code TEXT,
                joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                left_at TIMESTAMPTZ
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_discord_invite_joins_member
            ON discord_invite_joins (guild_id, COALESCE(clone_id, -1), member_id, left_at)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_discord_invite_joins_inviter
            ON discord_invite_joins (guild_id, COALESCE(clone_id, -1), inviter_id)
        """)

        # --- Discord port: music panel (feature expansion) ---------------------
        # Queue/playback state itself is in-memory only (per-guild, lives inside
        # music.py's GuildMusicState) — not persisted here, since it wouldn't
        # survive a restart anyway (voice connections drop on process exit) and
        # rebuilding a queue automatically post-restart was explicitly flagged as
        # out of scope for v1. This table only remembers WHERE the persistent
        # Now Playing panel message lives, same panel_channel_id/panel_message_id
        # convention as discord_download_config above, so it can be re-attached
        # (DynamicItem custom_ids) after a bot restart instead of going dead.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_music_panel (
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                panel_channel_id BIGINT,
                panel_message_id BIGINT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS discord_music_panel_guild_clone_key
            ON discord_music_panel (guild_id, COALESCE(clone_id, -1))
        """)

        # --- Discord port: voice XP (feature expansion) -------------------------
        # Per-guild config for voice-channel XP. Reuses discord_xp for storage —
        # voice XP and text XP add to the SAME total_xp/level, same as ProBot —
        # so /rank and /leaderboard need no changes at all.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_voice_xp_config (
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                xp_per_minute INTEGER NOT NULL DEFAULT 10,
                afk_channel_excluded BOOLEAN NOT NULL DEFAULT TRUE
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS discord_voice_xp_config_guild_clone_key
            ON discord_voice_xp_config (guild_id, COALESCE(clone_id, -1))
        """)

        # --- Discord port: starboard (feature expansion) ------------------------
        # Per-guild config (one starboard channel + threshold per guild/clone),
        # plus a mapping table so repeat reactions on the same message update
        # the same starboard post's star count instead of re-posting it.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_starboard_config (
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                channel_id BIGINT,
                threshold INTEGER NOT NULL DEFAULT 5,
                emoji TEXT NOT NULL DEFAULT '⭐'
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS discord_starboard_config_guild_clone_key
            ON discord_starboard_config (guild_id, COALESCE(clone_id, -1))
        """)
        # wizard_*: same purpose/shape as the automod/ticket wizards' pointer
        # columns. The /community setup wizard combines starboard AND
        # suggestions into one message, but only needs one pointer to find
        # it again — kept here (rather than duplicated on
        # discord_suggestion_config too) since starboard is the wizard's
        # first section and this table already exists per-guild by the time
        # suggestions gets configured.
        await conn.execute(
            "ALTER TABLE discord_starboard_config ADD COLUMN IF NOT EXISTS wizard_channel_id BIGINT"
        )
        await conn.execute(
            "ALTER TABLE discord_starboard_config ADD COLUMN IF NOT EXISTS wizard_message_id BIGINT"
        )
        await conn.execute(
            "ALTER TABLE discord_starboard_config ADD COLUMN IF NOT EXISTS wizard_invoker_id BIGINT"
        )
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_starboard_posts (
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                source_message_id BIGINT NOT NULL,
                source_channel_id BIGINT NOT NULL,
                starboard_message_id BIGINT NOT NULL,
                star_count INTEGER NOT NULL DEFAULT 0
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS discord_starboard_posts_guild_clone_source_key
            ON discord_starboard_posts (guild_id, COALESCE(clone_id, -1), source_message_id)
        """)

        # --- Discord port: suggestion box (feature expansion) -------------------
        # One row per suggestion. status drives the embed color/footer and
        # whether it still shows up as "pending" — approved/denied is a manual
        # staff action (✅/❌ reaction), not automatic on any vote threshold.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_suggestions (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                author_id BIGINT NOT NULL,
                message_id BIGINT NOT NULL,
                channel_id BIGINT NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                upvotes INTEGER NOT NULL DEFAULT 0,
                downvotes INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS discord_suggestions_message_key
            ON discord_suggestions (message_id)
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_suggestion_config (
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                approved_log_channel_id BIGINT
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS discord_suggestion_config_guild_clone_key
            ON discord_suggestion_config (guild_id, COALESCE(clone_id, -1))
        """)

        # --- Discord port: ticket / support system (feature expansion) ----------
        # discord_ticket_config: one panel config per guild (support role,
        # category to create ticket channels under). discord_tickets: one row
        # per open/closed ticket channel, with a claimed_by staff id and a
        # closed flag so /ticket close can be re-run idempotently on restart.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_ticket_config (
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                support_role_id BIGINT,
                category_id BIGINT,
                panel_channel_id BIGINT,
                panel_message_id BIGINT
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS discord_ticket_config_guild_clone_key
            ON discord_ticket_config (guild_id, COALESCE(clone_id, -1))
        """)
        # welcome_message: shown in the embed posted inside a freshly-opened
        # ticket channel, in place of the hardcoded "Thanks for reaching
        # out..." line — same {member}/{guild} placeholder convention as
        # discord_welcome_config.message_template.
        await conn.execute(
            "ALTER TABLE discord_ticket_config ADD COLUMN IF NOT EXISTS welcome_message TEXT"
        )
        # wizard_*: same purpose/shape as discord_automod_config's wizard_*
        # columns — points at the most recently posted /ticket setup wizard
        # message so it can be refreshed in place. Ticket panel_channel_id/
        # panel_message_id above are a DIFFERENT pointer (the posted "Open
        # Ticket" panel itself) — the wizard message is a separate,
        # admin-only control surface.
        await conn.execute(
            "ALTER TABLE discord_ticket_config ADD COLUMN IF NOT EXISTS wizard_channel_id BIGINT"
        )
        await conn.execute(
            "ALTER TABLE discord_ticket_config ADD COLUMN IF NOT EXISTS wizard_message_id BIGINT"
        )
        await conn.execute(
            "ALTER TABLE discord_ticket_config ADD COLUMN IF NOT EXISTS wizard_invoker_id BIGINT"
        )
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_tickets (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                channel_id BIGINT NOT NULL,
                opener_id BIGINT NOT NULL,
                claimed_by BIGINT,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                closed_at TIMESTAMPTZ
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS discord_tickets_channel_key
            ON discord_tickets (channel_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_discord_tickets_opener
            ON discord_tickets (guild_id, clone_id, opener_id, status)
        """)

        # --- Discord port: giveaways (feature expansion) -------------------------
        # One row per giveaway. entrant ids stored as a BIGINT[] array (simpler
        # than a join table for the scale a Discord giveaway needs — low
        # thousands of entrants at most). ends_at drives the background poller
        # in giveaways.py; winner_ids is filled in once rolled so /giveaway
        # reroll has something to exclude/replace.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_giveaways (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                channel_id BIGINT NOT NULL,
                message_id BIGINT NOT NULL,
                host_id BIGINT NOT NULL,
                prize TEXT NOT NULL,
                winner_count INTEGER NOT NULL DEFAULT 1,
                ends_at TIMESTAMPTZ NOT NULL,
                entrant_ids BIGINT[] NOT NULL DEFAULT '{}',
                winner_ids BIGINT[] NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'active'
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS discord_giveaways_message_key
            ON discord_giveaways (message_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_discord_giveaways_active
            ON discord_giveaways (status, ends_at)
        """)
        # role_requirement_id: optional role a member must already hold to
        # enter — nothing enforced this before (handle_entry in
        # giveaways.py let anyone in), matching the giveaway_setup_wizard.html
        # mockup's optional "Role requirement" step, added for real here.
        await conn.execute(
            "ALTER TABLE discord_giveaways ADD COLUMN IF NOT EXISTS role_requirement_id BIGINT"
        )

        # discord_giveaway_drafts: transient state for the /giveaway setup
        # wizard while it's being filled in — unlike the other five wizards,
        # a giveaway isn't an existing per-guild config row being edited, it's
        # a NEW thing not created yet, so there's nothing in discord_giveaways
        # to point a wizard at until "Start giveaway" is pressed. Keyed by
        # the wizard message itself (one draft per open wizard) rather than
        # by guild, so an admin can have more than one giveaway wizard open
        # (e.g. two channels) without them clobbering each other. Deleted
        # once the giveaway is actually started; a draft left abandoned
        # (wizard message just... never finished) is harmless dead weight,
        # same as an unclicked /ticket setup wizard.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_giveaway_drafts (
                wizard_message_id BIGINT PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                wizard_channel_id BIGINT NOT NULL,
                invoker_id BIGINT NOT NULL,
                prize TEXT,
                duration_seconds INTEGER,
                target_channel_id BIGINT,
                winner_count INTEGER NOT NULL DEFAULT 1,
                role_requirement_id BIGINT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # --- Discord port: general-purpose scheduled messages (feature expansion) -
        # Distinct from discord_autopost_config (that's the fixed self-promo
        # rotation) — this is arbitrary admin-authored messages, one-off or
        # recurring, on their own schedule. `interval_seconds` NULL means a
        # one-off post that disables itself after sending once; otherwise it
        # reschedules `next_run_at += interval_seconds` after each send.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_scheduled_messages (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                channel_id BIGINT NOT NULL,
                content TEXT NOT NULL,
                next_run_at TIMESTAMPTZ NOT NULL,
                interval_seconds INTEGER,
                created_by BIGINT NOT NULL,
                enabled BOOLEAN NOT NULL DEFAULT TRUE
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_discord_scheduled_messages_due
            ON discord_scheduled_messages (enabled, next_run_at)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_discord_scheduled_messages_guild
            ON discord_scheduled_messages (guild_id, clone_id)
        """)

        # --- Discord port: welcome cards (Phase 2, ProBot parity) ---------------
        # One row per guild. message_template supports {member}/{guild}/
        # {count} placeholders, applied in discord_bot/cogs/welcome.py.
        # Colors are hex strings so this stays JSON/UI-friendly if a config
        # dashboard is ever built (see the expansion spec's open question
        # about a dashboard) without needing a schema change.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_welcome_config (
                guild_id BIGINT NOT NULL,
                enabled BOOLEAN NOT NULL DEFAULT FALSE,
                channel_id BIGINT,
                message_template TEXT NOT NULL DEFAULT 'Welcome {member} to {guild}! You are member #{count}.',
                background_color TEXT NOT NULL DEFAULT '#2b2d31',
                accent_color TEXT NOT NULL DEFAULT '#5865F2',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        # Migration: add clone_id and drop the guild_id-only PRIMARY KEY —
        # same bug/fix pattern as discord_xp above. Was PRIMARY KEY
        # (guild_id) with no clone_id, so a clone couldn't have its own
        # welcome message/channel in a guild the main bot also welcomes
        # members in.
        await conn.execute("""
            ALTER TABLE discord_welcome_config ADD COLUMN IF NOT EXISTS clone_id INTEGER
        """)
        await conn.execute("""
            ALTER TABLE discord_welcome_config DROP CONSTRAINT IF EXISTS discord_welcome_config_pkey
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS discord_welcome_config_guild_clone_key
            ON discord_welcome_config (guild_id, COALESCE(clone_id, -1))
        """)
        # Nudge tracking for WelcomeCog._nudge_owners: when a guild has never
        # turned welcome cards on, we DM the owner a one-time preview + an
        # Approve/Deny pair instead of silently doing nothing forever.
        # nudge_status distinguishes "haven't asked" (NULL) from "owner said
        # no" (denied) so a denial doesn't get re-asked every cycle the way
        # a never-configured guild does.
        await conn.execute("""
            ALTER TABLE discord_welcome_config ADD COLUMN IF NOT EXISTS nudge_sent_at TIMESTAMPTZ
        """)
        await conn.execute("""
            ALTER TABLE discord_welcome_config ADD COLUMN IF NOT EXISTS nudge_status TEXT
        """)
        # channel_auto_created: same pattern as
        # discord_automod_config.log_channel_auto_created — true when the
        # /setup channels flow created #welcome itself, so a future re-run
        # of that flow knows this channel is already "ours" rather than
        # something an admin picked manually.
        await conn.execute("""
            ALTER TABLE discord_welcome_config ADD COLUMN IF NOT EXISTS channel_auto_created BOOLEAN NOT NULL DEFAULT FALSE
        """)
        # sticker_url: an optional GIF/image URL posted as a follow-up
        # message right after the welcome card PNG. Kept as a separate
        # message rather than baked into the card image because
        # render_welcome_card produces a static PNG (Pillow), so an
        # animated GIF can't be composited into it — Discord will still
        # auto-embed/animate a GIF URL sent as its own message. Empty
        # string means "off"; the default is a direct .gif file (NOT a
        # tenor.com/view/... page link — those are HTML, not an image,
        # and won't decode) so a fresh guild gets a working sticker
        # without an admin having to set one.
        await conn.execute("""
            ALTER TABLE discord_welcome_config ADD COLUMN IF NOT EXISTS sticker_url TEXT NOT NULL DEFAULT 'https://media1.tenor.com/m/m9knzx4hgYUAAAAC/party-excited.gif'
        """)
        # card_style: 'gif' composites the sticker's frames into the card
        # itself (modules/welcome_card.py render_welcome_card animate=True)
        # so it dances in place; 'static' pastes a single sticker frame
        # instead, producing a plain PNG. Admin-togglable per guild via
        # /welcome style, independent of which sticker_url is set.
        await conn.execute("""
            ALTER TABLE discord_welcome_config ADD COLUMN IF NOT EXISTS card_style TEXT NOT NULL DEFAULT 'gif'
        """)
        # avatar_shape: which mask modules/welcome_card.py cuts the
        # member's avatar into. 'circle' (original behavior) stays the
        # default so existing guilds render unchanged; admins pick one of
        # the other 4 via the wizard's Step 6 select.
        await conn.execute("""
            ALTER TABLE discord_welcome_config ADD COLUMN IF NOT EXISTS avatar_shape TEXT NOT NULL DEFAULT 'circle'
        """)
        # Backfill fix: the column above originally seeded every existing
        # row's sticker_url with a tenor.com/view/... PAGE link, which is
        # HTML, not an image — _fetch_sticker_bytes can't decode it, so
        # affected guilds silently got no sticker at all despite card_style
        # defaulting to 'gif'. This one-time UPDATE repoints any row still
        # holding that broken default to a real, direct .gif file so the
        # sticker actually renders without an admin having to touch
        # /welcome sticker themselves. Guilds that already customized
        # sticker_url to something else are untouched (WHERE clause only
        # matches the old broken default).
        await conn.execute("""
            UPDATE discord_welcome_config
            SET sticker_url = 'https://media1.tenor.com/m/m9knzx4hgYUAAAAC/party-excited.gif'
            WHERE sticker_url = 'https://tenor.com/view/party-excited-excitement-minions-2-rise-of-gru-gif-11230050916342268293'
        """)
        # sticker_announced_at / sticker_announce_status: tracks the
        # one-time "hey, your welcome cards can now have a dancing sticker"
        # DM sent to owners of guilds that already had welcome cards
        # enabled before this feature existed — separate from
        # nudge_sent_at/nudge_status above, which is only for guilds that
        # never turned welcome cards on at all. Same shape/reasoning as
        # that pair: a NULL announced_at means "haven't told them yet",
        # and status distinguishes an explicit opt-out from silence so a
        # dismissal doesn't get re-sent every cycle.
        await conn.execute("""
            ALTER TABLE discord_welcome_config ADD COLUMN IF NOT EXISTS sticker_announced_at TIMESTAMPTZ
        """)
        await conn.execute("""
            ALTER TABLE discord_welcome_config ADD COLUMN IF NOT EXISTS sticker_announce_status TEXT
        """)
        # use_template: whether render_welcome_card should draw the designed
        # welcome_bg_wolf.png template card instead of the plain flat-color
        # card. Defaults TRUE so brand-new rows (and guilds that never
        # customized their card) get the new look automatically. The
        # backfill below flips it to FALSE for any row that already exists
        # with non-default background_color/accent_color/avatar_shape/
        # sticker_url — i.e. a guild that had actually customized its card
        # before the template existed — so deploying this never silently
        # discards someone's edits. WHERE template_announce_status IS NULL
        # makes this safe to re-run on every startup: once an owner has
        # acted on the one-time announcement below (see
        # template_announce_status), this backfill leaves their choice
        # alone instead of re-flipping it back to FALSE on the next
        # customization.
        await conn.execute("""
            ALTER TABLE discord_welcome_config ADD COLUMN IF NOT EXISTS use_template BOOLEAN NOT NULL DEFAULT TRUE
        """)
        # template_announced_at / template_announce_status: same one-time
        # "hey, try the new thing" pattern as sticker_announced_at/
        # sticker_announce_status above, for the guilds the backfill just
        # kept on their old flat card. NULL announced_at = "haven't told
        # them yet"; status distinguishes an explicit "try it" / "no
        # thanks" from silence so a dismissal doesn't get re-sent every
        # cycle. Added BEFORE the backfill UPDATE below since that query's
        # WHERE clause reads template_announce_status — running the
        # UPDATE first (on a fresh DB, or one that hasn't hit this
        # migration yet) would fail with UndefinedColumnError.
        await conn.execute("""
            ALTER TABLE discord_welcome_config ADD COLUMN IF NOT EXISTS template_announced_at TIMESTAMPTZ
        """)
        await conn.execute("""
            ALTER TABLE discord_welcome_config ADD COLUMN IF NOT EXISTS template_announce_status TEXT
        """)
        await conn.execute("""
            UPDATE discord_welcome_config
            SET use_template = FALSE
            WHERE template_announce_status IS NULL
              AND (
                background_color <> '#2b2d31' OR accent_color <> '#5865F2' OR avatar_shape <> 'circle'
                OR (sticker_url <> '' AND sticker_url <> 'https://media1.tenor.com/m/m9knzx4hgYUAAAAC/party-excited.gif')
              )
        """)
        # delivery_mode: 'channel' (default, unchanged behavior — posts in
        # the configured channel) or 'dm' (sends the card straight to the
        # new member instead, for admins who don't want join spam visible
        # to the rest of the server). channel_id stays required/used for
        # 'channel' mode only; 'dm' mode ignores it.
        await conn.execute("""
            ALTER TABLE discord_welcome_config ADD COLUMN IF NOT EXISTS delivery_mode TEXT NOT NULL DEFAULT 'channel'
        """)
        # wizard_*: points at the most recently posted /welcome setup
        # wizard message (if any and if it's still alive), so the 6
        # standalone /welcome commands (enable/disable/message/colors/
        # sticker/style) can push a live refresh to it instead of leaving
        # it showing stale info until someone happens to click it.
        # wizard_invoker_id mirrors the invoker_id baked into that
        # message's own dynamic-item custom_ids ("-"/NULL for the
        # anyone-with-Manage-Server auto-posted wizard) — refreshing has
        # to rebuild components with the SAME invoker_id or clicking them
        # afterward would enforce the wrong access rule.
        await conn.execute("""
            ALTER TABLE discord_welcome_config ADD COLUMN IF NOT EXISTS wizard_channel_id BIGINT
        """)
        await conn.execute("""
            ALTER TABLE discord_welcome_config ADD COLUMN IF NOT EXISTS wizard_message_id BIGINT
        """)
        await conn.execute("""
            ALTER TABLE discord_welcome_config ADD COLUMN IF NOT EXISTS wizard_invoker_id BIGINT
        """)
        # card_theme: which THEME_BACKGROUNDS key (modules/welcome_card.py)
        # this guild's template card renders — 'wolf' (free) by default, or
        # one of the premium card-pack themes once purchased.
        # card_pack_unlocked: whether this guild has bought the premium
        # welcome-card pack (see the `welcome_card_pack` payment_type below
        # and discord_bot/cogs/welcome.py's `buypack`/`theme` commands). A
        # whole-guild, one-time unlock — NOT per-user like discord_premium_groups
        # — so any admin's payment unlocks the themes for every future join,
        # deliberately separate from that payment_logs-driven per-user gate.
        await conn.execute("""
            ALTER TABLE discord_welcome_config ADD COLUMN IF NOT EXISTS card_theme TEXT NOT NULL DEFAULT 'wolf'
        """)
        await conn.execute("""
            ALTER TABLE discord_welcome_config ADD COLUMN IF NOT EXISTS card_pack_unlocked BOOLEAN NOT NULL DEFAULT FALSE
        """)
        # ultra_pack_unlocked / custom_background_url: a SEPARATE one-time
        # unlock from card_pack_unlocked above — instead of picking one of
        # the fixed artist themes, an ultra-pack guild points the template
        # card at their OWN png/jpeg via /welcome custombg (see the
        # `ultra_welcome_pack` payment_type in discord_bot/views_card_pack.py
        # and modules/welcome_card.py's _draw_custom_bg_card). Kept as its
        # own flag rather than folding into card_pack_unlocked so a guild
        # can own either, both, or neither independently — and so an admin
        # clearing custom_background_url doesn't need to touch the artist
        # themes' unlock at all.
        await conn.execute("""
            ALTER TABLE discord_welcome_config ADD COLUMN IF NOT EXISTS ultra_pack_unlocked BOOLEAN NOT NULL DEFAULT FALSE
        """)
        await conn.execute("""
            ALTER TABLE discord_welcome_config ADD COLUMN IF NOT EXISTS custom_background_url TEXT
        """)
        # custom_bg_channel_id / custom_bg_message_id: populated only when
        # the background came from an UPLOADED attachment (/welcome
        # custombg's `image` param) rather than a pasted URL. Discord's
        # attachment CDN links are signed and expire (~24h), so a raw URL
        # captured at upload time goes stale. Instead we remember WHERE the
        # image lives — a message in the bot's image-hosting channel (see
        # bot_global_settings' "image_host_channel_id" below) — and
        # re-fetch that message at render time to get a fresh, valid URL.
        # custom_background_url stays populated too (best-effort cache /
        # legacy fallback if the hosting message or channel ever vanishes).
        await conn.execute("""
            ALTER TABLE discord_welcome_config ADD COLUMN IF NOT EXISTS custom_bg_channel_id BIGINT
        """)
        await conn.execute("""
            ALTER TABLE discord_welcome_config ADD COLUMN IF NOT EXISTS custom_bg_message_id BIGINT
        """)

        # --- bot_global_settings (simple key/value store, bot-wide) --------
        # Currently used for "image_host_channel_id": the channel (in the
        # owner's support server) that /welcome custombg re-uploads images
        # to when an admin uploads a file instead of pasting a URL, so that
        # channel doubles as free, permanent-ish image hosting. Set via the
        # owner-only /hostingchannel command (discord_bot/cogs/welcome.py),
        # run directly in the channel that should be used.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_global_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # --- /setup channels (channel-suggestion wizard) -------------------
        # One row per guild (+clone) tracking state for the setup-channels
        # flow: which of the 9 suggested channel names the owner has
        # customized (overriding the default ☑️ prefix), which suggestions
        # were explicitly dismissed (so /setup channels and the join-DM
        # flow stop re-suggesting them), and the auto-created channel IDs
        # for the 5 "soft" channel types (mod-logs, chatroom, music-room,
        # genz-corner, announcements, rules) that have no feature-config
        # table of their own to live in — welcome/bump/leveling's own
        # channel_id + auto_created columns are the source of truth for
        # those 4 instead, this table is not authoritative for them.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_setup_suggestions (
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                category_id BIGINT,
                custom_names JSONB NOT NULL DEFAULT '{}',
                dismissed JSONB NOT NULL DEFAULT '[]',
                soft_channel_ids JSONB NOT NULL DEFAULT '{}',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS discord_setup_suggestions_guild_clone_key
            ON discord_setup_suggestions (guild_id, COALESCE(clone_id, -1))
        """)

        # Quick-start DM: sent once when the bot joins a new guild, listing
        # a handful of setup suggestions (welcome, automod, leveling, etc.)
        # for the server owner. followup_sent_at tracks a SINGLE optional
        # reminder a few days later — same "ask once, then leave them
        # alone" shape as discord_welcome_config's nudge_status above, kept
        # in its own table since this isn't specific to the welcome feature.
        # remind_at/dismissed back the "Remind me later" / "Don't ask
        # again" buttons on the combined owner join DM (bot.py's
        # _send_combined_owner_join_dm): remind_at is a future timestamp a
        # background loop polls for to resend the same DM once, dismissed
        # permanently suppresses it (including that pending resend).
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_quickstart_dm (
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                followup_sent_at TIMESTAMPTZ,
                followup_skipped BOOLEAN NOT NULL DEFAULT FALSE,
                remind_at TIMESTAMPTZ,
                dismissed BOOLEAN NOT NULL DEFAULT FALSE
            )
        """)
        await conn.execute("""
            ALTER TABLE discord_quickstart_dm ADD COLUMN IF NOT EXISTS remind_at TIMESTAMPTZ
        """)
        await conn.execute("""
            ALTER TABLE discord_quickstart_dm ADD COLUMN IF NOT EXISTS dismissed BOOLEAN NOT NULL DEFAULT FALSE
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS discord_quickstart_dm_guild_clone_key
            ON discord_quickstart_dm (guild_id, COALESCE(clone_id, -1))
        """)

        # --- Discord port: economy game (Phase 3, Dank Memer parity) ------------
        # Deliberately its own point pool, NOT shared with discord_xp — see
        # discord-bot-expansion-spec.md's decision log: leveling is an
        # engagement/retention mechanic that should be hard to game, the
        # economy is a gambling-adjacent minigame with its own faucets/sinks,
        # and mixing them means every currency exploit becomes a leveling
        # exploit too. Siloed per (guild_id, clone_id) — same reasoning as
        # discord_premium_groups: a clone owner running the bot across many
        # guilds gets a separate balance/shop per guild, not one shared pool,
        # so a member can't farm currency in one lax guild and spend it in
        # another the same clone also runs.
        #
        # No real-money purchase path anywhere in this schema or the cog that
        # reads it — the owner explicitly rejected pay-to-win. Earn boosts
        # are vote-gated (top.gg-style) or ad-embed-gated, both stored as a
        # timestamp cooldown (last_vote_bonus_at / last_ad_bonus_at) here
        # rather than a currency grant table, since they're just a cooldown
        # override, not a purchase.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_economy_balances (
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                user_id BIGINT NOT NULL,
                balance BIGINT NOT NULL DEFAULT 0,
                last_daily_at TIMESTAMPTZ,
                last_work_at TIMESTAMPTZ,
                last_beg_at TIMESTAMPTZ,
                last_rob_at TIMESTAMPTZ,
                last_vote_bonus_at TIMESTAMPTZ,
                last_ad_bonus_at TIMESTAMPTZ
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS discord_economy_balances_guild_clone_user_key
            ON discord_economy_balances (guild_id, COALESCE(clone_id, -1), user_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_discord_economy_balances_leaderboard
            ON discord_economy_balances (guild_id, clone_id, balance DESC)
        """)

        # Per-guild shop. price is in that guild's currency (see
        # discord_economy_config.currency_name/symbol below), never real
        # money — buy_role_id is optional (a shop item can just grant a
        # cosmetic role) so /shop can double as a cheap alternative to a
        # full premium-role flow for guilds that don't want to touch Paystack.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_economy_shop_items (
                item_id SERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                name TEXT NOT NULL,
                description TEXT,
                price BIGINT NOT NULL,
                role_id BIGINT,
                created_by BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_discord_economy_shop_guild
            ON discord_economy_shop_items (guild_id, clone_id)
        """)

        # Audit trail for every balance change. Deliberately its own table,
        # NOT sharing storage with the real-money payment_logs table — this
        # is fake in-guild currency, not a financial transaction, and
        # comingling the two would make payment_logs unreliable for actual
        # revenue reporting/reconciliation.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_economy_transactions (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                user_id BIGINT NOT NULL,
                amount BIGINT NOT NULL,
                reason TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_discord_economy_transactions_user
            ON discord_economy_transactions (guild_id, clone_id, user_id, created_at DESC)
        """)

        # Per-guild economy config: currency branding + the ad-supported earn
        # knobs from the expansion spec's open question #1. All three
        # interpretations are supported and independently toggleable rather
        # than picking one, since different guild admins will have different
        # top.gg listings / affiliate deals / ad-SDK access:
        #   vote_bonus_enabled + vote_bonus_amount  -> /vote command, grants
        #     a currency bonus once per vote_cooldown_hours (guild admin is
        #     trusted to only enable this once the bot is actually listed
        #     on a voting site; the cog does not verify votes itself here,
        #     see the economy cog's docstring for why).
        #   ad_bonus_enabled + ad_bonus_amount -> /watchad-style command that
        #     shows a sponsored/affiliate embed before granting a bonus.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_economy_config (
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                currency_name TEXT NOT NULL DEFAULT 'Coins',
                currency_symbol TEXT NOT NULL DEFAULT '🪙',
                daily_amount BIGINT NOT NULL DEFAULT 100,
                work_min BIGINT NOT NULL DEFAULT 20,
                work_max BIGINT NOT NULL DEFAULT 80,
                beg_min BIGINT NOT NULL DEFAULT 1,
                beg_max BIGINT NOT NULL DEFAULT 20,
                rob_cooldown_hours INTEGER NOT NULL DEFAULT 6,
                rob_success_chance INTEGER NOT NULL DEFAULT 40,
                vote_bonus_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                vote_bonus_amount BIGINT NOT NULL DEFAULT 200,
                vote_cooldown_hours INTEGER NOT NULL DEFAULT 12,
                vote_url TEXT,
                ad_bonus_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                ad_bonus_amount BIGINT NOT NULL DEFAULT 50,
                ad_cooldown_hours INTEGER NOT NULL DEFAULT 4,
                ad_embed_title TEXT,
                ad_embed_description TEXT,
                ad_embed_url TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS discord_economy_config_guild_clone_key
            ON discord_economy_config (guild_id, COALESCE(clone_id, -1))
        """)
        # wizard_*: same purpose/shape as the other wizards' pointer columns.
        await conn.execute(
            "ALTER TABLE discord_economy_config ADD COLUMN IF NOT EXISTS wizard_channel_id BIGINT"
        )
        await conn.execute(
            "ALTER TABLE discord_economy_config ADD COLUMN IF NOT EXISTS wizard_message_id BIGINT"
        )
        await conn.execute(
            "ALTER TABLE discord_economy_config ADD COLUMN IF NOT EXISTS wizard_invoker_id BIGINT"
        )

        # --- Discord port: automation polish (Phase 4) ---------------------------
        # Auto-responders: simple trigger -> response pairs, checked against
        # every non-bot message (case-insensitive substring match, kept
        # deliberately simple rather than regex to stay predictable for
        # non-technical server admins configuring it via slash command).
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_autoresponders (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                trigger TEXT NOT NULL,
                response TEXT NOT NULL,
                created_by BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_discord_autoresponders_guild
            ON discord_autoresponders (guild_id, clone_id)
        """)

        # Scheduled announcements. Sent via Discord's REST API (see
        # discord_bot/role_grant.py's precedent for why a stateless REST
        # call rather than the gateway) from api/cron_discord_announcements.py,
        # polled by the same style of external cron this repo already uses
        # for api/cron_broadcast.py — one scheduler pattern, not two.
        # interval_minutes NULL means "send once, then mark inactive";
        # a value repeats the post every N minutes.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_scheduled_announcements (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                channel_id BIGINT NOT NULL,
                message TEXT NOT NULL,
                interval_minutes INTEGER,
                next_run_at TIMESTAMPTZ NOT NULL,
                active BOOLEAN NOT NULL DEFAULT TRUE,
                created_by BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_discord_scheduled_announcements_due
            ON discord_scheduled_announcements (active, next_run_at)
        """)

        # Owner broadcasts: a single DM blast from the bot owner (not a
        # per-guild admin) to every user who has ever used the main bot OR
        # any of its clones — e.g. "we shipped X" or "clone tokens must be
        # reset by Friday". Recipients are resolved once at creation time
        # (fan-out into discord_owner_broadcast_recipients) rather than
        # queried live at send time, so the job is a stable, resumable list
        # even if users keep interacting with the bot while it's sending.
        # Sent via api/cron_discord_owner_broadcast.py using the same
        # stateless-REST-per-clone-token approach as
        # api/cron_discord_announcements.py, for the same reason: no single
        # gateway process is guaranteed to be up for every clone.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_owner_broadcasts (
                id SERIAL PRIMARY KEY,
                created_by BIGINT NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                total_recipients INTEGER DEFAULT 0,
                sent_count INTEGER DEFAULT 0,
                failed_count INTEGER DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                completed_at TIMESTAMPTZ
            )
        """)
        # image_url: optional attachment dragged/uploaded onto /ownerbroadcast,
        # sent as an embed image alongside the text (see clone_admin.py and
        # api/cron_discord_owner_broadcast.py). We store Discord's own CDN
        # URL for the attachment rather than re-hosting it ourselves — see
        # the caveat on that URL's lifetime in clone_admin.py's ownerbroadcast
        # command docstring.
        await conn.execute("""
            ALTER TABLE discord_owner_broadcasts ADD COLUMN IF NOT EXISTS image_url TEXT
        """)
        # payment_button_type: optional SELAR_PRODUCT_LINKS key. When set,
        # the cron sender (api/cron_discord_owner_broadcast.py) attaches a
        # "I've Paid" button (_views_direct_paid.py) to every DM in this
        # broadcast, letting a buyer who pays straight off this DM (no
        # /welcome buyultra involved) claim it without typing a server ID —
        # see that module's docstring for how the guild gets resolved.
        await conn.execute("""
            ALTER TABLE discord_owner_broadcasts ADD COLUMN IF NOT EXISTS payment_button_type TEXT
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_owner_broadcast_recipients (
                id SERIAL PRIMARY KEY,
                broadcast_id INTEGER NOT NULL REFERENCES discord_owner_broadcasts(id) ON DELETE CASCADE,
                clone_id INTEGER,
                user_id BIGINT NOT NULL,
                sent BOOLEAN NOT NULL DEFAULT FALSE,
                sent_at TIMESTAMPTZ,
                error TEXT
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_discord_owner_broadcast_recipients_pending
            ON discord_owner_broadcast_recipients (broadcast_id, sent)
        """)
        # claimed_at: lets get_owner_broadcast_recipient_batch atomically
        # "claim" a batch instead of just SELECTing unsent rows — without
        # this, two overlapping cron invocations (e.g. a manual test hit
        # landing while the scheduled tick is still mid-run) both fetch
        # the same unsent rows, both DM the user, and both mark them
        # sent, double-counting sent/failed against a smaller total. A
        # claim older than 2 minutes is treated as abandoned (crashed
        # mid-batch) and becomes claimable again.
        await conn.execute("""
            ALTER TABLE discord_owner_broadcast_recipients ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ
        """)
        # A user can show up in both discord_xp and discord_economy_balances
        # for the same clone; dedupe at read time (see get_discord_bot_user_ids)
        # rather than a UNIQUE constraint here, since two rows for the same
        # (clone_id, user_id) that were queued from different source tables
        # before dedup would otherwise violate it.

        # Dashboard access tokens (spec §4 open question #5: automod config
        # via a real dashboard, extending the existing Next.js app/ site,
        # rather than pure slash-command config). No user-account/OAuth
        # system exists anywhere else in this repo, so rather than bolting
        # one on for a single config page, access is a long random
        # capability token generated by /automod dashboard (must-have-
        # Manage-Server to run it) and embedded in the link the admin is
        # given — same trust model as an unlisted Google Doc link. Anyone
        # who runs /automod dashboard again gets the SAME token back
        # (idempotent), so sharing the link with a co-admin doesn't require
        # regenerating it.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_dashboard_tokens (
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                token TEXT NOT NULL UNIQUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS discord_dashboard_tokens_guild_clone_key
            ON discord_dashboard_tokens (guild_id, COALESCE(clone_id, -1))
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS link_whitelist_domains (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                domain TEXT NOT NULL,
                added_by BIGINT NOT NULL REFERENCES users(user_id),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, domain)
            )
        """)

        # --- Migration: dedicated analytics table (was incorrectly piggy-
        # backing on superbot_user_points, which is the points/leaderboard
        # table — writing arbitrary "action" strings there would have
        # corrupted point totals) -----------------------------------------
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_analytics (
                user_id BIGINT PRIMARY KEY REFERENCES users(user_id),
                ai_uses INTEGER DEFAULT 0,
                downloads INTEGER DEFAULT 0,
                purchases INTEGER DEFAULT 0,
                referrals INTEGER DEFAULT 0,
                total_spent NUMERIC DEFAULT 0,
                total_earned NUMERIC DEFAULT 0,
                last_action DATE
            )
        """)

        # Broadcast Jobs (one-off admin broadcasts, processed in batches by
        # a cron-triggered endpoint since this deployment has no long-running
        # process to loop over recipients in a single request)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS broadcast_jobs (
                id SERIAL PRIMARY KEY,
                admin_id BIGINT NOT NULL REFERENCES users(user_id),
                content TEXT,
                media_file_id TEXT,
                media_type TEXT,
                target_scope TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                total_recipients INTEGER DEFAULT 0,
                sent_count INTEGER DEFAULT 0,
                failed_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            )
        """)

        # --- Migration: per-broadcast "join our group/channel" link -------------
        # Admin-assigned per broadcast (not a fixed bot-wide setting) — set via
        # the /broadcast flow, see handlers/broadcast_handler.py. Rendered as a
        # URL button alongside the fixed-price Premium Group paywall button in
        # api/cron_broadcast.py's _send_broadcast.
        await conn.execute("""
            ALTER TABLE broadcast_jobs ADD COLUMN IF NOT EXISTS join_link TEXT
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS broadcast_recipients (
                id SERIAL PRIMARY KEY,
                job_id INTEGER NOT NULL REFERENCES broadcast_jobs(id),
                chat_id BIGINT NOT NULL,
                kind TEXT NOT NULL,
                sent BOOLEAN DEFAULT FALSE,
                sent_at TIMESTAMP,
                error TEXT
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_broadcast_recipients_job_pending
            ON broadcast_recipients (job_id, sent)
        """)

        # --- Discord port: clone bot registry ---------------------------------
        # Discord equivalent of `cloned_bots`, but with a real, meaningful
        # difference from the Telegram side: a Telegram clone is routed by
        # clone_id over one shared serverless webhook, so registering one is
        # "just a database row". A Discord clone needs its own persistent
        # gateway (WebSocket) connection, so registering one here only marks
        # it eligible to run — discord_bot/clone_manager.py is what actually
        # spawns/supervises a `python -m discord_bot.bot --clone-id N`
        # process per active row. bot_token_encrypted uses the same
        # utils.crypto.secret_manager as clone_service.py's Telegram tokens.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_cloned_bots (
                clone_id SERIAL PRIMARY KEY,
                owner_id BIGINT NOT NULL,
                bot_token_encrypted TEXT NOT NULL,
                bot_user_id BIGINT,
                bot_username TEXT,
                application_id BIGINT,
                status TEXT NOT NULL DEFAULT 'active',
                last_heartbeat TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_discord_cloned_bots_owner
            ON discord_cloned_bots (owner_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_discord_cloned_bots_status
            ON discord_cloned_bots (status)
        """)
        # custom_data mirrors cloned_bots.custom_data's JSON-blob pattern
        # (branding fields, pricing overrides, payment_provider/
        # payment_key_encrypted) — added after the table already existed in
        # some deployments, hence ALTER rather than folding into CREATE TABLE.
        await conn.execute("""
            ALTER TABLE discord_cloned_bots ADD COLUMN IF NOT EXISTS custom_data JSONB DEFAULT '{}'::jsonb
        """)

        # Discord's own monetization-activation table — deliberately NOT the
        # same clone_monetization_subscriptions table Telegram uses: that
        # table's clone_id column is FK'd straight to cloned_bots(clone_id),
        # and discord_cloned_bots has its own independent SERIAL sequence,
        # so a Discord clone_id would either violate that FK or (if the FK
        # were dropped) silently collide with an unrelated Telegram clone's
        # subscription row. A parallel table, FK'd to discord_cloned_bots,
        # is the same pattern already used for discord_premium_groups vs.
        # Telegram's premium groups.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_clone_monetization_subscriptions (
                clone_id INTEGER PRIMARY KEY REFERENCES discord_cloned_bots(clone_id) ON DELETE CASCADE,
                owner_id BIGINT NOT NULL,
                status VARCHAR(20) DEFAULT 'inactive',
                payment_reference VARCHAR(255),
                activated_at TIMESTAMP NULL,
                expires_at TIMESTAMP NULL
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_discord_clone_monetization_status
            ON discord_clone_monetization_subscriptions(status)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_discord_clone_monetization_expires
            ON discord_clone_monetization_subscriptions(expires_at)
        """)

        # --- Discord port: /registerclone payment gate --------------------------
        # Holds a submitted token + validated bot info between "user paid via
        # Paystack" and "webhook confirmed it" (api/paystack_webhook.py's
        # discord_clone case), since a Discord clone can't be inserted into
        # discord_cloned_bots until payment is actually confirmed. A free
        # (no-charge) registration never creates a row here — it goes
        # straight to db.create_discord_clone.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_clone_pending_payments (
                reference TEXT PRIMARY KEY,
                owner_id BIGINT NOT NULL,
                bot_token_encrypted TEXT NOT NULL,
                bot_user_id BIGINT,
                bot_username TEXT,
                application_id BIGINT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_clone_id INTEGER,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_discord_clone_pending_payments_owner
            ON discord_clone_pending_payments (owner_id)
        """)

        # --- Discord port: multiple premium groups per guild -------------------
        # Replaces the old one-row-per-guild `discord_guild_premium` table:
        # a guild (main bot OR a clone) can now define any number of
        # independently-priced paid roles — no ranking/tiering between them,
        # a member can buy any subset. clone_id is NULL for groups created
        # in the main bot; a clone's groups are scoped to that clone_id so
        # two different clones running in the same guild never share pricing.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_premium_groups (
                group_id SERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                clone_id INTEGER REFERENCES discord_cloned_bots(clone_id),
                name TEXT NOT NULL,
                role_id BIGINT NOT NULL,
                fee_ghs NUMERIC NOT NULL,
                active BOOLEAN NOT NULL DEFAULT TRUE,
                created_by BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_discord_premium_groups_guild
            ON discord_premium_groups (guild_id, clone_id)
        """)

        # payment_logs needs to know WHICH premium group a payment was for,
        # now that a single (user, payment_type, chat_id) triple is no
        # longer unique — a guild can have several groups sharing the same
        # payment_type ("premium_group_join") and chat_id (the guild_id).
        await conn.execute("ALTER TABLE payment_logs ADD COLUMN IF NOT EXISTS group_id INTEGER")

        # One-time, idempotent backfill: fold any pre-existing single-tier
        # `discord_guild_premium` row into discord_premium_groups as a
        # "Premium" default group, so guilds configured before this change
        # don't lose their price/role. Safe to run every cold start —
        # WHERE NOT EXISTS makes it a no-op after the first successful run.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_guild_premium (
                guild_id   BIGINT PRIMARY KEY,
                role_id    BIGINT,
                fee_ghs    NUMERIC,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            INSERT INTO discord_premium_groups (guild_id, clone_id, name, role_id, fee_ghs, created_by)
            SELECT g.guild_id, NULL, 'Premium', g.role_id, COALESCE(g.fee_ghs, 20), 0
            FROM discord_guild_premium g
            WHERE g.role_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM discord_premium_groups p
                  WHERE p.guild_id = g.guild_id AND p.clone_id IS NULL AND p.role_id = g.role_id
              )
        """)

        # Generic admin-action audit log (originally shipped alongside
        # discord_guild_premium; moved here so it's auto-provisioned on cold
        # start like every other table instead of requiring a manual SQL run).
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_action_log (
                id              SERIAL PRIMARY KEY,
                admin_id        BIGINT NOT NULL,
                target_user_id  BIGINT NOT NULL,
                action          TEXT NOT NULL,
                chat_id         BIGINT,
                reason          TEXT,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_admin_action_log_target
            ON admin_action_log (target_user_id)
        """)

        # ─────────────────────────────────────────────────────────────
        # AI Store — buyers spend credits (bought via Paystack) chatting
        # with Claude/GPT/Gemini on the PLATFORM'S OWN API keys. Sellers
        # list personas for placement only; no revenue share, no
        # personal-account resale. See modules/ai_store_providers.py.
        # ─────────────────────────────────────────────────────────────

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_store_wallets (
                user_id BIGINT PRIMARY KEY,
                credits NUMERIC NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_store_transactions (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                type TEXT NOT NULL CHECK (type IN ('topup','debit','refund')),
                amount NUMERIC NOT NULL,
                balance_after NUMERIC NOT NULL,
                meta JSONB,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ai_store_tx_user
            ON ai_store_transactions (user_id, created_at)
        """)

        # Sellers list personas, scoped either to one guild (guild_id set,
        # visible only there) or platform-wide (guild_id NULL, visible
        # everywhere) — seller's choice at listing time.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_store_listings (
                id SERIAL PRIMARY KEY,
                seller_id BIGINT NOT NULL,
                guild_id BIGINT,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'general',
                system_prompt TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                placement_tier TEXT NOT NULL DEFAULT 'free' CHECK (placement_tier IN ('free','featured','top')),
                placement_expires_at TIMESTAMPTZ,
                active BOOLEAN NOT NULL DEFAULT TRUE,
                review_status TEXT NOT NULL DEFAULT 'pending' CHECK (review_status IN ('pending','approved','rejected','needs_human')),
                review_reason TEXT,
                uses_count INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ai_store_listings_scope
            ON ai_store_listings (guild_id, active, review_status)
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_store_sessions (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                guild_id BIGINT,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                listing_id INTEGER REFERENCES ai_store_listings(id),
                active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ai_store_sessions_user
            ON ai_store_sessions (user_id, active)
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_store_messages (
                id SERIAL PRIMARY KEY,
                session_id INTEGER NOT NULL REFERENCES ai_store_sessions(id),
                role TEXT NOT NULL CHECK (role IN ('user','assistant')),
                content TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cost_credits NUMERIC NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ai_store_messages_session
            ON ai_store_messages (session_id, created_at)
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_store_refund_requests (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                message_id INTEGER NOT NULL REFERENCES ai_store_messages(id),
                session_id INTEGER NOT NULL,
                amount_credits NUMERIC NOT NULL,
                reason TEXT,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','auto_approved','approved','denied')),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_store_rate_limits (
                user_id BIGINT PRIMARY KEY,
                last_ask_at TIMESTAMPTZ,
                ask_count_window INTEGER NOT NULL DEFAULT 0,
                window_started_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # --- Discord port: autopost (bot self-promo cycling posts) --------------
        # Two tables, deliberately separate:
        #   discord_autopost_config — one row per (guild, clone): the on/off
        #     toggle + schedule. This is the only thing a guild admin
        #     controls ("/autopost setup", "/autopost disable" — a simple
        #     on/off switch, not per-category selection).
        #   discord_autopost_content — a SHARED, bot-wide library of rotating
        #     posts (different function/command highlighted each time,
        #     different copy each time). NOT per-guild data — every guild
        #     that has autopost on cycles through the same content library,
        #     same way a single bot's "tips" rotation works for every server
        #     it's in. Managed by the bot owner (DISCORD_CLONE_ADMIN_IDS),
        #     not per-guild admins — see discord_bot/cogs/autopost.py.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_autopost_config (
                guild_id BIGINT NOT NULL,
                clone_id INTEGER REFERENCES discord_cloned_bots(clone_id),
                channel_id BIGINT NOT NULL,
                interval_hours INTEGER NOT NULL DEFAULT 24,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                current_index INTEGER NOT NULL DEFAULT 0,
                last_posted_at TIMESTAMPTZ,
                configured_by BIGINT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS discord_autopost_config_guild_clone_key
            ON discord_autopost_config (guild_id, COALESCE(clone_id, -1))
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_autopost_content (
                id SERIAL PRIMARY KEY,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                example_command TEXT,
                active BOOLEAN NOT NULL DEFAULT TRUE,
                created_by BIGINT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # Seed starter content once — only if the table is empty, so a bot
        # owner's own edits/deletes are never clobbered on later restarts.
        seed_count = await conn.fetchval("SELECT COUNT(*) FROM discord_autopost_content")
        if seed_count == 0:
            for category, title, body, example in DEFAULT_AUTOPOST_CONTENT:
                await conn.execute("""
                    INSERT INTO discord_autopost_content (category, title, body, example_command)
                    VALUES ($1, $2, $3, $4)
                """, category, title, body, example)
        else:
            # Top-up migration: the table was already seeded by an older
            # deploy (fewer DEFAULT_AUTOPOST_CONTENT rows existed back then).
            # Insert only entries whose exact title isn't already present —
            # this adds newly-introduced feature posts without touching or
            # duplicating anything the owner has since edited/added/removed.
            existing_titles = {
                row["title"] for row in await conn.fetch("SELECT title FROM discord_autopost_content")
            }
            for category, title, body, example in DEFAULT_AUTOPOST_CONTENT:
                if title in existing_titles:
                    continue
                await conn.execute("""
                    INSERT INTO discord_autopost_content (category, title, body, example_command)
                    VALUES ($1, $2, $3, $4)
                """, category, title, body, example)

        # --- Discord port: /feedback -----------------------------------------
        # guild_id is nullable because /feedback works from DMs, where there's
        # no guild context at all.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_user_feedback (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                guild_id BIGINT,
                message TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # --- Bump network ------------------------------------------------
        # One row per guild: the bump channel (used for BOTH directions —
        # a guild's own /bump posts here, and other listings' bumps land
        # here too) plus guild-level filters (language, nsfw opt-in,
        # intensity). Scoped per clone like discord_autopost_config, since
        # each clone is a separate bot instance with its own guild set —
        # cross-clone bump distribution would post into servers the
        # sending clone was never invited to.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bump_guild_config (
                guild_id BIGINT NOT NULL,
                clone_id INTEGER REFERENCES discord_cloned_bots(clone_id),
                bump_channel_id BIGINT,
                receives_bumps BOOLEAN NOT NULL DEFAULT TRUE,
                language TEXT DEFAULT 'any',
                nsfw_opt_in BOOLEAN NOT NULL DEFAULT FALSE,
                intensity_level INTEGER NOT NULL DEFAULT 3,
                is_premium BOOLEAN NOT NULL DEFAULT FALSE,
                configured_by BIGINT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS bump_guild_config_guild_clone_key
            ON bump_guild_config (guild_id, COALESCE(clone_id, -1))
        """)
        # Safety for anyone who already had this table from before
        # receives_bumps existed — CREATE TABLE IF NOT EXISTS above is a
        # no-op on a table that's already there.
        await conn.execute("""
            ALTER TABLE bump_guild_config ADD COLUMN IF NOT EXISTS receives_bumps BOOLEAN NOT NULL DEFAULT TRUE
        """)
        # channel_auto_created: same pattern as discord_welcome_config
        # above — true when /setup channels created #bump itself.
        await conn.execute("""
            ALTER TABLE bump_guild_config ADD COLUMN IF NOT EXISTS channel_auto_created BOOLEAN NOT NULL DEFAULT FALSE
        """)

        # A guild can have more than one listing: its own server ad, plus
        # one per bot it owns (see discord_bot/cogs/bump.py docstring for
        # why bot listings still key off a guild_id rather than existing
        # standalone — they ride the owning guild's bump_channel_id).
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bump_listings (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                clone_id INTEGER REFERENCES discord_cloned_bots(clone_id),
                listing_type TEXT NOT NULL DEFAULT 'server',
                name TEXT,
                description TEXT,
                invite_url TEXT,
                support_url TEXT,
                tags TEXT[] NOT NULL DEFAULT '{}',
                receives_ads BOOLEAN NOT NULL DEFAULT TRUE,
                streak_count INTEGER NOT NULL DEFAULT 0,
                total_bumps INTEGER NOT NULL DEFAULT 0,
                perks TEXT[] NOT NULL DEFAULT '{}',
                rating_sum INTEGER NOT NULL DEFAULT 0,
                rating_count INTEGER NOT NULL DEFAULT 0,
                last_bump_at TIMESTAMPTZ,
                created_by BIGINT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS bump_listings_guild_idx ON bump_listings (guild_id, COALESCE(clone_id, -1))
        """)
        # Safety for anyone who already had this table before the ad-card
        # rework added lifetime counters / perks / rating aggregates.
        for _col_sql in (
            "ALTER TABLE bump_listings ADD COLUMN IF NOT EXISTS total_bumps INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE bump_listings ADD COLUMN IF NOT EXISTS perks TEXT[] NOT NULL DEFAULT '{}'",
            "ALTER TABLE bump_listings ADD COLUMN IF NOT EXISTS rating_sum INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE bump_listings ADD COLUMN IF NOT EXISTS rating_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE bump_listings ADD COLUMN IF NOT EXISTS support_url TEXT",
            # application_id ties a bot listing to the Discord application it
            # was fetched from (RPC), so invite_url can always be regenerated
            # from it server-side rather than trusted from user input.
            # verified_owner_id is the Discord user id that completed OAuth
            # for this submission — set once, never trusted from the slash
            # command itself. status gates a bot listing out of bump sends
            # until a moderator approves it (see bump_review_listing).
            "ALTER TABLE bump_listings ADD COLUMN IF NOT EXISTS application_id BIGINT",
            "ALTER TABLE bump_listings ADD COLUMN IF NOT EXISTS verified_owner_id BIGINT",
            "ALTER TABLE bump_listings ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'approved'",
            # reminder_sent_at: when we last posted a "cooldown's reset, go
            # bump again" nudge for this listing. Compared against
            # last_bump_at (not just a flat timer) so a fresh bump always
            # clears the way for exactly one new reminder next cooldown —
            # see bump_get_listings_needing_reminder.
            "ALTER TABLE bump_listings ADD COLUMN IF NOT EXISTS reminder_sent_at TIMESTAMPTZ",
        ):
            await conn.execute(_col_sql)

        # One-time OAuth handshake state for /bump bot's ownership-verify
        # wizard (see api/bump_oauth.py). Mirrors discover_oauth_states:
        # minted when the wizard's modal is submitted, deleted on first use
        # so a replayed callback can't finalize the same submission twice.
        # Holds the fetched RPC data (name/icon/description) so the OAuth
        # callback never has to re-hit Discord for it, and application_id
        # is what invite_url gets generated from — never from user input.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bump_oauth_states (
                state TEXT PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                clone_id INTEGER,
                invoker_id BIGINT NOT NULL,
                application_id BIGINT NOT NULL,
                bot_name TEXT NOT NULL,
                bot_icon_url TEXT,
                description TEXT,
                tags TEXT[] NOT NULL DEFAULT '{}',
                existing_listing_id INTEGER,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # One row per (listing, rater) — lets a user update their own
        # rating instead of stacking duplicates. bump_listings.rating_sum
        # / rating_count are denormalized aggregates kept in sync by
        # bump_rate_listing() so the ad-card embed can read them without
        # an extra join on every send.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bump_ratings (
                id SERIAL PRIMARY KEY,
                listing_id INTEGER NOT NULL REFERENCES bump_listings(id),
                user_id BIGINT NOT NULL,
                rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS bump_ratings_listing_user_key ON bump_ratings (listing_id, user_id)
        """)

        # Drip-send queue — /bump fills this with one row per target guild
        # instead of posting instantly everywhere (same rate-limit /
        # anti-spam reasoning as autopost's staggered due-check loop).
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bump_queue (
                id SERIAL PRIMARY KEY,
                listing_id INTEGER NOT NULL REFERENCES bump_listings(id),
                target_guild_id BIGINT NOT NULL,
                target_channel_id BIGINT NOT NULL,
                clone_id INTEGER REFERENCES discord_cloned_bots(clone_id),
                scheduled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                sent_at TIMESTAMPTZ
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS bump_queue_pending_idx ON bump_queue (clone_id, sent_at, scheduled_at)
        """)

        # --- Discover Players ------------------------------------------------
        # A category is either guild-scoped (guild_id set, only joinable/
        # visible from that server) or platform-wide (guild_id NULL,
        # joinable from any server or DM) — creator picks at creation time.
        # invite_code is generated once at creation and is stable — a fresh
        # code is only issued if the creator explicitly resets it.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discover_categories (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                guild_id BIGINT,
                created_by BIGINT NOT NULL,
                member_cap INTEGER NOT NULL DEFAULT 15,
                invite_code TEXT UNIQUE NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS discover_categories_name_scope_key
            ON discover_categories (LOWER(name), COALESCE(guild_id, -1))
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discover_memberships (
                id SERIAL PRIMARY KEY,
                category_id INTEGER NOT NULL REFERENCES discover_categories(id) ON DELETE CASCADE,
                user_id BIGINT NOT NULL,
                joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (category_id, user_id)
            )
        """)

        # One row per user, shared across all categories they're in. Contact
        # fields are only ever handed to another user by
        # get_discover_contact_reveal (called on mutual challenge accept) —
        # never exposed by the plain browse/list path.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discover_profiles (
                user_id BIGINT PRIMARY KEY,
                phone TEXT,
                socials JSONB NOT NULL DEFAULT '[]',
                availability TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # Log of cap-upgrade payments per category — mirrors the
        # discord_premium_groups / payment_logs pattern rather than a new
        # payment table shape. status is set to 'paid' by the Paystack
        # webhook (payment_type == 'discover_category_upgrade'); the row is
        # inserted 'pending' when the checkout link is created.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discover_category_payments (
                id SERIAL PRIMARY KEY,
                category_id INTEGER NOT NULL REFERENCES discover_categories(id) ON DELETE CASCADE,
                reference TEXT UNIQUE NOT NULL,
                initiated_by BIGINT NOT NULL,
                cap_from INTEGER NOT NULL,
                cap_to INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # Pending challenge invites between two users in a shared category —
        # accept/decline via button (see discord_bot/cogs/discover_players.py).
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discover_challenges (
                id SERIAL PRIMARY KEY,
                category_id INTEGER NOT NULL REFERENCES discover_categories(id) ON DELETE CASCADE,
                from_user_id BIGINT NOT NULL,
                to_user_id BIGINT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                resolved_at TIMESTAMPTZ
            )
        """)

        # OAuth "click-to-join" state — mirrors gdrive_oauth_states exactly.
        # A state is minted when the invite landing page redirects the
        # visitor to Discord's consent screen, and is single-use (deleted on
        # first lookup) so a replayed callback can't join someone twice off
        # one click.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discover_oauth_states (
                state TEXT PRIMARY KEY,
                category_id INTEGER NOT NULL REFERENCES discover_categories(id) ON DELETE CASCADE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # Shared currency preference — /currency set, reused by any paywall
        # (currently just Discover Players' upgrade tiers; see
        # CURRENCY_CONVERSION_HANDOFF.md for wiring it into the rest).
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_currency_prefs (
                user_id BIGINT PRIMARY KEY,
                currency TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # Seed the 4 starter platform-wide categories once — bot owner
        # account (id 0) as creator since these aren't user-created; only
        # runs if no platform-wide categories exist yet, so it never
        # clobbers a real category a user later renames/removes.
        seed_count = await conn.fetchval(
            "SELECT COUNT(*) FROM discover_categories WHERE guild_id IS NULL"
        )
        if seed_count == 0:
            for cat_name in ("Gamer", "Developer", "FC Mobile Player", "eFootball Player"):
                await conn.execute("""
                    INSERT INTO discover_categories (name, guild_id, created_by, invite_code)
                    VALUES ($1, NULL, 0, $2)
                    ON CONFLICT DO NOTHING
                """, cat_name, secrets.token_urlsafe(6))

        # Heist Wars — schema, constraints, indexes, and seed locations
        # live in database/migrations/001_heist_wars.sql (see game/heist_service.py).
        # Applied here, idempotently (every statement is IF NOT EXISTS /
        # ON CONFLICT), so a cold start provisions it the same way every
        # other table above is provisioned — no separate manual migration
        # step required for this table set.
        import pathlib
        heist_migration = pathlib.Path(__file__).parent / "database" / "migrations" / "001_heist_wars.sql"
        if heist_migration.exists():
            await conn.execute(heist_migration.read_text())

        # Heist Wars — items/inventory/loadout expansion (additive-only,
        # same idempotent IF NOT EXISTS / ON CONFLICT pattern as 001 above).
        heist_items_migration = pathlib.Path(__file__).parent / "database" / "migrations" / "002_heist_items.sql"
        if heist_items_migration.exists():
            await conn.execute(heist_items_migration.read_text())

        # Inter-server roast arena — per-guild opt-in, challenges, and votes
        # (see discord_bot/cogs/roast_arena.py). Same additive-only, idempotent
        # migration-file pattern as 001/002 above; separate from the
        # single-server discord_roast_* tables.
        roast_arena_migration = pathlib.Path(__file__).parent / "database" / "migrations" / "003_roast_arena.sql"
        if roast_arena_migration.exists():
            await conn.execute(roast_arena_migration.read_text())

        # Roast arena — single shared battleground + apply-to-host flow,
        # additive on top of 003 above (see
        # discord_bot/cogs/_views_roast_arena_host_wizard.py).
        roast_arena_host_migration = pathlib.Path(__file__).parent / "database" / "migrations" / "004_roast_arena_host.sql"
        if roast_arena_host_migration.exists():
            await conn.execute(roast_arena_host_migration.read_text())

        # Roast arena — cross-clone outbox relay, additive on top of 003/004
        # above (see discord_bot/cogs/roast_arena.py _drain_arena_actions).
        # Lets a challenge/decline/event-invite action be executed by
        # whichever clone process actually holds the target guild, instead of
        # requiring the acting process to have that guild in its own cache.
        roast_arena_outbox_migration = pathlib.Path(__file__).parent / "database" / "migrations" / "005_roast_arena_outbox.sql"
        if roast_arena_outbox_migration.exists():
            await conn.execute(roast_arena_outbox_migration.read_text())

        # Roast arena — mirrored panel columns so the live vote panel can be
        # posted in BOTH contesting guilds, not just the single shared
        # battleground (see discord_bot/cogs/roast_arena.py on_member_accept).
        roast_arena_mirror_migration = pathlib.Path(__file__).parent / "database" / "migrations" / "006_roast_arena_mirror_panels.sql"
        if roast_arena_mirror_migration.exists():
            await conn.execute(roast_arena_mirror_migration.read_text())

        # --- Trading cards (cross-server marketplace) --------------------------
        # Deliberately GLOBAL (no guild_id anywhere here) — the whole point
        # is a user can pull a card in Server A and sell it to someone in
        # Server B, same as discord_cloned_bots' user-level tables. Still
        # scoped by clone_id so a clone's card economy/catalog never mixes
        # with the main bot's or another clone's, matching every other
        # per-bot-instance table in this schema.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS card_catalog (
                card_id SERIAL PRIMARY KEY,
                clone_id INTEGER,
                name TEXT NOT NULL,
                rarity TEXT NOT NULL DEFAULT 'common',
                emoji TEXT,
                image_url TEXT,
                base_value INTEGER NOT NULL DEFAULT 10,
                active BOOLEAN NOT NULL DEFAULT TRUE
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_card_catalog_clone_active
            ON card_catalog (COALESCE(clone_id, -1), active)
        """)

        # A user can own multiple copies of the same card_id — quantity
        # column rather than one row per physical copy, since copies of the
        # same card are fungible (no per-copy state like a serial number).
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_cards (
                user_id BIGINT NOT NULL,
                clone_id INTEGER,
                card_id INTEGER NOT NULL REFERENCES card_catalog(card_id),
                quantity INTEGER NOT NULL DEFAULT 0
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS user_cards_user_clone_card_key
            ON user_cards (user_id, COALESCE(clone_id, -1), card_id)
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS card_coins (
                user_id BIGINT NOT NULL,
                clone_id INTEGER,
                balance BIGINT NOT NULL DEFAULT 0,
                last_daily_at TIMESTAMPTZ,
                started_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS card_coins_user_clone_key
            ON card_coins (user_id, COALESCE(clone_id, -1))
        """)

        # Marketplace listings. status: active / sold / cancelled — kept
        # (not deleted) on sale/cancel so /mylistings and future "sales
        # history" have something to read; the swipe browser only ever
        # queries status='active'.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS card_listings (
                listing_id SERIAL PRIMARY KEY,
                clone_id INTEGER,
                seller_id BIGINT NOT NULL,
                card_id INTEGER NOT NULL REFERENCES card_catalog(card_id),
                price INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                buyer_id BIGINT,
                listed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                resolved_at TIMESTAMPTZ
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_card_listings_active
            ON card_listings (COALESCE(clone_id, -1), status, listed_at DESC)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_card_listings_seller
            ON card_listings (seller_id, COALESCE(clone_id, -1), status)
        """)

        # Wishlist alerts — notified (via DM, see cards.py) the next time a
        # matching card_id gets a new active listing. One row per
        # (user, card); re-running /card watch on the same card is a no-op.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS card_watches (
                user_id BIGINT NOT NULL,
                clone_id INTEGER,
                card_id INTEGER NOT NULL REFERENCES card_catalog(card_id),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS card_watches_user_clone_card_key
            ON card_watches (user_id, COALESCE(clone_id, -1), card_id)
        """)

        # Seed a starter catalog once per clone (clone_id IS NULL = main
        # bot) — only if that clone has no cards yet, so it never clobbers
        # a catalog the owner has since edited/expanded.
        starter_count = await conn.fetchval(
            "SELECT COUNT(*) FROM card_catalog WHERE clone_id IS NULL"
        )
        if starter_count == 0:
            starter_cards = [
                ("Ember Sprite", "common", "🔥", 10),
                ("Tide Pup", "common", "💧", 10),
                ("Leaf Whelp", "common", "🌿", 10),
                ("Spark Mouse", "common", "⚡", 10),
                ("Stone Golemite", "rare", "🪨", 40),
                ("Frost Fang", "rare", "❄️", 40),
                ("Shadow Kit", "rare", "🌑", 40),
                ("Gale Hawk", "epic", "🌪️", 120),
                ("Crimson Drake", "epic", "🐉", 120),
                ("Celestial Fox", "legendary", "✨", 400),
                ("Void Serpent", "legendary", "🐍", 400),
            ]
            for name, rarity, emoji, value in starter_cards:
                await conn.execute("""
                    INSERT INTO card_catalog (clone_id, name, rarity, emoji, base_value)
                    VALUES (NULL, $1, $2, $3, $4)
                """, name, rarity, emoji, value)

    # ─────────────────────────────────────────────────────────────────
    # Trading cards (cross-server marketplace)
    # ─────────────────────────────────────────────────────────────────

    RARITY_WEIGHTS = {"common": 60, "rare": 27, "epic": 10, "legendary": 3}

    async def get_card_catalog(self, clone_id: Optional[int] = None) -> List[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM card_catalog
                WHERE COALESCE(clone_id, -1) = COALESCE($1, -1) AND active = TRUE
                ORDER BY base_value ASC
            """, clone_id)
            return [dict(r) for r in rows]

    async def get_card_by_name(self, name: str, clone_id: Optional[int] = None) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM card_catalog
                WHERE COALESCE(clone_id, -1) = COALESCE($1, -1) AND active = TRUE
                AND LOWER(name) = LOWER($2)
            """, clone_id, name)
            return dict(row) if row else None

    async def roll_random_card(self, clone_id: Optional[int] = None) -> Optional[Dict]:
        """Rarity-weighted random pull from the catalog, using RARITY_WEIGHTS.
        Falls back to a plain random row if no card of the rolled rarity
        exists (e.g. a clone owner deletes all Legendary cards)."""
        import random
        catalog = await self.get_card_catalog(clone_id)
        if not catalog:
            return None
        rarities = list(self.RARITY_WEIGHTS.keys())
        weights = list(self.RARITY_WEIGHTS.values())
        chosen_rarity = random.choices(rarities, weights=weights, k=1)[0]
        pool_for_rarity = [c for c in catalog if c["rarity"] == chosen_rarity]
        if not pool_for_rarity:
            pool_for_rarity = catalog
        return random.choice(pool_for_rarity)

    async def grant_card(self, user_id: int, card_id: int, clone_id: Optional[int] = None, quantity: int = 1) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO user_cards (user_id, clone_id, card_id, quantity)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (user_id, COALESCE(clone_id, -1), card_id) DO UPDATE
                SET quantity = user_cards.quantity + EXCLUDED.quantity
            """, user_id, clone_id, card_id, quantity)

    async def take_card(self, user_id: int, card_id: int, clone_id: Optional[int] = None, quantity: int = 1) -> bool:
        """Removes `quantity` copies if the user has enough; returns False
        (no-op) otherwise. Deletes the row once it hits 0 rather than
        leaving a 0-quantity row around."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("""
                    SELECT quantity FROM user_cards
                    WHERE user_id = $1 AND COALESCE(clone_id, -1) = COALESCE($2, -1) AND card_id = $3
                    FOR UPDATE
                """, user_id, clone_id, card_id)
                if not row or row["quantity"] < quantity:
                    return False
                if row["quantity"] == quantity:
                    await conn.execute("""
                        DELETE FROM user_cards
                        WHERE user_id = $1 AND COALESCE(clone_id, -1) = COALESCE($2, -1) AND card_id = $3
                    """, user_id, clone_id, card_id)
                else:
                    await conn.execute("""
                        UPDATE user_cards SET quantity = quantity - $4
                        WHERE user_id = $1 AND COALESCE(clone_id, -1) = COALESCE($2, -1) AND card_id = $3
                    """, user_id, clone_id, card_id, quantity)
                return True

    async def get_user_cards(self, user_id: int, clone_id: Optional[int] = None) -> List[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT uc.card_id, uc.quantity, c.name, c.rarity, c.emoji, c.base_value
                FROM user_cards uc
                JOIN card_catalog c ON c.card_id = uc.card_id
                WHERE uc.user_id = $1 AND COALESCE(uc.clone_id, -1) = COALESCE($2, -1) AND uc.quantity > 0
                ORDER BY c.base_value DESC
            """, user_id, clone_id)
            return [dict(r) for r in rows]

    async def has_starter_pack(self, user_id: int, clone_id: Optional[int] = None) -> bool:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchval("""
                SELECT 1 FROM user_cards
                WHERE user_id = $1 AND COALESCE(clone_id, -1) = COALESCE($2, -1) LIMIT 1
            """, user_id, clone_id)
            return row is not None

    # --- Card Coins (global currency) ---

    async def get_card_coins(self, user_id: int, clone_id: Optional[int] = None) -> int:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT balance FROM card_coins
                WHERE user_id = $1 AND COALESCE(clone_id, -1) = COALESCE($2, -1)
            """, user_id, clone_id)
            return row["balance"] if row else 0

    async def _ensure_card_coins_row(self, conn, user_id: int, clone_id: Optional[int]) -> None:
        await conn.execute("""
            INSERT INTO card_coins (user_id, clone_id, balance)
            VALUES ($1, $2, 0)
            ON CONFLICT (user_id, COALESCE(clone_id, -1)) DO NOTHING
        """, user_id, clone_id)

    async def add_card_coins(self, user_id: int, amount: int, clone_id: Optional[int] = None) -> int:
        """amount may be negative (a spend) — caller must have already
        confirmed sufficient balance via get_card_coins/try_spend_card_coins;
        this does not itself block going negative."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await self._ensure_card_coins_row(conn, user_id, clone_id)
            row = await conn.fetchrow("""
                UPDATE card_coins SET balance = balance + $3
                WHERE user_id = $1 AND COALESCE(clone_id, -1) = COALESCE($2, -1)
                RETURNING balance
            """, user_id, clone_id, amount)
            return row["balance"]

    async def try_spend_card_coins(self, user_id: int, amount: int, clone_id: Optional[int] = None) -> bool:
        """Atomically deducts `amount` only if the balance covers it.
        Returns False (no deduction) if insufficient — prevents a
        race where two nearly-simultaneous purchases both read a
        sufficient balance before either deducts."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await self._ensure_card_coins_row(conn, user_id, clone_id)
            async with conn.transaction():
                row = await conn.fetchrow("""
                    SELECT balance FROM card_coins
                    WHERE user_id = $1 AND COALESCE(clone_id, -1) = COALESCE($2, -1)
                    FOR UPDATE
                """, user_id, clone_id)
                if not row or row["balance"] < amount:
                    return False
                await conn.execute("""
                    UPDATE card_coins SET balance = balance - $3
                    WHERE user_id = $1 AND COALESCE(clone_id, -1) = COALESCE($2, -1)
                """, user_id, clone_id, amount)
                return True

    async def get_card_daily_cooldown(self, user_id: int, clone_id: Optional[int] = None) -> Optional[datetime]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval("""
                SELECT last_daily_at FROM card_coins
                WHERE user_id = $1 AND COALESCE(clone_id, -1) = COALESCE($2, -1)
            """, user_id, clone_id)

    async def set_card_daily_claimed(self, user_id: int, clone_id: Optional[int] = None) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await self._ensure_card_coins_row(conn, user_id, clone_id)
            await conn.execute("""
                UPDATE card_coins SET last_daily_at = NOW()
                WHERE user_id = $1 AND COALESCE(clone_id, -1) = COALESCE($2, -1)
            """, user_id, clone_id)

    # --- Marketplace ---

    async def create_card_listing(self, seller_id: int, card_id: int, price: int,
                                   clone_id: Optional[int] = None) -> int:
        pool = await get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval("""
                INSERT INTO card_listings (clone_id, seller_id, card_id, price, status)
                VALUES ($1, $2, $3, $4, 'active')
                RETURNING listing_id
            """, clone_id, seller_id, card_id, price)

    async def get_active_listings(self, clone_id: Optional[int] = None, rarity: Optional[str] = None,
                                   search: Optional[str] = None) -> List[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            query = """
                SELECT l.listing_id, l.seller_id, l.price, l.listed_at,
                       c.card_id, c.name, c.rarity, c.emoji, c.image_url, c.base_value
                FROM card_listings l
                JOIN card_catalog c ON c.card_id = l.card_id
                WHERE COALESCE(l.clone_id, -1) = COALESCE($1, -1) AND l.status = 'active'
            """
            params = [clone_id]
            if rarity:
                params.append(rarity)
                query += f" AND c.rarity = ${len(params)}"
            if search:
                params.append(f"%{search}%")
                query += f" AND c.name ILIKE ${len(params)}"
            query += " ORDER BY l.listed_at DESC"
            rows = await conn.fetch(query, *params)
            return [dict(r) for r in rows]

    async def get_listing(self, listing_id: int) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT l.*, c.name, c.rarity, c.emoji, c.image_url, c.base_value
                FROM card_listings l JOIN card_catalog c ON c.card_id = l.card_id
                WHERE l.listing_id = $1
            """, listing_id)
            return dict(row) if row else None

    async def get_user_listings(self, seller_id: int, clone_id: Optional[int] = None) -> List[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT l.listing_id, l.price, l.status, l.listed_at, c.name, c.rarity, c.emoji
                FROM card_listings l JOIN card_catalog c ON c.card_id = l.card_id
                WHERE l.seller_id = $1 AND COALESCE(l.clone_id, -1) = COALESCE($2, -1) AND l.status = 'active'
                ORDER BY l.listed_at DESC
            """, seller_id, clone_id)
            return [dict(r) for r in rows]

    async def cancel_card_listing(self, listing_id: int, requester_id: int) -> bool:
        """Only cancellable by its own seller. Returns the card to the
        seller's inventory and marks the listing cancelled — atomic so a
        crash mid-op can't lose the card."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("""
                    SELECT * FROM card_listings WHERE listing_id = $1 AND status = 'active' FOR UPDATE
                """, listing_id)
                if not row or row["seller_id"] != requester_id:
                    return False
                await conn.execute("""
                    UPDATE card_listings SET status = 'cancelled', resolved_at = NOW() WHERE listing_id = $1
                """, listing_id)
                await conn.execute("""
                    INSERT INTO user_cards (user_id, clone_id, card_id, quantity)
                    VALUES ($1, $2, $3, 1)
                    ON CONFLICT (user_id, COALESCE(clone_id, -1), card_id) DO UPDATE
                    SET quantity = user_cards.quantity + 1
                """, row["seller_id"], row["clone_id"], row["card_id"])
                return True

    async def buy_card_listing(self, listing_id: int, buyer_id: int) -> Optional[Dict]:
        """Atomically: checks the listing is still active, checks the buyer
        isn't the seller, deducts buyer's coins, credits seller's coins,
        marks the listing sold, grants the card to the buyer. Returns the
        resolved listing dict on success (for the sale-alert DM), or None
        if the buy failed (already sold/cancelled, buyer is seller, or
        insufficient coins) — all within one row-locked transaction so two
        buyers racing the same listing can't both succeed."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                listing = await conn.fetchrow("""
                    SELECT * FROM card_listings WHERE listing_id = $1 AND status = 'active' FOR UPDATE
                """, listing_id)
                if not listing:
                    return None
                if listing["seller_id"] == buyer_id:
                    return None

                await self._ensure_card_coins_row(conn, buyer_id, listing["clone_id"])
                buyer_row = await conn.fetchrow("""
                    SELECT balance FROM card_coins
                    WHERE user_id = $1 AND COALESCE(clone_id, -1) = COALESCE($2, -1) FOR UPDATE
                """, buyer_id, listing["clone_id"])
                if not buyer_row or buyer_row["balance"] < listing["price"]:
                    return None

                await conn.execute("""
                    UPDATE card_coins SET balance = balance - $3
                    WHERE user_id = $1 AND COALESCE(clone_id, -1) = COALESCE($2, -1)
                """, buyer_id, listing["clone_id"], listing["price"])

                await self._ensure_card_coins_row(conn, listing["seller_id"], listing["clone_id"])
                await conn.execute("""
                    UPDATE card_coins SET balance = balance + $3
                    WHERE user_id = $1 AND COALESCE(clone_id, -1) = COALESCE($2, -1)
                """, listing["seller_id"], listing["clone_id"], listing["price"])

                await conn.execute("""
                    UPDATE card_listings SET status = 'sold', buyer_id = $2, resolved_at = NOW()
                    WHERE listing_id = $1
                """, listing_id, buyer_id)

                await conn.execute("""
                    INSERT INTO user_cards (user_id, clone_id, card_id, quantity)
                    VALUES ($1, $2, $3, 1)
                    ON CONFLICT (user_id, COALESCE(clone_id, -1), card_id) DO UPDATE
                    SET quantity = user_cards.quantity + 1
                """, buyer_id, listing["clone_id"], listing["card_id"])

                card = await conn.fetchrow("SELECT * FROM card_catalog WHERE card_id = $1", listing["card_id"])
                return {**dict(listing), **{"card_name": card["name"], "card_emoji": card["emoji"]}}

    # --- Wishlist alerts ---

    async def add_card_watch(self, user_id: int, card_id: int, clone_id: Optional[int] = None) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO card_watches (user_id, clone_id, card_id) VALUES ($1, $2, $3)
                ON CONFLICT (user_id, COALESCE(clone_id, -1), card_id) DO NOTHING
            """, user_id, clone_id, card_id)

    async def get_watchers_for_card(self, card_id: int, clone_id: Optional[int] = None) -> List[int]:
        """Returns watcher user_ids and clears their watch (one-shot alert,
        not a recurring subscription) so the same listing doesn't re-notify
        them on every future listing of the same card."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch("""
                    SELECT user_id FROM card_watches
                    WHERE card_id = $1 AND COALESCE(clone_id, -1) = COALESCE($2, -1) FOR UPDATE
                """, card_id, clone_id)
                user_ids = [r["user_id"] for r in rows]
                if user_ids:
                    await conn.execute("""
                        DELETE FROM card_watches
                        WHERE card_id = $1 AND COALESCE(clone_id, -1) = COALESCE($2, -1)
                    """, card_id, clone_id)
                return user_ids

    # ─────────────────────────────────────────────────────────────────
    # Autopost (recurring posts)
    # ─────────────────────────────────────────────────────────────────

    async def create_recurring_post(self, chat_id: int, admin_id: int, interval_minutes: int,
                                     content: Optional[str] = None, media_file_id: Optional[str] = None,
                                     media_type: Optional[str] = None) -> int:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO recurring_posts (chat_id, admin_id, content, interval_minutes, media_file_id, media_type, is_active)
                VALUES ($1, $2, $3, $4, $5, $6, TRUE)
                RETURNING id
            """, chat_id, admin_id, content, interval_minutes, media_file_id, media_type)
            return row["id"]

    async def get_due_recurring_posts(self, limit: int = 20) -> List[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM recurring_posts
                WHERE is_active = TRUE
                AND (
                    last_posted IS NULL
                    OR NOW() - last_posted >= (COALESCE(interval_minutes, interval_hours * 60) || ' minutes')::interval
                )
                ORDER BY last_posted NULLS FIRST
                LIMIT $1
            """, limit)
            return [dict(r) for r in rows]

    async def mark_recurring_posted(self, post_id: int):
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("UPDATE recurring_posts SET last_posted = NOW() WHERE id = $1", post_id)

    async def bump_recurring_failure(self, post_id: int, max_failures: int = 5) -> int:
        """Increment failure_count; auto-deactivate after max_failures (e.g. bot was removed/kicked)."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                UPDATE recurring_posts SET failure_count = failure_count + 1
                WHERE id = $1
                RETURNING failure_count
            """, post_id)
            new_count = row["failure_count"] if row else 0
            if new_count >= max_failures:
                await conn.execute("UPDATE recurring_posts SET is_active = FALSE WHERE id = $1", post_id)
            return new_count

    # ─────────────────────────────────────────────────────────────────
    # Discord port: autopost (bot self-promo cycling posts)
    # ─────────────────────────────────────────────────────────────────

    async def set_discord_autopost(self, guild_id: int, clone_id: Optional[int], channel_id: int,
                                    interval_hours: int, configured_by: int) -> None:
        """Upsert = the on/off + schedule toggle for one guild. Re-running
        /autopost setup on an already-configured guild just updates the
        channel/interval and re-enables it, current_index/last_posted_at are
        left alone so it doesn't skip back to the start of the rotation."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO discord_autopost_config
                    (guild_id, clone_id, channel_id, interval_hours, enabled, configured_by, updated_at)
                VALUES ($1, $2, $3, $4, TRUE, $5, NOW())
                ON CONFLICT (guild_id, COALESCE(clone_id, -1)) DO UPDATE SET
                    channel_id = EXCLUDED.channel_id,
                    interval_hours = EXCLUDED.interval_hours,
                    enabled = TRUE,
                    configured_by = EXCLUDED.configured_by,
                    updated_at = NOW()
            """, guild_id, clone_id, channel_id, interval_hours, configured_by)

    async def disable_discord_autopost(self, guild_id: int, clone_id: Optional[int]) -> bool:
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE discord_autopost_config SET enabled = FALSE, updated_at = NOW()
                WHERE guild_id = $1 AND COALESCE(clone_id, -1) = COALESCE($2, -1)
            """, guild_id, clone_id)
            return result.split()[-1] != "0"

    async def get_discord_autopost(self, guild_id: int, clone_id: Optional[int]) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM discord_autopost_config
                WHERE guild_id = $1 AND COALESCE(clone_id, -1) = COALESCE($2, -1)
            """, guild_id, clone_id)
            return dict(row) if row else None

    async def get_due_discord_autoposts(self, clone_id: Optional[int], limit: int = 25) -> List[Dict]:
        """Guilds (scoped to this process's clone_id — None = main bot) whose
        interval has elapsed. NULL last_posted_at (never posted) counts as
        due immediately."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM discord_autopost_config
                WHERE enabled = TRUE
                AND COALESCE(clone_id, -1) = COALESCE($1, -1)
                AND (
                    last_posted_at IS NULL
                    OR NOW() - last_posted_at >= (interval_hours || ' hours')::interval
                )
                ORDER BY last_posted_at NULLS FIRST
                LIMIT $2
            """, clone_id, limit)
            return [dict(r) for r in rows]

    async def advance_discord_autopost(self, guild_id: int, clone_id: Optional[int], new_index: int) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE discord_autopost_config
                SET current_index = $3, last_posted_at = NOW()
                WHERE guild_id = $1 AND COALESCE(clone_id, -1) = COALESCE($2, -1)
            """, guild_id, clone_id, new_index)

    # ─────────────────────────────────────────────────────────────────
    # Discord port: /feedback
    # ─────────────────────────────────────────────────────────────────

    async def add_discord_user_feedback(self, user_id: int, guild_id: Optional[int], message: str) -> int:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO discord_user_feedback (user_id, guild_id, message)
                VALUES ($1, $2, $3)
                RETURNING id
            """, user_id, guild_id, message)
            return row["id"]

    async def get_discord_user_feedback(self, limit: int = 50) -> List[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM discord_user_feedback
                ORDER BY created_at DESC
                LIMIT $1
            """, limit)
            return [dict(r) for r in rows]

    # ─────────────────────────────────────────────────────────────────
    # Discover Players
    # ─────────────────────────────────────────────────────────────────

    async def create_discover_category(self, name: str, guild_id: Optional[int],
                                         created_by: int, invite_code: str) -> Optional[Dict]:
        """Returns the new row, or None if a category with this name already
        exists in this scope (guild_id, or platform-wide when None) —
        checked via the case-insensitive unique index rather than a
        SELECT-then-INSERT race."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            try:
                row = await conn.fetchrow("""
                    INSERT INTO discover_categories (name, guild_id, created_by, invite_code)
                    VALUES ($1, $2, $3, $4)
                    RETURNING *
                """, name, guild_id, created_by, invite_code)
                return dict(row)
            except asyncpg.UniqueViolationError:
                return None

    async def get_discover_category(self, category_id: int) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM discover_categories WHERE id = $1", category_id)
            return dict(row) if row else None

    async def get_discover_category_by_code(self, invite_code: str) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM discover_categories WHERE invite_code = $1", invite_code
            )
            return dict(row) if row else None

    async def find_discover_category(self, name: str, guild_id: Optional[int]) -> Optional[Dict]:
        """Look up by name within a scope: first the guild-local category (if
        guild_id given), falling back to a platform-wide one of the same
        name — so /discover join works the same whether the category the
        user means is this server's or platform-wide."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            if guild_id is not None:
                row = await conn.fetchrow("""
                    SELECT * FROM discover_categories
                    WHERE LOWER(name) = LOWER($1) AND guild_id = $2
                """, name, guild_id)
                if row:
                    return dict(row)
            row = await conn.fetchrow("""
                SELECT * FROM discover_categories
                WHERE LOWER(name) = LOWER($1) AND guild_id IS NULL
            """, name)
            return dict(row) if row else None

    async def list_discover_categories(self, guild_id: Optional[int]) -> List[Dict]:
        """Platform-wide categories plus this guild's own, for the browse
        list. When guild_id is None (DM context) only platform-wide ones
        show, since a guild-scoped category isn't joinable from outside
        that server."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT c.*, COUNT(m.id) AS member_count
                FROM discover_categories c
                LEFT JOIN discover_memberships m ON m.category_id = c.id
                WHERE c.guild_id IS NULL OR c.guild_id = $1
                GROUP BY c.id
                ORDER BY c.name
            """, guild_id)
            return [dict(r) for r in rows]

    async def get_discover_member_count(self, category_id: int) -> int:
        pool = await get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM discover_memberships WHERE category_id = $1", category_id
            )

    async def join_discover_category(self, category_id: int, user_id: int) -> str:
        """Returns 'joined', 'already_member', or 'full'. Cap check and
        insert happen in one transaction so two joins racing right at the
        cap can't both slip in."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                cat = await conn.fetchrow(
                    "SELECT member_cap FROM discover_categories WHERE id = $1 FOR UPDATE", category_id
                )
                if cat is None:
                    return "not_found"
                existing = await conn.fetchval(
                    "SELECT 1 FROM discover_memberships WHERE category_id = $1 AND user_id = $2",
                    category_id, user_id,
                )
                if existing:
                    return "already_member"
                count = await conn.fetchval(
                    "SELECT COUNT(*) FROM discover_memberships WHERE category_id = $1", category_id
                )
                if count >= cat["member_cap"]:
                    return "full"
                await conn.execute(
                    "INSERT INTO discover_memberships (category_id, user_id) VALUES ($1, $2)",
                    category_id, user_id,
                )
                return "joined"

    async def list_discover_category_members(self, category_id: int, limit: int = 25,
                                               offset: int = 0) -> List[int]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT user_id FROM discover_memberships
                WHERE category_id = $1
                ORDER BY joined_at
                LIMIT $2 OFFSET $3
            """, category_id, limit, offset)
            return [r["user_id"] for r in rows]

    async def set_discover_profile(self, user_id: int, phone: Optional[str] = None,
                                    socials: Optional[list] = None,
                                    availability: Optional[str] = None) -> None:
        """Partial update — a field left as None keeps its current stored
        value rather than being wiped, so /discover profile can be re-run to
        change just one field."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO discover_profiles (user_id, phone, socials, availability, updated_at)
                VALUES ($1, $2, COALESCE($3, '[]'::jsonb), $4, NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                    phone = COALESCE($2, discover_profiles.phone),
                    socials = COALESCE($3, discover_profiles.socials),
                    availability = COALESCE($4, discover_profiles.availability),
                    updated_at = NOW()
            """, user_id, phone, json.dumps(socials) if socials is not None else None, availability)

    async def get_discover_profile(self, user_id: int) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM discover_profiles WHERE user_id = $1", user_id)
            if not row:
                return None
            d = dict(row)
            d["socials"] = json.loads(d["socials"]) if isinstance(d["socials"], str) else d["socials"]
            return d

    async def raise_discover_category_cap(self, category_id: int, new_cap: int) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE discover_categories SET member_cap = $2 WHERE id = $1", category_id, new_cap
            )

    async def create_discover_category_payment(self, category_id: int, reference: str,
                                                 initiated_by: int, cap_from: int, cap_to: int) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO discover_category_payments (category_id, reference, initiated_by, cap_from, cap_to)
                VALUES ($1, $2, $3, $4, $5)
            """, category_id, reference, initiated_by, cap_from, cap_to)

    async def mark_discover_category_payment_paid(self, reference: str) -> Optional[Dict]:
        """Marks the payment row paid and raises the category's cap in one
        transaction; returns the payment row (with category_id/cap_to) so
        the webhook can notify the payer, or None if the reference is
        unknown or was already processed."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("""
                    UPDATE discover_category_payments SET status = 'paid'
                    WHERE reference = $1 AND status = 'pending'
                    RETURNING *
                """, reference)
                if not row:
                    return None
                await conn.execute(
                    "UPDATE discover_categories SET member_cap = $2 WHERE id = $1",
                    row["category_id"], row["cap_to"],
                )
                return dict(row)

    async def create_discover_challenge(self, category_id: int, from_user_id: int, to_user_id: int) -> int:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO discover_challenges (category_id, from_user_id, to_user_id)
                VALUES ($1, $2, $3)
                RETURNING id
            """, category_id, from_user_id, to_user_id)
            return row["id"]

    async def resolve_discover_challenge(self, challenge_id: int, status: str) -> Optional[Dict]:
        """status is 'accepted' or 'declined'. Returns the row (so the
        caller knows both user ids) or None if already resolved."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                UPDATE discover_challenges SET status = $2, resolved_at = NOW()
                WHERE id = $1 AND status = 'pending'
                RETURNING *
            """, challenge_id, status)
            return dict(row) if row else None

    async def get_discover_contact_reveal(self, user_id: int) -> Optional[Dict]:
        """Only called after a mutual challenge accept — see
        discord_bot/cogs/discover_players.py. Never call this from a plain
        browse/list path."""
        return await self.get_discover_profile(user_id)

    async def create_discover_oauth_state(self, state: str, category_id: int) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO discover_oauth_states (state, category_id) VALUES ($1, $2)", state, category_id
            )

    async def pop_discover_oauth_state(self, state: str) -> Optional[int]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "DELETE FROM discover_oauth_states WHERE state = $1 RETURNING category_id", state
            )
            return row["category_id"] if row else None

    # ─────────────────────────────────────────────────────────────────
    # Currency preference (shared — usable by any paywall, not just
    # Discover Players; see utils/currency.py)
    # ─────────────────────────────────────────────────────────────────

    async def set_user_currency(self, user_id: int, currency: str) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO user_currency_prefs (user_id, currency)
                VALUES ($1, $2)
                ON CONFLICT (user_id) DO UPDATE SET currency = $2, updated_at = NOW()
            """, user_id, currency.upper())

    async def get_user_currency(self, user_id: int) -> Optional[str]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval("SELECT currency FROM user_currency_prefs WHERE user_id = $1", user_id)

    async def list_discord_autopost_content(self, active_only: bool = True) -> List[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            if active_only:
                rows = await conn.fetch(
                    "SELECT * FROM discord_autopost_content WHERE active = TRUE ORDER BY id"
                )
            else:
                rows = await conn.fetch("SELECT * FROM discord_autopost_content ORDER BY id")
            return [dict(r) for r in rows]

    async def add_discord_autopost_content(self, category: str, title: str, body: str,
                                            example_command: Optional[str], created_by: int) -> int:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO discord_autopost_content (category, title, body, example_command, created_by)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
            """, category, title, body, example_command, created_by)
            return row["id"]

    async def remove_discord_autopost_content(self, content_id: int) -> bool:
        """Soft-delete (active = FALSE) rather than a hard DELETE, so a
        content id already referenced by some guild's current_index doesn't
        matter — the rotation query only ever selects active rows."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE discord_autopost_content SET active = FALSE WHERE id = $1", content_id
            )
            return result.split()[-1] != "0"

    async def add_sponsored_post(self, admin_id: int, content: str, button_label: str,
                                  button_url: str, runs_total: int) -> Optional[int]:
        """Queue a sponsored post to be injected into the autopost cron cycle."""
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow("""
                    INSERT INTO sponsored_posts (admin_id, content, button_label, button_url, runs_remaining, runs_total)
                    VALUES ($1, $2, $3, $4, $5, $5)
                    RETURNING id
                """, admin_id, content, button_label, button_url, runs_total)
            return row["id"] if row else None
        except Exception as e:
            print(f"[v0] Error adding sponsored post: {e}")
            return None

    async def get_next_sponsored(self) -> Optional[Dict]:
        """The oldest active sponsored post that still has runs remaining, or None."""
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow("""
                    SELECT * FROM sponsored_posts
                    WHERE is_active = TRUE AND runs_remaining > 0
                    ORDER BY created_at ASC LIMIT 1
                """)
            return dict(row) if row else None
        except Exception as e:
            print(f"[v0] Error fetching next sponsored post: {e}")
            return None

    async def mark_sponsored_sent(self, sponsored_id: int):
        """Decrement runs_remaining once per cron cycle (not once per chat sent to).
        Auto-deactivates once it hits zero."""
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute("""
                    UPDATE sponsored_posts SET runs_remaining = runs_remaining - 1
                    WHERE id = $1
                """, sponsored_id)
                await conn.execute("""
                    UPDATE sponsored_posts SET is_active = FALSE
                    WHERE id = $1 AND runs_remaining <= 0
                """, sponsored_id)
        except Exception as e:
            print(f"[v0] Error marking sponsored post sent: {e}")

    async def get_autopost_chat_ids(self) -> List[int]:
        """Every chat currently known to have the bot as a member — reuses the
        same authoritative table get_known_group_ids()/broadcast use, NOT
        chat_memberships (a much smaller table only touched by manual
        /registerme + welcome-message settings — using it here would have
        silently limited sponsored posts to only manually-registered chats)."""
        return await self.get_known_group_ids()

    async def deactivate_recurring_post(self, post_id: int, requester_id: int, is_owner: bool = False) -> bool:
        pool = await get_pool()
        async with pool.acquire() as conn:
            if is_owner:
                result = await conn.execute("UPDATE recurring_posts SET is_active = FALSE WHERE id = $1", post_id)
            else:
                result = await conn.execute(
                    "UPDATE recurring_posts SET is_active = FALSE WHERE id = $1 AND admin_id = $2",
                    post_id, requester_id
                )
            return bool(result) and result.endswith("1")

    async def list_recurring_for_chat(self, chat_id: int) -> List[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM recurring_posts WHERE chat_id = $1 AND is_active = TRUE ORDER BY id", chat_id
            )
            return [dict(r) for r in rows]

    async def list_recurring_for_admin(self, admin_id: int) -> List[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM recurring_posts WHERE admin_id = $1 AND is_active = TRUE ORDER BY id", admin_id
            )
            return [dict(r) for r in rows]

    async def get_all_active_recurring(self) -> List[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM recurring_posts WHERE is_active = TRUE ORDER BY id")
            return [dict(r) for r in rows]

    # ─────────────────────────────────────────────────────────────────
    # Broadcast
    # ─────────────────────────────────────────────────────────────────

    async def get_known_group_ids(self, clone_id: int = 0) -> List[int]:
        """clone_id=0 is the main bot; each clone only ever sees its own
        chats, never the main bot's or another clone's."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT group_id FROM bot_group_membership WHERE bot_status IN ('member', 'administrator') "
                "AND clone_id = $1",
                clone_id
            )
            return [r["group_id"] for r in rows]

    async def get_known_groups_with_titles(self, clone_id: int = 0) -> List[Dict]:
        """Same set as get_known_group_ids, but with chat_title so the
        broadcast exempt-groups picker can show names instead of raw IDs.
        Scoped to clone_id (0 = main bot) so clones can't see each other's
        or the main bot's groups."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT group_id, chat_title FROM bot_group_membership "
                "WHERE bot_status IN ('member', 'administrator') AND clone_id = $1 "
                "ORDER BY chat_title NULLS LAST",
                clone_id
            )
            return [{"group_id": r["group_id"], "chat_title": r["chat_title"] or f"Group {r['group_id']}"} for r in rows]

    async def get_all_user_ids(self) -> List[int]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id FROM users")
            return [r["user_id"] for r in rows]

    async def create_broadcast_job(self, admin_id: int, target_scope: str, content: Optional[str] = None,
                                    media_file_id: Optional[str] = None, media_type: Optional[str] = None,
                                    excluded_group_ids: Optional[List[int]] = None,
                                    join_link: Optional[str] = None, clone_id: int = 0) -> Dict:
        """Creates the job and pre-populates its recipient list. Returns the job dict.
        excluded_group_ids: group_ids to leave out even though target_scope includes groups —
        the tap-to-exempt picker in the broadcast flow, no group ID typing needed.
        join_link: optional admin-provided invite link for this specific broadcast
        (see handlers/broadcast_handler.py) — rendered as a "Join" button alongside
        the fixed Premium Group paywall button when the job is sent.
        clone_id: 0 for the main bot, else the clone's own id — a clone's broadcast
        must only ever reach groups that clone itself is a member of."""
        excluded = set(excluded_group_ids or [])
        recipients: List[tuple] = []
        if target_scope in ("users", "both"):
            for uid in await self.get_all_user_ids():
                recipients.append((uid, "user"))
        if target_scope in ("groups", "both"):
            for gid in await self.get_known_group_ids(clone_id=clone_id):
                if gid not in excluded:
                    recipients.append((gid, "group"))

        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO broadcast_jobs (admin_id, content, media_file_id, media_type, target_scope, total_recipients, status, join_link)
                VALUES ($1, $2, $3, $4, $5, $6, 'pending', $7)
                RETURNING *
            """, admin_id, content, media_file_id, media_type, target_scope, len(recipients), join_link)
            job = dict(row)
            if recipients:
                await conn.executemany(
                    "INSERT INTO broadcast_recipients (job_id, chat_id, kind) VALUES ($1, $2, $3)",
                    [(job["id"], chat_id, kind) for chat_id, kind in recipients]
                )
            return job

    async def preview_broadcast_counts(self, target_scope: str, excluded_group_ids: Optional[List[int]] = None,
                                        clone_id: int = 0) -> Dict[str, int]:
        excluded = set(excluded_group_ids or [])
        users = len(await self.get_all_user_ids()) if target_scope in ("users", "both") else 0
        groups = 0
        if target_scope in ("groups", "both"):
            groups = len([gid for gid in await self.get_known_group_ids(clone_id=clone_id) if gid not in excluded])
        return {"users": users, "groups": groups, "total": users + groups}

    async def get_next_broadcast_job(self) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM broadcast_jobs WHERE status IN ('pending', 'in_progress') ORDER BY id LIMIT 1"
            )
            return dict(row) if row else None

    async def get_broadcast_batch(self, job_id: int, batch_size: int = 20) -> List[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM broadcast_recipients WHERE job_id = $1 AND sent = FALSE ORDER BY id LIMIT $2",
                job_id, batch_size
            )
            return [dict(r) for r in rows]

    async def mark_broadcast_recipient(self, recipient_id: int, error: Optional[str] = None):
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE broadcast_recipients SET sent = TRUE, sent_at = NOW(), error = $2 WHERE id = $1",
                recipient_id, error
            )

    async def finalize_broadcast_job_progress(self, job_id: int) -> str:
        """Recompute sent/failed counts from broadcast_recipients and close out the job if fully processed."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            counts = await conn.fetchrow("""
                SELECT
                    COUNT(*) FILTER (WHERE sent AND error IS NULL) AS sent_count,
                    COUNT(*) FILTER (WHERE sent AND error IS NOT NULL) AS failed_count,
                    COUNT(*) FILTER (WHERE NOT sent) AS remaining
                FROM broadcast_recipients WHERE job_id = $1
            """, job_id)
            status = "done" if counts["remaining"] == 0 else "in_progress"
            await conn.execute("""
                UPDATE broadcast_jobs
                SET sent_count = $2, failed_count = $3, status = $4,
                    completed_at = CASE WHEN $4 = 'done' THEN NOW() ELSE completed_at END
                WHERE id = $1
            """, job_id, counts["sent_count"], counts["failed_count"], status)
            return status

    async def get_broadcast_job(self, job_id: int) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM broadcast_jobs WHERE id = $1", job_id)
            return dict(row) if row else None

    async def add_user(self, user_id: int, username: str, first_name: str, is_admin: bool = False):
        """Add or update a user"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO users (user_id, username, first_name, is_admin)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (user_id) DO UPDATE
                SET username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    is_admin = EXCLUDED.is_admin
            """, user_id, username, first_name, is_admin)

    async def _ensure_clone_status_row(self, conn, user_id: int, clone_id: int) -> None:
        """Make sure a user_clone_status row exists for this (user_id, clone_id)
        before an UPDATE — a brand-new user on a brand-new clone won't have one
        yet, and UPDATE against a missing row silently does nothing."""
        await conn.execute("""
            INSERT INTO user_clone_status (user_id, clone_id)
            VALUES ($1, $2)
            ON CONFLICT (user_id, clone_id) DO NOTHING
        """, user_id, clone_id)

    async def set_tos_accepted(self, user_id: int, clone_id: int = 0) -> bool:
        """Mark a user as having accepted the BotStore listing terms — scoped
        to clone_id (0 = main bot) since ToS acceptance, like tier and
        subscriptions, must not carry across bots."""
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                await self._ensure_clone_status_row(conn, user_id, clone_id)
                await conn.execute(
                    "UPDATE user_clone_status SET tos_accepted = TRUE WHERE user_id = $1 AND clone_id = $2",
                    user_id, clone_id
                )
            return True
        except Exception as e:
            print(f"[v0] Error setting tos_accepted: {e}")
            return False

    async def set_premium_tier(self, user_id: int, clone_id: int = 0) -> bool:
        """Mark a user's tier as premium (BotStore unlimited listings) —
        scoped to clone_id (0 = main bot). A premium purchase on one bot
        must never grant premium on another bot."""
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                await self._ensure_clone_status_row(conn, user_id, clone_id)
                await conn.execute(
                    "UPDATE user_clone_status SET tier = $1 WHERE user_id = $2 AND clone_id = $3",
                    "premium", user_id, clone_id
                )
            return True
        except Exception as e:
            print(f"[v0] Error setting premium tier: {e}")
            return False

    async def get_user(self, user_id: int, clone_id: int = 0) -> Optional[Dict]:
        """Get user info: identity fields (username/first_name/joined_date/
        is_admin/submissions_count) come from the global `users` row, but
        tier/subscription/quota fields come from user_clone_status scoped to
        clone_id (0 = main bot) — those must never leak across bots."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
            if not row:
                return None
            status = await conn.fetchrow(
                "SELECT * FROM user_clone_status WHERE user_id = $1 AND clone_id = $2",
                user_id, clone_id
            )
            return {
                "user_id": row["user_id"],
                "username": row["username"],
                "first_name": row["first_name"],
                "joined_date": row["joined_date"],
                "tier": status["tier"] if status else "free",
                "submissions_count": row["submissions_count"],
                "is_admin": bool(row["is_admin"]),
                "subscription_status": status["subscription_status"] if status else "inactive",
                "subscription_expiry": status["subscription_expiry"] if status else None,
                "tos_accepted": bool(status["tos_accepted"]) if status else False,
                "free_ai_chat_uses": status["free_ai_chat_uses"] if status else 0,
                "free_download_uses": status["free_download_uses"] if status else 0,
                "utility_sub_status": status["utility_sub_status"] if status else "inactive",
                "utility_sub_expiry": status["utility_sub_expiry"] if status else None,
                "free_image_search_used": bool(status["free_image_search_used"]) if status else False,
                "language": (status["language"] if status and status["language"] else "en"),
            }

    async def get_user_language(self, user_id: int, clone_id: int = 0) -> str:
        """Get the user's saved language code for this bot, defaulting to 'en'
        if unset/missing. Language is per (user, clone) — a user may want a
        different language on a different clone."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT language FROM user_clone_status WHERE user_id = $1 AND clone_id = $2",
                user_id, clone_id
            )
            if row and row["language"]:
                return row["language"]
            return "en"

    async def set_user_language(self, user_id: int, lang: str, clone_id: int = 0) -> bool:
        """Persist the user's chosen language (2-letter code, see i18n.SUPPORTED_LANGUAGES),
        scoped to clone_id (0 = main bot)."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await self._ensure_clone_status_row(conn, user_id, clone_id)
            await conn.execute(
                "UPDATE user_clone_status SET language = $1 WHERE user_id = $2 AND clone_id = $3",
                lang, user_id, clone_id
            )
            return True

    async def mark_free_image_search_used(self, user_id: int, clone_id: int = 0) -> bool:
        """Marks a user's one free reverse-image-search reveal as used on this
        bot (0 = main bot) — free-use allowances are per-clone, not shared."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await self._ensure_clone_status_row(conn, user_id, clone_id)
            await conn.execute(
                "UPDATE user_clone_status SET free_image_search_used = TRUE WHERE user_id = $1 AND clone_id = $2",
                user_id, clone_id
            )
            return True

    async def get_utility_usage(self, user_id: int, clone_id: int = 0) -> Dict:
        """Free-use counters + subscription status for the AI Chat / Download
        paywall (handlers/utility_paywall.py), scoped to clone_id (0 = main
        bot). Ensures the user's clone-status row exists so brand-new users
        read as 0 uses / no subscription instead of erroring out."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT free_ai_chat_uses, free_download_uses, utility_sub_status, utility_sub_expiry "
                "FROM user_clone_status WHERE user_id = $1 AND clone_id = $2",
                user_id, clone_id
            )
            if not row:
                return {
                    "free_ai_chat_uses": 0,
                    "free_download_uses": 0,
                    "utility_sub_status": "inactive",
                    "utility_sub_expiry": None,
                }
            return dict(row)

    async def increment_free_ai_chat_uses(self, user_id: int, clone_id: int = 0) -> int:
        """Consume one free AI Chat use on this bot (0 = main bot). Returns
        the new count."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await self._ensure_clone_status_row(conn, user_id, clone_id)
            row = await conn.fetchrow(
                "UPDATE user_clone_status SET free_ai_chat_uses = COALESCE(free_ai_chat_uses, 0) + 1 "
                "WHERE user_id = $1 AND clone_id = $2 RETURNING free_ai_chat_uses",
                user_id, clone_id
            )
            return row["free_ai_chat_uses"] if row else 0

    async def increment_free_download_uses(self, user_id: int, clone_id: int = 0) -> int:
        """Consume one free Download use on this bot (0 = main bot). Returns
        the new count."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await self._ensure_clone_status_row(conn, user_id, clone_id)
            row = await conn.fetchrow(
                "UPDATE user_clone_status SET free_download_uses = COALESCE(free_download_uses, 0) + 1 "
                "WHERE user_id = $1 AND clone_id = $2 RETURNING free_download_uses",
                user_id, clone_id
            )
            return row["free_download_uses"] if row else 0

    async def activate_utility_subscription(self, user_id: int, days: int = 60, clone_id: int = 0) -> bool:
        """Activate the AI Chat + Download subscription for `days` (default
        60, i.e. ~2 months) from now, scoped to clone_id (0 = main bot) — a
        subscription bought on one bot must not unlock another bot."""
        try:
            pool = await get_pool()
            expiry = datetime.now() + timedelta(days=days)
            async with pool.acquire() as conn:
                await self._ensure_clone_status_row(conn, user_id, clone_id)
                await conn.execute(
                    "UPDATE user_clone_status SET utility_sub_status = 'active', utility_sub_expiry = $1 "
                    "WHERE user_id = $2 AND clone_id = $3",
                    expiry, user_id, clone_id
                )
            return True
        except Exception as e:
            print(f"[v0] Error activating utility subscription: {e}")
            return False

    async def add_link_button(self, chat_id: int, label: str, url: str, created_by: int) -> bool:
        """Upsert a labeled-link button (feature #8) — same label in the
        same chat just updates the URL rather than erroring."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            try:
                existing = await conn.fetchval(
                    "SELECT COALESCE(MAX(position), -1) FROM custom_link_buttons WHERE chat_id = $1", chat_id
                )
                await conn.execute(
                    """
                    INSERT INTO custom_link_buttons (chat_id, label, url, position, created_by)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (chat_id, label) DO UPDATE SET url = EXCLUDED.url
                    """,
                    chat_id, label, url, existing + 1, created_by,
                )
                return True
            except Exception as e:
                print(f"[v0] Error adding link button: {e}")
                return False

    async def remove_link_button(self, chat_id: int, label: str) -> bool:
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM custom_link_buttons WHERE chat_id = $1 AND label = $2",
                chat_id, label,
            )
            return result is not None and "DELETE 0" not in result

    async def list_link_buttons(self, chat_id: int):
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT label, url FROM custom_link_buttons WHERE chat_id = $1 ORDER BY position",
                chat_id,
            )
            return [dict(r) for r in rows]

    async def log_payment(self, user_id: int, amount: float, reference: str, status: str = "pending",
                           payment_type: str = None, chat_id: int = None, group_id: int = None,
                           provider: str = "paystack"):
        """Log a payment to the generic payment_logs table (reused here for
        utility-subscription payments rather than a new table).

        payment_logs.user_id has a FK to users(user_id). Every paywall button
        (premium_group_handler.py, welcome_pay.py, image_search_handler.py,
        etc.) can fire before a user has ever run /start — e.g. tapping a
        "Pay to Join" button straight off a broadcast — so add_user() may
        never have inserted their row yet. Upsert a minimal users row first
        (same ON CONFLICT DO NOTHING pattern as _ensure_clone_status_row)
        so the payment_logs insert never trips the FK constraint.

        payment_type/chat_id let callers (premium_group_handler.py, etc.)
        tag *what* was paid for and *which* chat it gates, so has_paid() can
        later answer "did this user pay for this specific thing" instead of
        just "has this user ever paid for anything."

        group_id additionally scopes to one specific discord_premium_groups
        row. A guild can now have several premium groups sharing the same
        payment_type ("premium_group_join") and chat_id (the guild_id), so
        without group_id has_paid() couldn't tell which group was paid for.
        Telegram callers (which have no concept of "group") simply omit it.

        provider: which gateway ('paystack'/'stripe') this charge was
        actually started under — callers routing through resolve_gateway()
        for a Discord clone should pass the provider it returned. Verify-time
        code should read this back (not re-resolve "whatever's current")
        so a clone owner switching payment settings can't strand an
        in-flight payment. Defaults to 'paystack' for every caller that
        predates per-provider tracking (main bot, Telegram, anything not
        yet updated to pass it explicitly).
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO users (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
                    user_id
                )
                await conn.execute(
                    "INSERT INTO payment_logs (user_id, amount, status, paystack_reference, payment_type, chat_id, group_id, provider) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) ON CONFLICT (paystack_reference) DO NOTHING",
                    user_id, amount, status, reference, payment_type, chat_id, group_id, provider
                )

    async def has_paid(self, user_id: int, payment_type: str, chat_id: int = None, group_id: int = None) -> bool:
        """Has this user completed a payment of this specific type
        (e.g. 'premium_group_join')? Scoped to payment_type so paying for one
        paywalled feature never counts as having paid for a different one.

        chat_id is optional and backward-compatible: the Telegram bot's single
        global Premium Group calls this with chat_id=None (unchanged
        behavior — any completed payment of this type counts, matching the
        original single-tenant design documented in
        handlers/premium_group_handler.py).

        group_id scopes further to one specific discord_premium_groups row.
        Since a single guild can now host several independently-priced
        premium groups sharing the same payment_type and chat_id (guild_id),
        every Discord call site MUST pass group_id explicitly — otherwise
        paying for one group would incorrectly read as "paid" for every
        other group in the same guild. Do not regress that."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            if group_id is not None:
                row = await conn.fetchrow(
                    "SELECT 1 FROM payment_logs WHERE user_id = $1 AND payment_type = $2 "
                    "AND group_id = $3 AND status = 'completed' LIMIT 1",
                    user_id, payment_type, group_id
                )
            elif chat_id is not None:
                row = await conn.fetchrow(
                    "SELECT 1 FROM payment_logs WHERE user_id = $1 AND payment_type = $2 "
                    "AND chat_id = $3 AND status = 'completed' LIMIT 1",
                    user_id, payment_type, chat_id
                )
            else:
                row = await conn.fetchrow(
                    "SELECT 1 FROM payment_logs WHERE user_id = $1 AND payment_type = $2 "
                    "AND status = 'completed' LIMIT 1",
                    user_id, payment_type
                )
            return row is not None

    async def get_revenue_by_type(self, payment_types: List[str]) -> List[Dict]:
        """Real revenue aggregation off payment_logs (the same table every
        Discord + Telegram paywall already writes to via log_payment) —
        used by /admin revenue instead of hardcoded placeholder text.
        Returns one row per payment_type actually present:
        {"payment_type", "completed_count", "completed_total",
         "pending_count"}. Only completed payments count toward revenue;
        pending is surfaced separately so a stuck/abandoned checkout isn't
        counted as money in hand."""
        if not payment_types:
            return []
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    payment_type,
                    COUNT(*) FILTER (WHERE status = 'completed') AS completed_count,
                    COALESCE(SUM(amount) FILTER (WHERE status = 'completed'), 0) AS completed_total,
                    COUNT(*) FILTER (WHERE status = 'pending') AS pending_count
                FROM payment_logs
                WHERE payment_type = ANY($1::text[])
                GROUP BY payment_type
                ORDER BY completed_total DESC
                """,
                payment_types,
            )
            return [dict(r) for r in rows]

    async def get_latest_pending_payment(self, user_id: int, payment_type: str, chat_id: int = None) -> Optional[Dict]:
        """Most recent pending payment_logs row for this (user, payment_type
        [, chat_id]), regardless of which group it's for. Used by
        discord_bot's "I've Paid — Verify" button: since that button is a
        persistent view (survives bot restarts) it can't stash the Paystack
        reference (or which group_id it's for) in its custom_id the way the
        Telegram handler stashes it in context.user_data, so it looks the
        reference up here instead — the returned row's own group_id column
        tells the caller which group's role to grant. A user is only ever
        expected to have one payment in flight at a time, so "latest
        pending, any group" is the correct lookup rather than requiring the
        button click to already know the group."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            if chat_id is not None:
                row = await conn.fetchrow(
                    "SELECT * FROM payment_logs WHERE user_id = $1 AND payment_type = $2 "
                    "AND chat_id = $3 AND status = 'pending' ORDER BY created_date DESC LIMIT 1",
                    user_id, payment_type, chat_id
                )
            else:
                row = await conn.fetchrow(
                    "SELECT * FROM payment_logs WHERE user_id = $1 AND payment_type = $2 "
                    "AND status = 'pending' ORDER BY created_date DESC LIMIT 1",
                    user_id, payment_type
                )
            return dict(row) if row else None

    async def mark_payment_paid(self, reference: str):
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE payment_logs SET status = 'completed' WHERE paystack_reference = $1",
                reference
            )

    # ────────────────────────────────────────────────────────────────────��
    # Discord: multiple premium groups per guild (per clone)
    # ─────────────────────────────────────────────────────────────────────
    # Each row is one independently-priced paid role. No ranking between
    # groups — a guild admin (main bot or clone owner) can create as many as
    # they want, and a member can buy any subset of them.

    async def create_premium_group(self, guild_id: int, name: str, role_id: int, fee_ghs: float,
                                    created_by: int, clone_id: Optional[int] = None) -> int:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO discord_premium_groups (guild_id, clone_id, name, role_id, fee_ghs, created_by)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING group_id
                """,
                guild_id, clone_id, name, role_id, fee_ghs, created_by
            )
            return row["group_id"]

    async def list_premium_groups(self, guild_id: int, clone_id: Optional[int] = None,
                                   active_only: bool = True) -> List[Dict]:
        """List premium groups for a guild, scoped to clone_id (None = main
        bot). clone_id must match exactly (including NULL) so a clone
        running in a guild never sees — or lets members pay into — a
        different clone's (or the main bot's) groups for that same guild."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            query = "SELECT * FROM discord_premium_groups WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2"
            params = [guild_id, clone_id]
            if active_only:
                query += " AND active = TRUE"
            query += " ORDER BY group_id ASC"
            rows = await conn.fetch(query, *params)
            return [dict(r) for r in rows]

    async def get_premium_group(self, group_id: int) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM discord_premium_groups WHERE group_id = $1", group_id)
            return dict(row) if row else None

    async def update_premium_group(self, group_id: int, name: str = None, role_id: int = None,
                                    fee_ghs: float = None, active: bool = None) -> None:
        """Partial update — pass only the fields you want to change.
        Existing values are preserved via COALESCE, except `active`, which
        needs its own branch since COALESCE(NULL-meaning-"leave alone",
        FALSE) can't distinguish "leave alone" from "set to false"."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE discord_premium_groups SET
                    name = COALESCE($2, name),
                    role_id = COALESCE($3, role_id),
                    fee_ghs = COALESCE($4, fee_ghs),
                    active = CASE WHEN $5::boolean IS NULL THEN active ELSE $5 END
                WHERE group_id = $1
                """,
                group_id, name, role_id, fee_ghs, active
            )

    # ─────────────────────────────────────────────────────────────────────
    # Discord: clone bot registry
    # ─────────────────────────────────────────────────────────────────────
    # Registering a clone here only makes it *eligible* to run — a live
    # gateway process for it is spawned/supervised separately by
    # discord_bot/clone_manager.py (see that file's docstring for why
    # Discord clones can't be routed the way Telegram's webhook clones are).

    async def create_discord_clone(self, owner_id: int, bot_token_encrypted: str, bot_user_id: int,
                                    bot_username: str, application_id: int) -> int:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO discord_cloned_bots (owner_id, bot_token_encrypted, bot_user_id, bot_username, application_id)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING clone_id
                """,
                owner_id, bot_token_encrypted, bot_user_id, bot_username, application_id
            )
            return row["clone_id"]

    async def get_discord_clone(self, clone_id: int) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM discord_cloned_bots WHERE clone_id = $1", clone_id)
            return dict(row) if row else None

    async def get_discord_clones_by_owner(self, owner_id: int) -> List[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM discord_cloned_bots WHERE owner_id = $1 ORDER BY clone_id ASC",
                owner_id
            )
            return [dict(r) for r in rows]

    async def get_active_discord_clones(self) -> List[Dict]:
        """Polled by clone_manager.py's supervisor loop to decide which
        clone processes should be running right now."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM discord_cloned_bots WHERE status = 'active' ORDER BY clone_id ASC")
            return [dict(r) for r in rows]

    async def set_discord_clone_status(self, clone_id: int, status: str) -> None:
        """status is 'active' or 'inactive'. Setting 'inactive' is how an
        owner (or an admin) stops a clone — clone_manager.py will terminate
        its process on its next poll."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE discord_cloned_bots SET status = $2 WHERE clone_id = $1",
                clone_id, status
            )

    async def list_active_discord_clones(self) -> List[Dict]:
        """Discord equivalent of list_active_clones() — used by /admin clones."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM discord_cloned_bots WHERE status = 'active' ORDER BY clone_id ASC")
            return [dict(r) for r in rows]

    async def get_discord_clone_owner_ids(self) -> List[int]:
        """Distinct owner_id of every currently-active clone — the "admins"
        pool for /ownerbroadcast's target option (clone operators, not
        regular bot members). All of these users are known to the MAIN bot
        (they DM'd it to run /registerclone), so callers can always send
        via clone_id=None regardless of which clone(s) they own."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT owner_id FROM discord_cloned_bots WHERE status = 'active'"
            )
            return [r["owner_id"] for r in rows]

    async def count_discord_clones_by_owner(self, owner_id: int) -> int:
        """Total clones this owner has ever registered (any status) — used to
        work out /registerclone's every-Nth-clone-free perk. Deliberately
        counts inactive/removed clones too, so deactivating one can't be used
        to reset the free-clone counter."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM discord_cloned_bots WHERE owner_id = $1", owner_id
            )

    async def store_discord_clone_pending_payment(self, reference: str, owner_id: int, bot_token_encrypted: str,
                                                    bot_user_id: int, bot_username: str, application_id: int) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO discord_clone_pending_payments
                    (reference, owner_id, bot_token_encrypted, bot_user_id, bot_username, application_id)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (reference) DO NOTHING
                """,
                reference, owner_id, bot_token_encrypted, bot_user_id, bot_username, application_id
            )

    async def get_discord_clone_pending_payment(self, reference: str) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM discord_clone_pending_payments WHERE reference = $1", reference
            )
            return dict(row) if row else None

    async def complete_discord_clone_pending_payment(self, reference: str) -> Optional[int]:
        """Called by api/paystack_webhook.py once Paystack confirms a
        discord_clone payment. Idempotent: if this reference was already
        completed (e.g. a Paystack retry), returns the clone_id created the
        first time instead of registering a second clone for one payment."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM discord_clone_pending_payments WHERE reference = $1 FOR UPDATE",
                    reference
                )
                if row is None:
                    return None
                if row["status"] == "paid" and row["created_clone_id"] is not None:
                    return row["created_clone_id"]

                clone_row = await conn.fetchrow(
                    """
                    INSERT INTO discord_cloned_bots (owner_id, bot_token_encrypted, bot_user_id, bot_username, application_id)
                    VALUES ($1, $2, $3, $4, $5)
                    RETURNING clone_id
                    """,
                    row["owner_id"], row["bot_token_encrypted"], row["bot_user_id"],
                    row["bot_username"], row["application_id"]
                )
                clone_id = clone_row["clone_id"]
                await conn.execute(
                    "UPDATE discord_clone_pending_payments SET status = 'paid', created_clone_id = $2 WHERE reference = $1",
                    reference, clone_id
                )
                return clone_id

    async def touch_discord_clone_heartbeat(self, clone_id: int) -> None:
        """Called periodically by a running clone process itself (see
        discord_bot/bot.py's heartbeat loop) so /myclones can show
        "last seen" instead of just "registered"."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE discord_cloned_bots SET last_heartbeat = NOW() WHERE clone_id = $1",
                clone_id
            )

    # ─────────────────────────────────────────────────────────────────────
    # Discord: guild registry (what servers the bot is currently in)
    # ─────────────────────────────────────────────────────────────────────

    async def upsert_discord_guild(self, guild_id: int, guild_name: str, member_count: int,
                                    clone_id: Optional[int] = None, invite_url: Optional[str] = None) -> None:
        """Called from on_guild_join (and on_ready, to catch up any guilds
        the bot joined while offline). Clears left_at so a re-join shows
        current membership again rather than looking permanently departed.
        invite_url is best-effort (the caller may not have permission to
        create one) — only overwritten when a fresh one is actually passed,
        so a later permission-less call doesn't blank out one we already
        have stored."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            if invite_url:
                await conn.execute("""
                    INSERT INTO discord_guilds (guild_id, clone_id, guild_name, member_count, invite_url, joined_at, left_at)
                    VALUES ($1, $2, $3, $4, $5, NOW(), NULL)
                    ON CONFLICT (guild_id, COALESCE(clone_id, -1)) DO UPDATE
                    SET guild_name = EXCLUDED.guild_name,
                        member_count = EXCLUDED.member_count,
                        invite_url = EXCLUDED.invite_url,
                        left_at = NULL
                """, guild_id, clone_id, guild_name, member_count, invite_url)
            else:
                await conn.execute("""
                    INSERT INTO discord_guilds (guild_id, clone_id, guild_name, member_count, joined_at, left_at)
                    VALUES ($1, $2, $3, $4, NOW(), NULL)
                    ON CONFLICT (guild_id, COALESCE(clone_id, -1)) DO UPDATE
                    SET guild_name = EXCLUDED.guild_name,
                        member_count = EXCLUDED.member_count,
                        left_at = NULL
                """, guild_id, clone_id, guild_name, member_count)

    async def mark_discord_guild_left(self, guild_id: int, clone_id: Optional[int] = None) -> None:
        """Called from on_guild_remove. Keeps the row (left_at set) rather
        than deleting it, so history/analytics survive a kick or leave."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE discord_guilds SET left_at = NOW()
                WHERE guild_id = $1 AND COALESCE(clone_id, -1) = COALESCE($2, -1)
            """, guild_id, clone_id)

    async def get_discord_guild_count(self, clone_id: Optional[int] = None) -> int:
        """Currently-joined count (left_at IS NULL) for this bot/clone."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval("""
                SELECT COUNT(*) FROM discord_guilds
                WHERE COALESCE(clone_id, -1) = COALESCE($1, -1) AND left_at IS NULL
            """, clone_id)

    async def list_discord_guilds(self, clone_id: Optional[int] = None, include_left: bool = False) -> list:
        pool = await get_pool()
        async with pool.acquire() as conn:
            if include_left:
                rows = await conn.fetch("""
                    SELECT guild_id, guild_name, member_count, invite_url, joined_at, left_at
                    FROM discord_guilds WHERE COALESCE(clone_id, -1) = COALESCE($1, -1)
                    ORDER BY joined_at DESC
                """, clone_id)
            else:
                rows = await conn.fetch("""
                    SELECT guild_id, guild_name, member_count, invite_url, joined_at, left_at
                    FROM discord_guilds WHERE COALESCE(clone_id, -1) = COALESCE($1, -1) AND left_at IS NULL
                    ORDER BY joined_at DESC
                """, clone_id)
            return [dict(r) for r in rows]

    # ─────────────────────────────────────────────────────────────────────
    # Discord: reaction roles (button-based)
    # ─────────────────────────────────────────────────────────────────────

    async def add_reaction_role(self, guild_id: int, channel_id: int, message_id: int, role_id: int,
                                 label: str, emoji: Optional[str], created_by: int,
                                 clone_id: Optional[int] = None) -> bool:
        pool = await get_pool()
        async with pool.acquire() as conn:
            try:
                await conn.execute(
                    """
                    INSERT INTO discord_reaction_roles
                        (guild_id, clone_id, channel_id, message_id, role_id, label, emoji, created_by)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ON CONFLICT (message_id, role_id) DO UPDATE SET label = EXCLUDED.label, emoji = EXCLUDED.emoji
                    """,
                    guild_id, clone_id, channel_id, message_id, role_id, label, emoji, created_by
                )
                return True
            except Exception as e:
                logger.error(f"[v0] Error adding reaction role: {e}")
                return False

    async def remove_reaction_role(self, message_id: int, role_id: int) -> bool:
        # message_id is globally unique on Discord's side, so no clone_id
        # is needed here to disambiguate — unlike the guild-scoped lookups
        # below, which must filter by clone_id or a clone could list/rebuild
        # another clone's (or the main bot's) panels in a shared guild.
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM discord_reaction_roles WHERE message_id = $1 AND role_id = $2",
                message_id, role_id
            )
            return result is not None and "DELETE 0" not in result

    async def get_reaction_roles_for_message(self, message_id: int) -> List[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM discord_reaction_roles WHERE message_id = $1 ORDER BY id ASC",
                message_id
            )
            return [dict(r) for r in rows]

    async def get_reaction_role_panels_for_guild(self, guild_id: int, clone_id: Optional[int] = None) -> List[Dict]:
        """Distinct (channel_id, message_id) panels for this guild+clone, each
        with its role rows — used both by /reactionrole list and to rebuild
        persistent views for every existing panel on cog load."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM discord_reaction_roles WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 ORDER BY message_id, id ASC",
                guild_id, clone_id
            )
        panels: Dict[int, Dict] = {}
        for r in rows:
            row = dict(r)
            mid = row["message_id"]
            panels.setdefault(mid, {"channel_id": row["channel_id"], "message_id": mid, "roles": []})
            panels[mid]["roles"].append(row)
        return list(panels.values())

    async def get_all_reaction_role_panels(self, clone_id: Optional[int] = None) -> List[Dict]:
        """Same shape as get_reaction_role_panels_for_guild but across every
        guild this process (main bot or a specific clone) owns panels in —
        used once at startup (cog_load) to rebuild every persistent
        reaction-role view this bot process is responsible for. Filtering by
        clone_id here (rather than "every panel, every guild") is what stops
        a clone process from also registering button handlers for panels
        that belong to the main bot or another clone in a shared guild."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM discord_reaction_roles WHERE clone_id IS NOT DISTINCT FROM $1 ORDER BY message_id, id ASC",
                clone_id
            )
        panels: Dict[int, Dict] = {}
        for r in rows:
            row = dict(r)
            mid = row["message_id"]
            panels.setdefault(mid, {"guild_id": row["guild_id"], "channel_id": row["channel_id"], "message_id": mid, "roles": []})
            panels[mid]["roles"].append(row)
        return list(panels.values())

    # ─────────────────────────────────────────────────────────────────────
    # Discord: auto-mod config
    # ─────────────────────────────────────────────────────────────────────

    _AUTOMOD_DEFAULTS = {
        "action": "delete", "timeout_minutes": 10, "log_channel_id": None,
        "word_filter_enabled": False, "banned_words": [],
        "anti_invite_enabled": False,
        "anti_mention_enabled": False, "anti_mention_threshold": 5,
        "spam_enabled": False, "spam_flood_threshold": 10, "spam_flood_window_seconds": 10,
        "min_account_age_hours": 0,
        "log_channel_auto_created": False, "log_channel_notice_count": 0, "log_channel_last_notice_at": None,
        "wordfilter_notice_count": 0, "wordfilter_last_notice_at": None,
        "wizard_channel_id": None, "wizard_message_id": None, "wizard_invoker_id": None,
    }

    async def get_automod_config(self, guild_id: int, clone_id: Optional[int] = None) -> Dict:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM discord_automod_config WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2",
                guild_id, clone_id
            )
            if row:
                d = dict(row)
                d["banned_words"] = json.loads(d["banned_words"]) if isinstance(d["banned_words"], str) else d["banned_words"]
                return d
            return {"guild_id": guild_id, "clone_id": clone_id, **self._AUTOMOD_DEFAULTS}

    async def set_automod_config(self, guild_id: int, clone_id: Optional[int] = None, **fields) -> None:
        """fields may include any key from _AUTOMOD_DEFAULTS. Upserts, so the
        first /automod command in a guild works without a separate 'create'
        step. clone_id keeps a clone's filters/action/log-channel separate
        from the main bot's (or another clone's) in a shared guild."""
        current = await self.get_automod_config(guild_id, clone_id)
        merged = {**current, **fields}
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO discord_automod_config
                    (guild_id, clone_id, action, timeout_minutes, log_channel_id,
                     word_filter_enabled, banned_words, anti_invite_enabled,
                     anti_mention_enabled, anti_mention_threshold,
                     spam_enabled, spam_flood_threshold, spam_flood_window_seconds,
                     min_account_age_hours, log_channel_auto_created,
                     log_channel_notice_count, log_channel_last_notice_at,
                     wordfilter_notice_count, wordfilter_last_notice_at,
                     wizard_channel_id, wizard_message_id, wizard_invoker_id, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, NOW())
                ON CONFLICT (guild_id, (COALESCE(clone_id, -1))) DO UPDATE SET
                    action = $3, timeout_minutes = $4, log_channel_id = $5,
                    word_filter_enabled = $6, banned_words = $7, anti_invite_enabled = $8,
                    anti_mention_enabled = $9, anti_mention_threshold = $10,
                    spam_enabled = $11, spam_flood_threshold = $12, spam_flood_window_seconds = $13,
                    min_account_age_hours = $14, log_channel_auto_created = $15,
                    log_channel_notice_count = $16, log_channel_last_notice_at = $17,
                    wordfilter_notice_count = $18, wordfilter_last_notice_at = $19,
                    wizard_channel_id = $20, wizard_message_id = $21, wizard_invoker_id = $22,
                    updated_at = NOW()
                """,
                guild_id, clone_id, merged["action"], merged["timeout_minutes"], merged["log_channel_id"],
                merged["word_filter_enabled"], json.dumps(merged["banned_words"]), merged["anti_invite_enabled"],
                merged["anti_mention_enabled"], merged["anti_mention_threshold"],
                merged["spam_enabled"], merged["spam_flood_threshold"], merged["spam_flood_window_seconds"],
                merged["min_account_age_hours"], merged["log_channel_auto_created"],
                merged["log_channel_notice_count"], merged["log_channel_last_notice_at"],
                merged["wordfilter_notice_count"], merged["wordfilter_last_notice_at"],
                merged["wizard_channel_id"], merged["wizard_message_id"], merged["wizard_invoker_id"],
            )

    async def add_automod_banned_word(self, guild_id: int, word: str, clone_id: Optional[int] = None) -> bool:
        config = await self.get_automod_config(guild_id, clone_id)
        words = config["banned_words"]
        w = word.strip().lower()
        if not w or w in words:
            return False
        words.append(w)
        await self.set_automod_config(guild_id, clone_id, banned_words=words)
        return True

    async def add_automod_banned_words_bulk(self, guild_id: int, new_words: list, clone_id: Optional[int] = None) -> int:
        """Merge a batch of words into the banned list at once (e.g. loading a
        preset list), skipping ones already present. Returns how many were
        actually added."""
        config = await self.get_automod_config(guild_id, clone_id)
        words = config["banned_words"]
        existing = set(words)
        added = 0
        for word in new_words:
            w = word.strip().lower()
            if w and w not in existing:
                words.append(w)
                existing.add(w)
                added += 1
        if added:
            await self.set_automod_config(guild_id, clone_id, banned_words=words)
        return added

    async def remove_automod_banned_word(self, guild_id: int, word: str, clone_id: Optional[int] = None) -> bool:
        config = await self.get_automod_config(guild_id, clone_id)
        words = config["banned_words"]
        w = word.strip().lower()
        if w not in words:
            return False
        words.remove(w)
        await self.set_automod_config(guild_id, clone_id, banned_words=words)
        return True

    # ─────────────────────────────────────────────────────────────────────
    # Discord: automod combined reminder DMs (see _reminder_loop /
    # _views_automod_reminders.py). One row per DM actually sent, so the
    # "Remind me later" / "Don't ask again" buttons and the owner cleanup
    # command can find, and act on, an exact set of guild rows / an exact
    # message without re-scanning anything.
    # ─────────────────────────────────────────────────────────────────────

    async def create_automod_reminder_batch(self, clone_id: Optional[int], owner_id: int, items: list) -> int:
        """Called BEFORE the DM is sent so the view's buttons can reference
        a real batch id from the moment the message goes out. A batch that
        never gets set_automod_reminder_batch_message'd (send failed) is
        just an inert row — nothing joins against channel_id/message_id
        being NULL, so it's safe to leave behind."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO discord_automod_reminder_batches (clone_id, owner_id, items) "
                "VALUES ($1, $2, $3) RETURNING id",
                clone_id, owner_id, json.dumps(items),
            )
            return row["id"]

    async def set_automod_reminder_batch_message(self, batch_id: int, channel_id: int, message_id: int) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE discord_automod_reminder_batches SET channel_id = $2, message_id = $3 WHERE id = $1",
                batch_id, channel_id, message_id,
            )

    async def get_automod_reminder_batch(self, batch_id: int) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM discord_automod_reminder_batches WHERE id = $1", batch_id
            )
            if row is None:
                return None
            d = dict(row)
            d["items"] = json.loads(d["items"]) if isinstance(d["items"], str) else d["items"]
            return d

    async def mark_automod_reminder_batch_resolved(self, batch_id: int) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE discord_automod_reminder_batches SET resolved = TRUE WHERE id = $1", batch_id
            )

    async def delete_automod_reminder_batch(self, batch_id: int) -> None:
        """Used when the DM send itself fails (known outcome, e.g. DMs
        closed) — no point keeping a batch row around that no message will
        ever back, since the button-bearing message that would reference it
        was never actually sent."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM discord_automod_reminder_batches WHERE id = $1", batch_id
            )

    async def list_automod_reminder_batches(self, clone_id: Optional[int] = None) -> list:
        """All batches that actually got a message sent (channel_id/
        message_id populated), for /automod owner cleanupreminders. Not
        filtered by resolved — an un-actioned batch's message still exists
        in the owner's DMs and is just as deletable as a resolved one."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM discord_automod_reminder_batches "
                "WHERE clone_id IS NOT DISTINCT FROM $1 AND message_id IS NOT NULL",
                clone_id,
            )
            out = []
            for row in rows:
                d = dict(row)
                d["items"] = json.loads(d["items"]) if isinstance(d["items"], str) else d["items"]
                out.append(d)
            return out

    async def delete_automod_reminder_batches(self, batch_ids: list) -> None:
        if not batch_ids:
            return
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM discord_automod_reminder_batches WHERE id = ANY($1::bigint[])", batch_ids
            )

    # ─────────────────────────────────────────────────────────────────────
    # Discord: leveling / XP
    # ─────────────────────────────────────────────────────────────────────

    async def add_xp(self, guild_id: int, user_id: int, amount: int, new_level: int,
                      clone_id: Optional[int] = None) -> Dict:
        """Adds amount to this member's total_xp and recomputes level from the
        POST-increment total_xp inside the same row-locked transaction, so level
        can never trail total_xp under concurrent awards (previously `level`
        was written from the caller's pre-read total_xp, which could lag a
        concurrent writer's atomic total_xp += amount).
        `new_level` is accepted for backward compatibility but is only used as
        the level for a brand-new row's initial insert value before recompute;
        the authoritative level is always derived from modules.leveling.compute_level
        on the row's true post-update total_xp.
        clone_id disambiguates the same (guild_id, user_id) so a clone and
        the main bot running XP in the same guild keep separate totals —
        see discord_xp_guild_clone_user_key."""
        from modules.leveling import compute_level
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Row lock via UPSERT-then-lock: upsert total_xp only (level
                # left untouched by this statement), returning the true
                # post-increment total_xp. The row is now locked for the rest
                # of this transaction, so no other add_xp call for this
                # (guild_id, clone_id, user_id) can interleave before we
                # write the level derived from that exact total_xp.
                row = await conn.fetchrow(
                    """
                    INSERT INTO discord_xp (guild_id, clone_id, user_id, total_xp, level, last_xp_at)
                    VALUES ($1, $2, $3, $4, $5, NOW())
                    ON CONFLICT (guild_id, (COALESCE(clone_id, -1)), user_id) DO UPDATE
                        SET total_xp = discord_xp.total_xp + $4, last_xp_at = NOW()
                    RETURNING *
                    """,
                    guild_id, clone_id, user_id, amount, new_level
                )
                true_level = compute_level(row["total_xp"])
                if true_level != row["level"]:
                    row = await conn.fetchrow(
                        """
                        UPDATE discord_xp SET level = $4
                        WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 AND user_id = $3
                        RETURNING *
                        """,
                        guild_id, clone_id, user_id, true_level
                    )
                return dict(row)

    async def get_xp(self, guild_id: int, user_id: int, clone_id: Optional[int] = None) -> Dict:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM discord_xp WHERE guild_id = $1 AND user_id = $2 AND clone_id IS NOT DISTINCT FROM $3",
                guild_id, user_id, clone_id
            )
            return dict(row) if row else {"guild_id": guild_id, "user_id": user_id, "clone_id": clone_id, "total_xp": 0, "level": 0, "last_xp_at": None}

    async def set_guild_invite_url(self, guild_id: int, invite_url: str, clone_id: Optional[int] = None) -> None:
        """Sets invite_url on an existing discord_guilds row without
        touching guild_name/member_count. Used only by the opt-in registry
        invite consent flow (bot.py's _offer_registry_invite_consent) —
        the owner explicitly clicked "Allow" in a DM, so this is the one
        place a fresh invite is written after the row already exists from
        on_guild_join's initial (invite-less) upsert."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE discord_guilds SET invite_url = $2 WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $3",
                guild_id, invite_url, clone_id
            )

    # --- Inter-server roast arena (see discord_bot/cogs/roast_arena.py) -----
    # Small, feature-scoped accessors matching the surrounding pool.acquire()
    # + `clone_id IS NOT DISTINCT FROM` style. Tables live in
    # database/migrations/003_roast_arena.sql.
    async def get_roast_arena_config(self, guild_id: int, clone_id: Optional[int] = None) -> Dict:
        """Returns the per-guild arena config row as a dict, or a dict of
        defaults (nothing enabled, never prompted) if the guild has no row
        yet — callers can always read .["enabled"] etc without a None check."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM discord_roast_arena_config "
                "WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2",
                guild_id, clone_id
            )
        if row:
            return dict(row)
        return {
            "guild_id": guild_id, "clone_id": clone_id, "enabled": False,
            "consent_prompted": False, "dont_ask_again": False,
            "remind_after": None, "battleground_channel_id": None,
        }

    async def upsert_roast_arena_config(
        self, guild_id: int, clone_id: Optional[int] = None, *,
        enabled: Optional[bool] = None, consent_prompted: Optional[bool] = None,
        dont_ask_again: Optional[bool] = None, remind_after=None,
        battleground_channel_id: Optional[int] = None,
    ) -> None:
        """Column-selective upsert: any kwarg left as None keeps its current
        value (COALESCE against the existing row), so a caller can flip a
        single flag — e.g. dont_ask_again=True — without clobbering the rest.
        On first insert the None columns fall back to the table defaults."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO discord_roast_arena_config
                    (guild_id, clone_id, enabled, consent_prompted, dont_ask_again,
                     remind_after, battleground_channel_id)
                VALUES ($1, $2, COALESCE($3, FALSE), COALESCE($4, FALSE),
                        COALESCE($5, FALSE), $6, $7)
                ON CONFLICT (guild_id, COALESCE(clone_id, -1)) DO UPDATE SET
                    enabled = COALESCE($3, discord_roast_arena_config.enabled),
                    consent_prompted = COALESCE($4, discord_roast_arena_config.consent_prompted),
                    dont_ask_again = COALESCE($5, discord_roast_arena_config.dont_ask_again),
                    remind_after = COALESCE($6, discord_roast_arena_config.remind_after),
                    battleground_channel_id = COALESCE($7, discord_roast_arena_config.battleground_channel_id),
                    updated_at = NOW()
                """,
                guild_id, clone_id, enabled, consent_prompted, dont_ask_again,
                remind_after, battleground_channel_id
            )

    async def list_optedin_roast_arena_guilds(
        self, clone_id: Optional[int] = None, exclude_guild_id: Optional[int] = None,
        any_clone: bool = False,
    ) -> List[Dict]:
        """Every guild that has enabled=TRUE, optionally excluding one (the
        challenger's own server). Used both by the random target picker and
        by the event-invite broadcast.

        any_clone=True pools across every clone + the main bot (the shared
        arena) instead of just this process's own clone_id. Matching a guild
        this way doesn't mean this process can act on it directly — see
        enqueue_roast_arena_action / _drain_arena_actions in
        discord_bot/cogs/roast_arena.py for how a cross-clone match actually
        gets executed by whichever process holds that guild's gateway
        connection."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            if any_clone:
                rows = await conn.fetch(
                    "SELECT * FROM discord_roast_arena_config "
                    "WHERE enabled = TRUE "
                    "AND ($1::BIGINT IS NULL OR guild_id <> $1)",
                    exclude_guild_id
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM discord_roast_arena_config "
                    "WHERE enabled = TRUE AND clone_id IS NOT DISTINCT FROM $1 "
                    "AND ($2::BIGINT IS NULL OR guild_id <> $2)",
                    clone_id, exclude_guild_id
                )
            return [dict(r) for r in rows]

    # --- Roast arena: cross-clone outbox relay ------------------------------
    # See database/migrations/005_roast_arena_outbox.sql and
    # discord_bot/cogs/roast_arena.py _drain_arena_actions. Each clone (and
    # the main bot) is its own Discord gateway process with its own in-memory
    # guild cache, so self.bot.get_guild(...) only ever resolves a guild THIS
    # process is connected to. When any_clone pooling above matches a
    # challenger with a guild living on a different process, that other
    # process is the only one that can actually DM its admins / post in it —
    # these functions are the relay that hands the job off.
    async def enqueue_roast_arena_action(
        self, target_guild_id: int, action_type: str, payload: dict
    ) -> int:
        """Writes a pending job for whichever process has target_guild_id in
        its own guild cache to pick up and execute. payload is a snapshot of
        whatever that action needs (challenge id, display names, etc.) taken
        at enqueue time, so the executing process never has to re-derive
        state the enqueuing process already had in hand."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO discord_roast_arena_outbox (target_guild_id, action_type, payload)
                VALUES ($1, $2, $3::jsonb)
                RETURNING id
                """,
                target_guild_id, action_type, json.dumps(payload)
            )
            return row["id"]

    async def claim_roast_arena_actions(
        self, reachable_guild_ids: List[int], limit: int = 25
    ) -> List[Dict]:
        """Atomically claims up to `limit` still-pending rows targeting any
        guild in reachable_guild_ids (i.e. guilds THIS process currently has
        cached), flipping them to 'claimed'. FOR UPDATE SKIP LOCKED means two
        processes ticking their pollers at the same moment can never both
        claim — and therefore never both execute — the same row."""
        if not reachable_guild_ids:
            return []
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                UPDATE discord_roast_arena_outbox
                SET status = 'claimed'
                WHERE id IN (
                    SELECT id FROM discord_roast_arena_outbox
                    WHERE status = 'pending' AND target_guild_id = ANY($1::bigint[])
                      AND expires_at > NOW()
                    ORDER BY id
                    LIMIT $2
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING *
                """,
                reachable_guild_ids, limit
            )
            return [dict(r) for r in rows]

    async def complete_roast_arena_action(
        self, action_id: int, *, success: bool, result: Optional[dict] = None
    ) -> None:
        """Marks a claimed row as its final 'completed' or 'failed' state,
        stamping whatever result info the caller wants preserved (e.g. how
        many admins got DMed)."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE discord_roast_arena_outbox
                SET status = $2, result = $3::jsonb, completed_at = NOW()
                WHERE id = $1
                """,
                action_id, "completed" if success else "failed", json.dumps(result or {})
            )

    async def expire_stale_roast_arena_actions(self) -> int:
        """Marks any still-'pending' row past its expiry as 'failed' — an
        orphaned job (the bot got kicked from target_guild_id, or that clone
        was deactivated, so no process will EVER have it cached) would
        otherwise sit pending and get re-checked by every poller tick
        forever. Returns how many rows were expired, for logging."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                UPDATE discord_roast_arena_outbox
                SET status = 'failed', completed_at = NOW()
                WHERE status = 'pending' AND expires_at <= NOW()
                RETURNING id
                """
            )
            return len(rows)

    async def create_roast_arena_challenge(
        self, *, clone_id: Optional[int], challenger_guild_id: int,
        challenger_user_id: int, challenged_guild_id: int, expires_at,
        challenger_contestant_id: Optional[int] = None,
    ) -> int:
        """Creates a 'pending_approval' challenge and returns its id."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO discord_roast_arena_challenges
                    (clone_id, challenger_guild_id, challenger_user_id,
                     challenged_guild_id, challenger_contestant_id, status, expires_at)
                VALUES ($1, $2, $3, $4, $5, 'pending_approval', $6)
                RETURNING id
                """,
                clone_id, challenger_guild_id, challenger_user_id,
                challenged_guild_id, challenger_contestant_id, expires_at
            )
            return row["id"]

    # --- Roast arena: single shared host + apply-to-host requests ----------
    # See database/migrations/004_roast_arena_host.sql and
    # discord_bot/cogs/_views_roast_arena_host_wizard.py.
    async def get_roast_arena_host(self) -> Dict:
        """Returns the singleton host row, or a dict with channel_id=None if
        no host has been approved yet — callers can read .get("channel_id")
        without a None check on the dict itself."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM discord_roast_arena_host WHERE id = 1"
            )
        if row:
            return dict(row)
        return {"guild_id": None, "channel_id": None, "approved_by_user_id": None}

    async def set_roast_arena_host(self, guild_id: int, channel_id: int, approved_by_user_id: int) -> None:
        """Flips the singleton host to this guild/channel. Called only after
        an owner/DISCORD_CLONE_ADMIN_IDS admin approves an application."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO discord_roast_arena_host (id, guild_id, channel_id, approved_by_user_id, updated_at)
                VALUES (1, $1, $2, $3, NOW())
                ON CONFLICT (id) DO UPDATE SET
                    guild_id = $1, channel_id = $2, approved_by_user_id = $3, updated_at = NOW()
                """,
                guild_id, channel_id, approved_by_user_id
            )
            # Only one host at a time: superseding every other still-pending
            # application avoids a stale "pending" wizard card claiming an
            # application is live once a different guild has already won.
            await conn.execute(
                """
                UPDATE discord_roast_arena_host_requests
                SET status = 'superseded', resolved_at = NOW()
                WHERE status = 'pending' AND guild_id <> $1
                """,
                guild_id
            )

    async def create_roast_arena_host_request(self, guild_id: int, channel_id: int, applicant_user_id: int) -> Dict:
        """Upserts a 'pending' application for this guild — re-applying (e.g.
        clicking Apply again, or offering a different channel) refreshes the
        existing pending row instead of stacking duplicates."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO discord_roast_arena_host_requests
                    (guild_id, channel_id, applicant_user_id, status)
                VALUES ($1, $2, $3, 'pending')
                ON CONFLICT (guild_id) WHERE status = 'pending' DO UPDATE SET
                    channel_id = $2, applicant_user_id = $3, created_at = NOW()
                RETURNING *
                """,
                guild_id, channel_id, applicant_user_id
            )
            return dict(row)

    async def get_pending_roast_arena_host_request(self, guild_id: int) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM discord_roast_arena_host_requests WHERE guild_id = $1 AND status = 'pending'",
                guild_id
            )
            return dict(row) if row else None

    async def get_roast_arena_host_request(self, request_id: int) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM discord_roast_arena_host_requests WHERE id = $1", request_id
            )
            return dict(row) if row else None

    async def resolve_roast_arena_host_request(
        self, request_id: int, *, status: str, reviewed_by_user_id: int
    ) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE discord_roast_arena_host_requests
                SET status = $2, reviewed_by_user_id = $3, resolved_at = NOW()
                WHERE id = $1
                """,
                request_id, status, reviewed_by_user_id
            )

    async def get_roast_arena_challenge(self, challenge_id: int) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM discord_roast_arena_challenges WHERE id = $1", challenge_id
            )
            return dict(row) if row else None

    async def get_active_roast_arena_challenge_by_channel(self, channel_id: int) -> Optional[Dict]:
        """The live battle currently hosted in a given channel, if any — used
        so anything channel-scoped (e.g. a listener) can find the row in one
        query instead of scanning."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM discord_roast_arena_challenges "
                "WHERE battleground_channel_id = $1 AND status = 'active' "
                "ORDER BY id DESC LIMIT 1",
                channel_id
            )
            return dict(row) if row else None

    async def list_roast_arena_challenges_by_status(self, statuses) -> List[Dict]:
        """All challenges in any of the given statuses (poller uses this for
        both the live-panel refresh and stale-challenge expiry)."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM discord_roast_arena_challenges WHERE status = ANY($1::text[])",
                list(statuses)
            )
            return [dict(r) for r in rows]

    async def update_roast_arena_challenge(self, challenge_id: int, **fields) -> None:
        """Whitelisted partial update on a challenge row. Only known columns
        are writable, so the dynamic SET clause can never take an
        attacker-influenced column name."""
        allowed = {
            "status", "winner_side", "challenger_contestant_id",
            "challenged_contestant_id", "battleground_guild_id",
            "battleground_channel_id", "panel_message_id", "battle_ends_at",
            "expires_at", "resolved_at",
            "challenger_panel_channel_id", "challenger_panel_message_id",
            "challenged_panel_channel_id", "challenged_panel_message_id",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        cols = list(updates.keys())
        set_clause = ", ".join(f"{c} = ${i + 1}" for i, c in enumerate(cols))
        args = [updates[c] for c in cols]
        args.append(challenge_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"UPDATE discord_roast_arena_challenges SET {set_clause} WHERE id = ${len(args)}",
                *args
            )

    async def claim_roast_arena_approval(self, challenge_id: int, new_expires_at) -> Optional[Dict]:
        """Atomically move pending_approval -> awaiting_accept, but only if it
        is still pending and unexpired. Returns the updated row, or None if
        another admin already handled it / it expired — prevents a double
        approve race."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE discord_roast_arena_challenges
                SET status = 'awaiting_accept', expires_at = $2
                WHERE id = $1 AND status = 'pending_approval' AND expires_at > NOW()
                RETURNING *
                """,
                challenge_id, new_expires_at
            )
            return dict(row) if row else None

    async def claim_roast_arena_accept(
        self, challenge_id: int, contestant_id: int, battle_ends_at
    ) -> Optional[Dict]:
        """Atomically move awaiting_accept -> active while stamping the
        challenged contestant and battle end time. Returns the row, or None if
        someone already accepted — so only the FIRST clicker becomes the
        contestant."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE discord_roast_arena_challenges
                SET challenged_contestant_id = $2, status = 'active',
                    battle_ends_at = $3, resolved_at = NULL
                WHERE id = $1 AND status = 'awaiting_accept'
                RETURNING *
                """,
                challenge_id, contestant_id, battle_ends_at
            )
            return dict(row) if row else None

    async def record_roast_arena_vote(self, challenge_id: int, voter_user_id: int, choice: str) -> None:
        """One vote per user per challenge, changeable up to expiry: upserts on
        the (challenge_id, voter_user_id) unique index so re-voting flips the
        existing row's choice instead of stacking a second vote."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO discord_roast_arena_votes (challenge_id, voter_user_id, choice)
                VALUES ($1, $2, $3)
                ON CONFLICT (challenge_id, voter_user_id)
                DO UPDATE SET choice = $3, updated_at = NOW()
                """,
                challenge_id, voter_user_id, choice
            )

    async def count_roast_arena_votes(self, challenge_id: int) -> Dict[str, int]:
        """{'challenger': n, 'challenged': m} — always both keys, zero-filled."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT choice, COUNT(*) AS n FROM discord_roast_arena_votes "
                "WHERE challenge_id = $1 GROUP BY choice",
                challenge_id
            )
        counts = {"challenger": 0, "challenged": 0}
        for r in rows:
            counts[r["choice"]] = r["n"]
        return counts

    async def get_active_roast_arena_challenge_for_guild(
        self, guild_id: int, clone_id: Optional[int] = None
    ) -> Optional[Dict]:
        """Any in-flight challenge this guild is the challenger for
        (pending_approval / awaiting_accept / active) — used to stop a server
        starting a second challenge while one is still running."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM discord_roast_arena_challenges
                WHERE challenger_guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2
                  AND status IN ('pending_approval', 'awaiting_accept', 'active')
                ORDER BY id DESC LIMIT 1
                """,
                guild_id, clone_id
            )
            return dict(row) if row else None

    async def get_xp_leaderboard(self, guild_id: int, limit: int = 10, clone_id: Optional[int] = None) -> List[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM discord_xp WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 ORDER BY total_xp DESC LIMIT $3",
                guild_id, clone_id, limit
            )
            return [dict(r) for r in rows]

    async def get_leader_link(self, guild_id: int, user_id: int, clone_id: Optional[int] = None) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM discord_leader_links WHERE guild_id = $1 AND user_id = $2 AND clone_id IS NOT DISTINCT FROM $3",
                guild_id, user_id, clone_id
            )
            return dict(row) if row else None

    async def submit_leader_link(self, guild_id: int, user_id: int, invite_url: str, clone_id: Optional[int] = None) -> Dict:
        """Insert or reset a link submission to 'pending'. Called from the
        DM modal — re-submitting (e.g. after a denial) puts it back into
        the review queue rather than silently no-op'ing."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO discord_leader_links (guild_id, clone_id, user_id, invite_url, status, submitted_at, reviewed_at, reviewed_by)
                VALUES ($1, $2, $3, $4, 'pending', NOW(), NULL, NULL)
                ON CONFLICT (guild_id, (COALESCE(clone_id, -1)), user_id)
                DO UPDATE SET invite_url = EXCLUDED.invite_url, status = 'pending', submitted_at = NOW(), reviewed_at = NULL, reviewed_by = NULL
                RETURNING *
                """,
                guild_id, clone_id, user_id, invite_url
            )
            return dict(row)

    async def review_leader_link(self, link_id: int, approve: bool, reviewer_id: int) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE discord_leader_links
                SET status = $2, reviewed_at = NOW(), reviewed_by = $3
                WHERE id = $1
                RETURNING *
                """,
                link_id, "approved" if approve else "denied", reviewer_id
            )
            return dict(row) if row else None

    async def count_active_members(self, guild_id: int, days: int = 7, clone_id: Optional[int] = None) -> int:
        """Rough activity signal for /serveranalytics: members with at
        least one XP-earning message in the last `days` days. Not a
        precise unique-message count — it's driven by discord_xp.last_xp_at,
        which only advances once per XP cooldown window — but it's a cheap,
        already-tracked proxy for "how many people are actually talking"
        without needing a dedicated message-log table."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(
                """
                SELECT COUNT(*) FROM discord_xp
                WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2
                  AND last_xp_at >= NOW() - ($3 || ' days')::INTERVAL
                """,
                guild_id, clone_id, str(days),
            )

    async def add_level_role(self, guild_id: int, level: int, role_id: int, clone_id: Optional[int] = None) -> bool:
        pool = await get_pool()
        async with pool.acquire() as conn:
            try:
                await conn.execute(
                    """
                    INSERT INTO discord_level_roles (guild_id, clone_id, level, role_id)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (guild_id, (COALESCE(clone_id, -1)), level) DO UPDATE SET role_id = EXCLUDED.role_id
                    """,
                    guild_id, clone_id, level, role_id
                )
                return True
            except Exception as e:
                logger.error(f"[v0] Error adding level role: {e}")
                return False

    async def remove_level_role(self, guild_id: int, level: int, clone_id: Optional[int] = None) -> bool:
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM discord_level_roles WHERE guild_id = $1 AND level = $2 AND clone_id IS NOT DISTINCT FROM $3",
                guild_id, level, clone_id
            )
            return result is not None and "DELETE 0" not in result

    async def get_level_roles(self, guild_id: int, clone_id: Optional[int] = None) -> List[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM discord_level_roles WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 ORDER BY level ASC",
                guild_id, clone_id
            )
            return [dict(r) for r in rows]

    async def get_voice_xp_config(self, guild_id: int, clone_id: Optional[int] = None) -> Dict:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM discord_voice_xp_config WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2",
                guild_id, clone_id
            )
            if row:
                return dict(row)
            return {
                "guild_id": guild_id, "clone_id": clone_id, "enabled": True,
                "xp_per_minute": 10, "afk_channel_excluded": True,
            }

    async def set_voice_xp_config(self, guild_id: int, clone_id: Optional[int] = None,
                                   enabled: Optional[bool] = None, xp_per_minute: Optional[int] = None,
                                   afk_channel_excluded: Optional[bool] = None) -> Dict:
        """Upserts only the fields passed (None = leave at current/default)."""
        current = await self.get_voice_xp_config(guild_id, clone_id=clone_id)
        enabled = current["enabled"] if enabled is None else enabled
        xp_per_minute = current["xp_per_minute"] if xp_per_minute is None else xp_per_minute
        afk_channel_excluded = current["afk_channel_excluded"] if afk_channel_excluded is None else afk_channel_excluded
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO discord_voice_xp_config (guild_id, clone_id, enabled, xp_per_minute, afk_channel_excluded)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (guild_id, (COALESCE(clone_id, -1))) DO UPDATE
                    SET enabled = $3, xp_per_minute = $4, afk_channel_excluded = $5
                RETURNING *
                """,
                guild_id, clone_id, enabled, xp_per_minute, afk_channel_excluded
            )
            return dict(row)

    _UNSET = object()  # sentinel distinct from None, so callers can explicitly clear a nullable field

    async def get_starboard_config(self, guild_id: int, clone_id: Optional[int] = None) -> Dict:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM discord_starboard_config WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2",
                guild_id, clone_id
            )
            if row:
                return dict(row)
            return {
                "guild_id": guild_id, "clone_id": clone_id, "channel_id": None, "threshold": 5, "emoji": "⭐",
                "wizard_channel_id": None, "wizard_message_id": None, "wizard_invoker_id": None,
            }

    async def set_starboard_config(self, guild_id: int, clone_id: Optional[int] = None,
                                    channel_id=_UNSET, threshold=_UNSET, emoji=_UNSET,
                                    wizard_channel_id=_UNSET, wizard_message_id=_UNSET, wizard_invoker_id=_UNSET) -> Dict:
        """channel_id/threshold/emoji/wizard_* each default to the _UNSET
        sentinel (not None) so a caller can explicitly clear channel_id to
        NULL — e.g. StarboardCog.disable_cmd — without that being
        indistinguishable from "didn't pass this argument, keep current
        value". Previously channel_id defaulted to None with that same
        keep-current meaning, which made /starboard disable's
        channel_id=None call a silent no-op; fixed here as part of wiring
        the wizard's enable/disable button to this same method."""
        current = await self.get_starboard_config(guild_id, clone_id=clone_id)
        channel_id = current["channel_id"] if channel_id is self._UNSET else channel_id
        threshold = current["threshold"] if threshold is self._UNSET else threshold
        emoji = current["emoji"] if emoji is self._UNSET else emoji
        wizard_channel_id = current.get("wizard_channel_id") if wizard_channel_id is self._UNSET else wizard_channel_id
        wizard_message_id = current.get("wizard_message_id") if wizard_message_id is self._UNSET else wizard_message_id
        wizard_invoker_id = current.get("wizard_invoker_id") if wizard_invoker_id is self._UNSET else wizard_invoker_id
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO discord_starboard_config
                    (guild_id, clone_id, channel_id, threshold, emoji,
                     wizard_channel_id, wizard_message_id, wizard_invoker_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (guild_id, (COALESCE(clone_id, -1))) DO UPDATE
                    SET channel_id = $3, threshold = $4, emoji = $5,
                        wizard_channel_id = $6, wizard_message_id = $7, wizard_invoker_id = $8
                RETURNING *
                """,
                guild_id, clone_id, channel_id, threshold, emoji,
                wizard_channel_id, wizard_message_id, wizard_invoker_id
            )
            return dict(row)

    async def get_starboard_post(self, guild_id: int, source_message_id: int, clone_id: Optional[int] = None) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM discord_starboard_posts WHERE guild_id = $1 AND source_message_id = $2 AND clone_id IS NOT DISTINCT FROM $3",
                guild_id, source_message_id, clone_id
            )
            return dict(row) if row else None

    async def upsert_starboard_post(self, guild_id: int, source_message_id: int, source_channel_id: int,
                                     starboard_message_id: int, star_count: int, clone_id: Optional[int] = None) -> Dict:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO discord_starboard_posts
                    (guild_id, clone_id, source_message_id, source_channel_id, starboard_message_id, star_count)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (guild_id, (COALESCE(clone_id, -1)), source_message_id) DO UPDATE
                    SET star_count = $6
                RETURNING *
                """,
                guild_id, clone_id, source_message_id, source_channel_id, starboard_message_id, star_count
            )
            return dict(row)

    async def delete_starboard_post(self, guild_id: int, source_message_id: int, clone_id: Optional[int] = None) -> None:
        """Clears a starboard mapping row — used when the repost itself was
        deleted out from under us, so the source message can be reposted on
        a future reaction instead of the entry staying permanently wedged."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM discord_starboard_posts WHERE guild_id = $1 AND source_message_id = $2 AND clone_id IS NOT DISTINCT FROM $3",
                guild_id, source_message_id, clone_id
            )

    async def create_suggestion(self, guild_id: int, author_id: int, message_id: int, channel_id: int,
                                 content: str, clone_id: Optional[int] = None) -> Dict:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO discord_suggestions (guild_id, clone_id, author_id, message_id, channel_id, content)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING *
                """,
                guild_id, clone_id, author_id, message_id, channel_id, content
            )
            return dict(row)

    async def get_suggestion_by_message(self, message_id: int) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM discord_suggestions WHERE message_id = $1", message_id)
            return dict(row) if row else None

    async def set_suggestion_votes(self, message_id: int, upvotes: int, downvotes: int) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE discord_suggestions SET upvotes = $2, downvotes = $3 WHERE message_id = $1 RETURNING *",
                message_id, upvotes, downvotes
            )
            return dict(row) if row else None

    async def set_suggestion_status(self, message_id: int, status: str) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE discord_suggestions SET status = $2 WHERE message_id = $1 RETURNING *",
                message_id, status
            )
            return dict(row) if row else None

    async def get_suggestion_config(self, guild_id: int, clone_id: Optional[int] = None) -> Dict:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM discord_suggestion_config WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2",
                guild_id, clone_id
            )
            if row:
                return dict(row)
            return {"guild_id": guild_id, "clone_id": clone_id, "approved_log_channel_id": None}

    async def set_suggestion_config(self, guild_id: int, clone_id: Optional[int] = None,
                                     approved_log_channel_id: Optional[int] = None) -> Dict:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO discord_suggestion_config (guild_id, clone_id, approved_log_channel_id)
                VALUES ($1, $2, $3)
                ON CONFLICT (guild_id, (COALESCE(clone_id, -1))) DO UPDATE
                    SET approved_log_channel_id = $3
                RETURNING *
                """,
                guild_id, clone_id, approved_log_channel_id
            )
            return dict(row)

    async def get_ticket_config(self, guild_id: int, clone_id: Optional[int] = None) -> Dict:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM discord_ticket_config WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2",
                guild_id, clone_id
            )
            if row:
                return dict(row)
            return {
                "guild_id": guild_id, "clone_id": clone_id, "support_role_id": None,
                "category_id": None, "panel_channel_id": None, "panel_message_id": None,
                "welcome_message": None,
                "wizard_channel_id": None, "wizard_message_id": None, "wizard_invoker_id": None,
            }

    async def set_ticket_config(self, guild_id: int, clone_id: Optional[int] = None, **fields) -> Dict:
        """fields may include support_role_id, category_id, panel_channel_id,
        panel_message_id, welcome_message, wizard_channel_id,
        wizard_message_id, wizard_invoker_id — any key omitted keeps its
        current value (upsert-merge), same convention as set_automod_config."""
        current = await self.get_ticket_config(guild_id, clone_id=clone_id)
        support_role_id = fields.get("support_role_id", current["support_role_id"])
        category_id = fields.get("category_id", current["category_id"])
        panel_channel_id = fields.get("panel_channel_id", current["panel_channel_id"])
        panel_message_id = fields.get("panel_message_id", current["panel_message_id"])
        welcome_message = fields.get("welcome_message", current.get("welcome_message"))
        wizard_channel_id = fields.get("wizard_channel_id", current.get("wizard_channel_id"))
        wizard_message_id = fields.get("wizard_message_id", current.get("wizard_message_id"))
        wizard_invoker_id = fields.get("wizard_invoker_id", current.get("wizard_invoker_id"))
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO discord_ticket_config
                    (guild_id, clone_id, support_role_id, category_id, panel_channel_id, panel_message_id,
                     welcome_message, wizard_channel_id, wizard_message_id, wizard_invoker_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                ON CONFLICT (guild_id, (COALESCE(clone_id, -1))) DO UPDATE
                    SET support_role_id = $3, category_id = $4, panel_channel_id = $5, panel_message_id = $6,
                        welcome_message = $7, wizard_channel_id = $8, wizard_message_id = $9, wizard_invoker_id = $10
                RETURNING *
                """,
                guild_id, clone_id, support_role_id, category_id, panel_channel_id, panel_message_id,
                welcome_message, wizard_channel_id, wizard_message_id, wizard_invoker_id
            )
            return dict(row)

    async def create_ticket(self, guild_id: int, channel_id: int, opener_id: int, clone_id: Optional[int] = None) -> Dict:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO discord_tickets (guild_id, clone_id, channel_id, opener_id)
                VALUES ($1, $2, $3, $4)
                RETURNING *
                """,
                guild_id, clone_id, channel_id, opener_id
            )
            return dict(row)

    async def get_ticket(self, channel_id: int) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM discord_tickets WHERE channel_id = $1", channel_id)
            return dict(row) if row else None

    async def get_open_ticket_for_opener(self, guild_id: int, opener_id: int, clone_id: Optional[int] = None) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM discord_tickets
                WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 AND opener_id = $3 AND status = 'open'
                """,
                guild_id, clone_id, opener_id
            )
            return dict(row) if row else None

    async def claim_ticket(self, channel_id: int, staff_id: int) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE discord_tickets SET claimed_by = $2 WHERE channel_id = $1 RETURNING *",
                channel_id, staff_id
            )
            return dict(row) if row else None

    async def close_ticket(self, channel_id: int) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE discord_tickets SET status = 'closed', closed_at = NOW() WHERE channel_id = $1 RETURNING *",
                channel_id
            )
            return dict(row) if row else None

    async def create_giveaway(self, guild_id: int, channel_id: int, message_id: int, host_id: int,
                               prize: str, winner_count: int, ends_at, clone_id: Optional[int] = None,
                               role_requirement_id: Optional[int] = None) -> Dict:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO discord_giveaways
                    (guild_id, clone_id, channel_id, message_id, host_id, prize, winner_count, ends_at, role_requirement_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                RETURNING *
                """,
                guild_id, clone_id, channel_id, message_id, host_id, prize, winner_count, ends_at, role_requirement_id
            )
            return dict(row)

    async def get_giveaway_draft(self, wizard_message_id: int) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM discord_giveaway_drafts WHERE wizard_message_id = $1", wizard_message_id
            )
            return dict(row) if row else None

    async def upsert_giveaway_draft(self, wizard_message_id: int, guild_id: int, wizard_channel_id: int,
                                     invoker_id: int, clone_id: Optional[int] = None, **fields) -> Dict:
        """fields may include prize, duration_seconds, target_channel_id,
        winner_count, role_requirement_id. Creates the draft row on first
        call (from the wizard's initial post), merges on every subsequent
        call — same upsert-merge convention as the other wizards'
        set_*_config methods, just keyed by message instead of guild."""
        current = await self.get_giveaway_draft(wizard_message_id) or {
            "prize": None, "duration_seconds": None, "target_channel_id": None,
            "winner_count": 1, "role_requirement_id": None,
        }
        merged = {**current, **fields}
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO discord_giveaway_drafts
                    (wizard_message_id, guild_id, clone_id, wizard_channel_id, invoker_id,
                     prize, duration_seconds, target_channel_id, winner_count, role_requirement_id, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW())
                ON CONFLICT (wizard_message_id) DO UPDATE SET
                    prize = $6, duration_seconds = $7, target_channel_id = $8,
                    winner_count = $9, role_requirement_id = $10, updated_at = NOW()
                RETURNING *
                """,
                wizard_message_id, guild_id, clone_id, wizard_channel_id, invoker_id,
                merged["prize"], merged["duration_seconds"], merged["target_channel_id"],
                merged["winner_count"], merged["role_requirement_id"],
            )
            return dict(row)

    async def delete_giveaway_draft(self, wizard_message_id: int) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM discord_giveaway_drafts WHERE wizard_message_id = $1", wizard_message_id)

    async def get_giveaway_by_message(self, message_id: int) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM discord_giveaways WHERE message_id = $1", message_id)
            return dict(row) if row else None

    async def get_active_giveaways(self, clone_id: Optional[int] = None) -> List[Dict]:
        """Scoped to this process's clone_id (None = main bot) — every clone
        process runs its own poller, so an unscoped query would let two bot
        instances in the same guild both roll (possibly different) winners
        for the same giveaway."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM discord_giveaways WHERE status = 'active' AND COALESCE(clone_id, -1) = COALESCE($1, -1)",
                clone_id
            )
            return [dict(r) for r in rows]

    async def add_giveaway_entrant(self, message_id: int, user_id: int) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE discord_giveaways
                SET entrant_ids = ARRAY(SELECT DISTINCT unnest(entrant_ids || $2::BIGINT))
                WHERE message_id = $1
                RETURNING *
                """,
                message_id, user_id
            )
            return dict(row) if row else None

    async def remove_giveaway_entrant(self, message_id: int, user_id: int) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE discord_giveaways
                SET entrant_ids = array_remove(entrant_ids, $2::BIGINT)
                WHERE message_id = $1
                RETURNING *
                """,
                message_id, user_id
            )
            return dict(row) if row else None

    async def finish_giveaway(self, message_id: int, winner_ids: list) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE discord_giveaways SET status = 'ended', winner_ids = $2::BIGINT[] WHERE message_id = $1 RETURNING *",
                message_id, winner_ids
            )
            return dict(row) if row else None

    async def set_giveaway_winners(self, message_id: int, winner_ids: list) -> Optional[Dict]:
        """Used by /giveaway reroll — same as finish_giveaway but the row is
        already 'ended', kept separate so the intent at each call site is clear."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE discord_giveaways SET winner_ids = $2::BIGINT[] WHERE message_id = $1 RETURNING *",
                message_id, winner_ids
            )
            return dict(row) if row else None

    async def create_scheduled_message(self, guild_id: int, channel_id: int, content: str, next_run_at,
                                        interval_seconds: Optional[int], created_by: int, clone_id: Optional[int] = None) -> Dict:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO discord_scheduled_messages
                    (guild_id, clone_id, channel_id, content, next_run_at, interval_seconds, created_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING *
                """,
                guild_id, clone_id, channel_id, content, next_run_at, interval_seconds, created_by
            )
            return dict(row)

    async def list_scheduled_messages(self, guild_id: int, clone_id: Optional[int] = None) -> List[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM discord_scheduled_messages WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 ORDER BY next_run_at ASC",
                guild_id, clone_id
            )
            return [dict(r) for r in rows]

    async def get_due_scheduled_messages(self, clone_id: Optional[int] = None) -> List[Dict]:
        """Scoped to this process's clone_id (None = main bot) — every clone
        process runs its own poller, so an unscoped query would let two bot
        instances in the same guild both send the same scheduled message."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM discord_scheduled_messages
                WHERE enabled = TRUE AND next_run_at <= NOW()
                AND COALESCE(clone_id, -1) = COALESCE($1, -1)
                """,
                clone_id
            )
            return [dict(r) for r in rows]

    async def advance_or_disable_scheduled_message(self, message_id: int, next_run_at=None) -> None:
        """Pass next_run_at to reschedule a recurring message; omit it (None)
        to disable a one-off after it fires."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            if next_run_at is not None:
                await conn.execute(
                    "UPDATE discord_scheduled_messages SET next_run_at = $2 WHERE id = $1", message_id, next_run_at
                )
            else:
                await conn.execute(
                    "UPDATE discord_scheduled_messages SET enabled = FALSE WHERE id = $1", message_id
                )

    async def delete_scheduled_message(self, guild_id: int, message_id: int, clone_id: Optional[int] = None) -> bool:
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM discord_scheduled_messages WHERE id = $1 AND guild_id = $2 AND clone_id IS NOT DISTINCT FROM $3",
                message_id, guild_id, clone_id
            )
            return result is not None and "DELETE 0" not in result

    # ─────────────────────────────────────────────────────────────────────
    # Personal media server connections (Jellyfin / Google Drive)
    # ─────────────────────────────────────────────────────────────────────

    async def set_jellyfin_connection(self, user_id: int, server_url: str, api_key: str, jellyfin_user_id: str):
        pool = await get_pool()
        encrypted = secret_manager.encrypt(api_key)
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO jellyfin_connections (user_id, server_url, encrypted_api_key, jellyfin_user_id)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (user_id) DO UPDATE SET
                    server_url = EXCLUDED.server_url,
                    encrypted_api_key = EXCLUDED.encrypted_api_key,
                    jellyfin_user_id = EXCLUDED.jellyfin_user_id
            """, user_id, server_url, encrypted, jellyfin_user_id)

    async def get_jellyfin_connection(self, user_id: int) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM jellyfin_connections WHERE user_id = $1", user_id)
        if not row:
            return None
        d = dict(row)
        d["api_key"] = secret_manager.decrypt(d.pop("encrypted_api_key"))
        return d

    async def delete_jellyfin_connection(self, user_id: int):
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM jellyfin_connections WHERE user_id = $1", user_id)

    async def set_gdrive_connection(self, user_id: int, access_token: str, refresh_token: str,
                                     expires_at: datetime, folder_id: str, folder_name: str = None):
        pool = await get_pool()
        enc_access = secret_manager.encrypt(access_token)
        enc_refresh = secret_manager.encrypt(refresh_token)
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO gdrive_connections
                    (user_id, encrypted_access_token, encrypted_refresh_token, token_expires_at, folder_id, folder_name)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (user_id) DO UPDATE SET
                    encrypted_access_token = EXCLUDED.encrypted_access_token,
                    encrypted_refresh_token = EXCLUDED.encrypted_refresh_token,
                    token_expires_at = EXCLUDED.token_expires_at,
                    folder_id = EXCLUDED.folder_id,
                    folder_name = EXCLUDED.folder_name
            """, user_id, enc_access, enc_refresh, expires_at, folder_id, folder_name)

    async def get_gdrive_connection(self, user_id: int) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM gdrive_connections WHERE user_id = $1", user_id)
        if not row:
            return None
        d = dict(row)
        d["access_token"] = secret_manager.decrypt(d.pop("encrypted_access_token"))
        d["refresh_token"] = secret_manager.decrypt(d.pop("encrypted_refresh_token"))
        return d

    async def update_gdrive_access_token(self, user_id: int, access_token: str, expires_at: datetime):
        pool = await get_pool()
        enc = secret_manager.encrypt(access_token)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE gdrive_connections SET encrypted_access_token = $1, token_expires_at = $2 WHERE user_id = $3",
                enc, expires_at, user_id
            )

    async def delete_gdrive_connection(self, user_id: int):
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM gdrive_connections WHERE user_id = $1", user_id)

    async def create_gdrive_oauth_state(self, state: str, user_id: int):
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO gdrive_oauth_states (state, user_id) VALUES ($1, $2)", state, user_id
            )

    async def pop_gdrive_oauth_state(self, state: str) -> Optional[int]:
        """Consume a one-time OAuth state token, returning the Discord user_id it belongs to (or None)."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("DELETE FROM gdrive_oauth_states WHERE state = $1 RETURNING user_id", state)
        return row["user_id"] if row else None

    async def set_plex_connection(self, user_id: int, access_token: str, server_name: str, base_url: str):
        pool = await get_pool()
        encrypted = secret_manager.encrypt(access_token)
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO plex_connections (user_id, encrypted_access_token, server_name, base_url)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (user_id) DO UPDATE SET
                    encrypted_access_token = EXCLUDED.encrypted_access_token,
                    server_name = EXCLUDED.server_name,
                    base_url = EXCLUDED.base_url
            """, user_id, encrypted, server_name, base_url)

    async def get_plex_connection(self, user_id: int) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM plex_connections WHERE user_id = $1", user_id)
        if not row:
            return None
        d = dict(row)
        d["access_token"] = secret_manager.decrypt(d.pop("encrypted_access_token"))
        return d

    async def delete_plex_connection(self, user_id: int):
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM plex_connections WHERE user_id = $1", user_id)

    async def set_plex_pin_session(self, user_id: int, pin_id: int):
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO plex_pin_sessions (user_id, pin_id) VALUES ($1, $2)
                ON CONFLICT (user_id) DO UPDATE SET pin_id = EXCLUDED.pin_id, created_at = CURRENT_TIMESTAMP
            """, user_id, pin_id)

    async def get_plex_pin_session(self, user_id: int) -> Optional[int]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT pin_id FROM plex_pin_sessions WHERE user_id = $1", user_id)
        return row["pin_id"] if row else None

    async def delete_plex_pin_session(self, user_id: int):
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM plex_pin_sessions WHERE user_id = $1", user_id)

    async def is_media_connect_active(self, user_id: int) -> bool:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT status, expires_at FROM media_connect_subscriptions WHERE user_id = $1", user_id
            )
        if not row or row["status"] != "active":
            return False
        return row["expires_at"] is not None and row["expires_at"] > datetime.now(timezone.utc).replace(tzinfo=None)

    async def activate_media_connect_subscription(self, user_id: int, payment_reference: str, days: int = 30):
        pool = await get_pool()
        expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=days)
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO media_connect_subscriptions (user_id, status, payment_reference, activated_at, expires_at)
                VALUES ($1, 'active', $2, CURRENT_TIMESTAMP, $3)
                ON CONFLICT (user_id) DO UPDATE SET
                    status = 'active',
                    payment_reference = EXCLUDED.payment_reference,
                    activated_at = CURRENT_TIMESTAMP,
                    expires_at = EXCLUDED.expires_at
            """, user_id, payment_reference, expires_at)

    async def get_media_connect_subscription(self, user_id: int) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM media_connect_subscriptions WHERE user_id = $1", user_id)
        return dict(row) if row else None

    # ─────────────────────────────────────────────────────────────────────
    # Discord: welcome cards
    # ────────────────────��────────────────────────────────────────────────

    async def get_global_setting(self, key: str) -> Optional[str]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT value FROM bot_global_settings WHERE key = $1", key)
            return row["value"] if row else None

    async def set_global_setting(self, key: str, value: str) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO bot_global_settings (key, value, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()
                """,
                key, value,
            )

    async def get_welcome_config(self, guild_id: int, clone_id: Optional[int] = None) -> Dict:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM discord_welcome_config WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2",
                guild_id, clone_id
            )
            if row:
                return dict(row)
            return {
                "guild_id": guild_id, "clone_id": clone_id, "enabled": False, "channel_id": None,
                "message_template": "Welcome {member} to {guild}! You are member #{count}.",
                "background_color": "#2b2d31", "accent_color": "#5865F2",
                "sticker_url": "https://media1.tenor.com/m/m9knzx4hgYUAAAAC/party-excited.gif",
                "card_style": "gif",
                "avatar_shape": "circle",
                "nudge_sent_at": None, "nudge_status": None,
                "sticker_announced_at": None, "sticker_announce_status": None,
                # A guild with no row at all has never customized anything,
                # so it's safe (and desired) for it to get the new template
                # card straight away — only existing customized rows get
                # backfilled to False, above.
                "use_template": True,
                "template_announced_at": None, "template_announce_status": None,
                "delivery_mode": "channel",
                "wizard_channel_id": None, "wizard_message_id": None, "wizard_invoker_id": None,
                "card_theme": "wolf", "card_pack_unlocked": False,
                "ultra_pack_unlocked": False, "custom_background_url": None,
                "custom_bg_channel_id": None, "custom_bg_message_id": None,
            }

    async def set_welcome_config(self, guild_id: int, clone_id: Optional[int] = None, **fields) -> None:
        """fields may include any of: enabled, channel_id, message_template,
        background_color, accent_color, sticker_url, card_style, avatar_shape,
        use_template, delivery_mode. Upserts, so the first /welcome command in a guild works
        without a separate 'create' step. clone_id keeps a clone's welcome
        config separate from the main bot's (or another clone's) in a shared
        guild.

        An admin setting background_color/accent_color is a deliberate look
        customization — those two only render in flat-card mode, so if it
        comes in without an explicit use_template we drop back to the flat
        card, unconditionally, so the edit actually has a visible effect
        instead of silently doing nothing while the template stays on
        screen. (This is separate from the one-time backfill above, which
        DOES check template_announce_status — that guard is only about not
        undoing an owner's explicit "try the new look" click on every
        restart when their stored colors happen to still be non-default; a
        fresh edit here is a new, current signal and always wins.)
        avatar_shape is deliberately NOT included here: unlike colors, it
        IS honored by the template card (see modules/welcome_card.py's
        _draw_template_card), so setting it should never itself switch a
        guild off the wolf card. sticker_url/card_style are also excluded
        for the same reason as before — no visual effect in template mode,
        so setting/clearing a sticker shouldn't flip a guild off it either
        — e.g. the sticker-announcement DM's "Turn sticker off" button only
        touches sticker_url/card_style and must not silently switch a
        template-mode guild to the flat card."""
        current = await self.get_welcome_config(guild_id, clone_id)
        customizing = any(
            k in fields for k in ("background_color", "accent_color")
        )
        if customizing and "use_template" not in fields:
            fields = {**fields, "use_template": False}
        # Setting a custom background is the opposite signal from
        # background_color/accent_color above: it's a template-mode
        # feature (see modules/welcome_card.py's _draw_custom_bg_card), so
        # an explicit /welcome custombg call should switch a flat-card
        # guild INTO template mode, not leave it stuck on the flat card
        # with no visible effect. Clearing it back to None (custombg
        # clear) does NOT force a mode change either way — it just drops
        # back to whatever theme/card_theme was already set.
        if "custom_background_url" in fields and fields["custom_background_url"] and "use_template" not in fields:
            fields = {**fields, "use_template": True}
        merged = {**current, **fields}
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO discord_welcome_config
                    (guild_id, clone_id, enabled, channel_id, message_template, background_color, accent_color, sticker_url, card_style, avatar_shape, use_template, delivery_mode, card_theme, custom_background_url, custom_bg_channel_id, custom_bg_message_id, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, NOW())
                ON CONFLICT (guild_id, (COALESCE(clone_id, -1))) DO UPDATE SET
                    enabled = $3, channel_id = $4, message_template = $5,
                    background_color = $6, accent_color = $7, sticker_url = $8, card_style = $9,
                    avatar_shape = $10, use_template = $11, delivery_mode = $12, card_theme = $13,
                    custom_background_url = $14, custom_bg_channel_id = $15, custom_bg_message_id = $16,
                    updated_at = NOW()
                """,
                guild_id, clone_id, merged["enabled"], merged["channel_id"], merged["message_template"],
                merged["background_color"], merged["accent_color"], merged["sticker_url"], merged["card_style"],
                merged["avatar_shape"], merged["use_template"], merged.get("delivery_mode", "channel"),
                merged.get("card_theme", "wolf"), merged.get("custom_background_url"),
                merged.get("custom_bg_channel_id"), merged.get("custom_bg_message_id"),
            )

    async def unlock_welcome_card_pack(self, guild_id: int, clone_id: Optional[int] = None) -> None:
        """Marks the premium welcome-card pack as purchased for this guild
        (whole-guild, one-time — see card_pack_unlocked's schema comment
        above). Upserts the same way set_welcome_config does, so this works
        even if /welcome was never configured yet."""
        await self.set_welcome_config(guild_id, clone_id)  # ensure a row exists
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE discord_welcome_config SET card_pack_unlocked = TRUE, updated_at = NOW() "
                "WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2",
                guild_id, clone_id,
            )

    async def unlock_ultra_pack(self, guild_id: int, clone_id: Optional[int] = None) -> None:
        """Marks the ultra welcome pack (custom background via /welcome
        custombg) as purchased for this guild — same whole-guild, one-time
        shape as unlock_welcome_card_pack, just its own independent flag.
        Upserts a row first so this works even if /welcome was never
        configured yet."""
        await self.set_welcome_config(guild_id, clone_id)  # ensure a row exists
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE discord_welcome_config SET ultra_pack_unlocked = TRUE, updated_at = NOW() "
                "WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2",
                guild_id, clone_id,
            )

    async def set_welcome_wizard_pointer(
        self, guild_id: int, channel_id: Optional[int], message_id: Optional[int],
        invoker_id: Optional[int], clone_id: Optional[int] = None,
    ) -> None:
        """Records (or clears, when all three are None) where the most
        recently posted /welcome setup wizard message lives, so the 6
        standalone /welcome commands (enable/disable/message/colors/
        sticker/style) can push a live edit to it — see
        _views_welcome.refresh_posted_wizard. Upserts the same as
        set_welcome_config's ON CONFLICT so this never needs its own
        'has a row yet?' check; a guild with no row at all just gets one
        with everything else at its column default."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO discord_welcome_config (guild_id, clone_id, wizard_channel_id, wizard_message_id, wizard_invoker_id, updated_at)
                VALUES ($1, $2, $3, $4, $5, NOW())
                ON CONFLICT (guild_id, (COALESCE(clone_id, -1))) DO UPDATE SET
                    wizard_channel_id = $3, wizard_message_id = $4, wizard_invoker_id = $5, updated_at = NOW()
                """,
                guild_id, clone_id, channel_id, message_id, invoker_id,
            )

    async def mark_welcome_nudge_sent(self, guild_id: int, clone_id: Optional[int] = None) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE discord_welcome_config SET nudge_sent_at = NOW()
                WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2
                """,
                guild_id, clone_id,
            )

    async def set_welcome_nudge_status(self, guild_id: int, status: str, clone_id: Optional[int] = None) -> None:
        """status is 'approved' or 'denied' — recorded so a denial isn't
        re-nudged every cycle the way a never-asked guild is."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE discord_welcome_config SET nudge_status = $3
                WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2
                """,
                guild_id, clone_id, status,
            )

    async def mark_sticker_announcement_sent(self, guild_id: int, clone_id: Optional[int] = None) -> None:
        """Same pattern as mark_welcome_nudge_sent, for the separate
        'your already-enabled welcome cards can now have a sticker'
        one-time DM."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE discord_welcome_config SET sticker_announced_at = NOW()
                WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2
                """,
                guild_id, clone_id,
            )

    async def set_sticker_announce_status(self, guild_id: int, status: str, clone_id: Optional[int] = None) -> None:
        """status is 'acknowledged' or 'disabled' (owner turned the sticker
        off from the announcement DM itself)."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE discord_welcome_config SET sticker_announce_status = $3
                WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2
                """,
                guild_id, clone_id, status,
            )

    async def mark_template_announcement_sent(self, guild_id: int, clone_id: Optional[int] = None) -> None:
        """Same pattern as mark_sticker_announcement_sent, for the
        one-time 'try the new welcome card look' DM sent to guilds the
        use_template backfill kept on their old flat/customized card."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE discord_welcome_config SET template_announced_at = NOW()
                WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2
                """,
                guild_id, clone_id,
            )

    async def set_template_announce_status(self, guild_id: int, status: str, clone_id: Optional[int] = None) -> None:
        """status is 'tried' (owner switched to the template card from the
        announcement DM) or 'declined' (owner kept their existing card).
        Recording either way — not just 'tried' — is what stops
        set_welcome_config's customizing check from ever flipping
        use_template back off underneath an owner who explicitly chose to
        keep their flat card."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE discord_welcome_config SET template_announce_status = $3
                WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2
                """,
                guild_id, clone_id, status,
            )

    # --- /setup channels helpers ----------------------------------------

    async def get_leveling_config(self, guild_id: int, clone_id: Optional[int] = None) -> Dict:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM discord_leveling_config WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2",
                guild_id, clone_id
            )
            if row:
                return dict(row)
            return {
                "guild_id": guild_id, "clone_id": clone_id,
                "announce_channel_id": None, "announce_auto_created": False,
                "xp_rate": "default", "card_style": "card",
                "wizard_channel_id": None, "wizard_message_id": None, "wizard_invoker_id": None,
            }

    async def set_leveling_config(self, guild_id: int, clone_id: Optional[int] = None, **fields) -> None:
        """fields may include announce_channel_id, announce_auto_created,
        xp_rate, card_style, wizard_channel_id, wizard_message_id,
        wizard_invoker_id. Upserts, same pattern as set_welcome_config."""
        current = await self.get_leveling_config(guild_id, clone_id)
        merged = {**current, **fields}
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO discord_leveling_config
                    (guild_id, clone_id, announce_channel_id, announce_auto_created, xp_rate, card_style,
                     wizard_channel_id, wizard_message_id, wizard_invoker_id, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW())
                ON CONFLICT (guild_id, (COALESCE(clone_id, -1))) DO UPDATE SET
                    announce_channel_id = $3, announce_auto_created = $4, xp_rate = $5, card_style = $6,
                    wizard_channel_id = $7, wizard_message_id = $8, wizard_invoker_id = $9, updated_at = NOW()
                """,
                guild_id, clone_id, merged["announce_channel_id"], merged["announce_auto_created"], merged["xp_rate"],
                merged["card_style"], merged["wizard_channel_id"], merged["wizard_message_id"], merged["wizard_invoker_id"],
            )

    async def get_download_config(self, guild_id: int, clone_id: Optional[int] = None) -> Dict:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM discord_download_config WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2",
                guild_id, clone_id
            )
            if row:
                return dict(row)
            return {
                "guild_id": guild_id, "clone_id": clone_id,
                "channel_id": None, "channel_auto_created": False,
                "panel_channel_id": None, "panel_message_id": None,
                "wizard_channel_id": None, "wizard_message_id": None, "wizard_invoker_id": None,
            }

    async def set_download_config(self, guild_id: int, clone_id: Optional[int] = None, **fields) -> None:
        """fields may include channel_id, channel_auto_created,
        panel_channel_id, panel_message_id, wizard_channel_id,
        wizard_message_id, wizard_invoker_id. Upserts."""
        current = await self.get_download_config(guild_id, clone_id)
        merged = {**current, **fields}
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO discord_download_config
                    (guild_id, clone_id, channel_id, channel_auto_created,
                     panel_channel_id, panel_message_id,
                     wizard_channel_id, wizard_message_id, wizard_invoker_id, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW())
                ON CONFLICT (guild_id, (COALESCE(clone_id, -1))) DO UPDATE SET
                    channel_id = $3, channel_auto_created = $4,
                    panel_channel_id = $5, panel_message_id = $6,
                    wizard_channel_id = $7, wizard_message_id = $8, wizard_invoker_id = $9, updated_at = NOW()
                """,
                guild_id, clone_id, merged["channel_id"], merged["channel_auto_created"],
                merged["panel_channel_id"], merged["panel_message_id"],
                merged["wizard_channel_id"], merged["wizard_message_id"], merged["wizard_invoker_id"],
            )

    # --- Invite tracker (discord_bot/cogs/invites.py, _views_invites.py) ----

    async def get_invite_tracker_config(self, guild_id: int, clone_id: Optional[int] = None) -> Dict:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM discord_invite_tracker_config WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2",
                guild_id, clone_id
            )
            if row:
                return dict(row)
            return {
                "guild_id": guild_id, "clone_id": clone_id,
                "enabled": True, "channel_id": None, "channel_auto_created": False,
                "wizard_channel_id": None, "wizard_message_id": None, "wizard_invoker_id": None,
                "wizard_due_at": None,
            }

    async def set_invite_tracker_config(self, guild_id: int, clone_id: Optional[int] = None, **fields) -> None:
        """fields may include enabled, channel_id, channel_auto_created,
        wizard_channel_id, wizard_message_id, wizard_invoker_id,
        wizard_due_at. Upserts."""
        current = await self.get_invite_tracker_config(guild_id, clone_id)
        merged = {**current, **fields}
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO discord_invite_tracker_config
                    (guild_id, clone_id, enabled, channel_id, channel_auto_created,
                     wizard_channel_id, wizard_message_id, wizard_invoker_id, wizard_due_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW())
                ON CONFLICT (guild_id, (COALESCE(clone_id, -1))) DO UPDATE SET
                    enabled = $3, channel_id = $4, channel_auto_created = $5,
                    wizard_channel_id = $6, wizard_message_id = $7, wizard_invoker_id = $8,
                    wizard_due_at = $9, updated_at = NOW()
                """,
                guild_id, clone_id, merged["enabled"], merged["channel_id"], merged["channel_auto_created"],
                merged["wizard_channel_id"], merged["wizard_message_id"], merged["wizard_invoker_id"],
                merged["wizard_due_at"],
            )

    async def get_due_invite_wizard_guilds(self, clone_id: Optional[int] = None) -> list:
        """Guild ids whose scheduled auto-wizard post time has passed and
        which haven't had one posted yet (wizard_message_id still NULL —
        covers both "never posted" and "posted manually via /invites setup
        before the delay elapsed", either of which should cancel the
        pending auto-post). Powers InvitesCog._scheduler_loop."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT guild_id FROM discord_invite_tracker_config
                WHERE clone_id IS NOT DISTINCT FROM $1
                  AND wizard_due_at IS NOT NULL AND wizard_due_at <= NOW()
                  AND wizard_message_id IS NULL
                """,
                clone_id,
            )
            return [r["guild_id"] for r in rows]

    async def get_invite_cache(self, guild_id: int, clone_id: Optional[int] = None) -> Dict[str, Dict]:
        """invite_code -> {"uses": int, "inviter_id": int|None, "is_vanity": bool} —
        the baseline InvitesCog._handle_join diffs the next guild.invites()
        fetch against to figure out which invite a new member used."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT invite_code, uses, inviter_id, is_vanity FROM discord_invite_cache "
                "WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2",
                guild_id, clone_id
            )
            return {
                r["invite_code"]: {"uses": r["uses"], "inviter_id": r["inviter_id"], "is_vanity": r["is_vanity"]}
                for r in rows
            }

    async def replace_invite_cache(self, guild_id: int, clone_id: Optional[int], cache: Dict[str, Dict]) -> None:
        """Wholesale replace, not a merge — called after every full
        guild.invites() fetch so deleted/expired invites drop out of the
        cache instead of accumulating stale rows forever."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM discord_invite_cache WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2",
                    guild_id, clone_id
                )
                if cache:
                    await conn.executemany(
                        """
                        INSERT INTO discord_invite_cache
                            (guild_id, clone_id, invite_code, uses, inviter_id, is_vanity, updated_at)
                        VALUES ($1, $2, $3, $4, $5, $6, NOW())
                        """,
                        [
                            (guild_id, clone_id, code, data.get("uses", 0), data.get("inviter_id"), data.get("is_vanity", False))
                            for code, data in cache.items()
                        ],
                    )

    async def record_invite_join(
        self, guild_id: int, clone_id: Optional[int], member_id: int,
        inviter_id: Optional[int], invite_code: Optional[str],
    ) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO discord_invite_joins (guild_id, clone_id, member_id, inviter_id, invite_code)
                VALUES ($1, $2, $3, $4, $5)
                """,
                guild_id, clone_id, member_id, inviter_id, invite_code,
            )

    async def record_invite_leave(self, guild_id: int, clone_id: Optional[int], member_id: int) -> None:
        """Closes the most recent still-open join row for this member —
        this (not a delete) is what makes get_inviter_stats' "net" count
        resistant to the join-then-leave fake-invite trick: the row still
        exists for the all-time "joins" total, it's just excluded from
        the left_at IS NULL count that net is based on."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE discord_invite_joins SET left_at = NOW()
                WHERE id = (
                    SELECT id FROM discord_invite_joins
                    WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2
                      AND member_id = $3 AND left_at IS NULL
                    ORDER BY joined_at DESC LIMIT 1
                )
                """,
                guild_id, clone_id, member_id,
            )

    async def get_inviter_stats(self, guild_id: int, clone_id: Optional[int], inviter_id: int) -> tuple:
        """Returns (joins, net) for one inviter — joins is the all-time
        count credited to them, net subtracts anyone who's since left.
        The join announcement shows net; joins is exposed for anyone who
        also wants the raw total (e.g. an admin comparing the two)."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT COUNT(*) AS joins, COUNT(*) FILTER (WHERE left_at IS NULL) AS net
                FROM discord_invite_joins
                WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 AND inviter_id = $3
                """,
                guild_id, clone_id, inviter_id,
            )
            return (row["joins"] or 0, row["net"] or 0)

    async def get_invite_leaderboard(self, guild_id: int, clone_id: Optional[int], limit: int = 10) -> list:
        """[(inviter_id, joins, net), ...] ordered by net desc — powers the
        wizard's 🏆 Leaderboard button."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT inviter_id, COUNT(*) AS joins, COUNT(*) FILTER (WHERE left_at IS NULL) AS net
                FROM discord_invite_joins
                WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 AND inviter_id IS NOT NULL
                GROUP BY inviter_id
                ORDER BY net DESC, joins DESC
                LIMIT $3
                """,
                guild_id, clone_id, limit,
            )
            return [(r["inviter_id"], r["joins"], r["net"]) for r in rows]

    async def get_music_panel(self, guild_id: int, clone_id: Optional[int] = None) -> Dict:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM discord_music_panel WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2",
                guild_id, clone_id
            )
            if row:
                return dict(row)
            return {
                "guild_id": guild_id, "clone_id": clone_id,
                "panel_channel_id": None, "panel_message_id": None,
            }

    async def set_music_panel(self, guild_id: int, clone_id: Optional[int] = None, **fields) -> None:
        """fields may include panel_channel_id, panel_message_id. Upserts."""
        current = await self.get_music_panel(guild_id, clone_id)
        merged = {**current, **fields}
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO discord_music_panel
                    (guild_id, clone_id, panel_channel_id, panel_message_id, updated_at)
                VALUES ($1, $2, $3, $4, NOW())
                ON CONFLICT (guild_id, (COALESCE(clone_id, -1))) DO UPDATE SET
                    panel_channel_id = $3, panel_message_id = $4, updated_at = NOW()
                """,
                guild_id, clone_id, merged["panel_channel_id"], merged["panel_message_id"],
            )

    async def get_setup_suggestions(self, guild_id: int, clone_id: Optional[int] = None) -> Dict:
        """Row is created lazily on first read — callers don't need a
        separate 'create' step before recording a dismissal or custom
        name, same upsert-on-write convention as the config tables above."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM discord_setup_suggestions WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2",
                guild_id, clone_id
            )
            if row:
                d = dict(row)
                d["custom_names"] = json.loads(d["custom_names"]) if isinstance(d["custom_names"], str) else d["custom_names"]
                d["dismissed"] = json.loads(d["dismissed"]) if isinstance(d["dismissed"], str) else d["dismissed"]
                d["soft_channel_ids"] = json.loads(d["soft_channel_ids"]) if isinstance(d["soft_channel_ids"], str) else d["soft_channel_ids"]
                return d
            return {
                "guild_id": guild_id, "clone_id": clone_id, "category_id": None,
                "custom_names": {}, "dismissed": [], "soft_channel_ids": {},
            }

    async def set_setup_suggestions(self, guild_id: int, clone_id: Optional[int] = None, **fields) -> None:
        """fields may include category_id, custom_names, dismissed,
        soft_channel_ids. Upserts."""
        current = await self.get_setup_suggestions(guild_id, clone_id)
        merged = {**current, **fields}
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO discord_setup_suggestions
                    (guild_id, clone_id, category_id, custom_names, dismissed, soft_channel_ids, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, NOW())
                ON CONFLICT (guild_id, (COALESCE(clone_id, -1))) DO UPDATE SET
                    category_id = $3, custom_names = $4, dismissed = $5, soft_channel_ids = $6, updated_at = NOW()
                """,
                guild_id, clone_id, merged["category_id"], json.dumps(merged["custom_names"]),
                json.dumps(merged["dismissed"]), json.dumps(merged["soft_channel_ids"]),
            )

    async def dismiss_setup_suggestion(self, guild_id: int, key: str, clone_id: Optional[int] = None) -> None:
        current = await self.get_setup_suggestions(guild_id, clone_id)
        dismissed = set(current["dismissed"])
        dismissed.add(key)
        await self.set_setup_suggestions(guild_id, clone_id=clone_id, dismissed=sorted(dismissed))

    async def set_custom_channel_name(self, guild_id: int, key: str, name: str, clone_id: Optional[int] = None) -> None:
        current = await self.get_setup_suggestions(guild_id, clone_id)
        names = dict(current["custom_names"])
        names[key] = name
        await self.set_setup_suggestions(guild_id, clone_id=clone_id, custom_names=names)

    async def get_quickstart_dm(self, guild_id: int, clone_id: Optional[int] = None) -> Optional[dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM discord_quickstart_dm WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2",
                guild_id, clone_id
            )
            return dict(row) if row else None

    async def mark_quickstart_dm_sent(self, guild_id: int, clone_id: Optional[int] = None) -> None:
        """Called right after the initial join DM goes out (or is
        attempted). Upserts so a re-join doesn't error on the unique
        constraint — sent_at refreshes to the re-join time, followup stays
        whatever it already was so a re-join can't restart the one-shot
        follow-up clock."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO discord_quickstart_dm (guild_id, clone_id, sent_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (guild_id, (COALESCE(clone_id, -1))) DO UPDATE
                SET sent_at = NOW()
                """,
                guild_id, clone_id,
            )

    async def mark_quickstart_followup_sent(self, guild_id: int, clone_id: Optional[int] = None) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE discord_quickstart_dm SET followup_sent_at = NOW()
                WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2
                """,
                guild_id, clone_id,
            )

    async def mark_quickstart_followup_skipped(self, guild_id: int, clone_id: Optional[int] = None) -> None:
        """Set when the one-time follow-up check finds the guild already
        configured something — so it's recorded as handled (not sent) and
        the daily loop never looks at this guild again."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE discord_quickstart_dm SET followup_skipped = TRUE
                WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2
                """,
                guild_id, clone_id,
            )

    async def list_quickstart_followup_candidates(self, clone_id: Optional[int] = None, days: int = 3) -> list:
        """Guilds that got the initial DM at least `days` ago and have had
        neither a follow-up sent nor skipped yet."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT guild_id FROM discord_quickstart_dm
                WHERE clone_id IS NOT DISTINCT FROM $1
                  AND followup_sent_at IS NULL
                  AND followup_skipped IS FALSE
                  AND sent_at <= NOW() - ($2 || ' days')::INTERVAL
                """,
                clone_id, str(days),
            )
            return [r["guild_id"] for r in rows]

    async def set_join_dm_remind_later(self, guild_id: int, clone_id: Optional[int] = None, hours: int = 24) -> None:
        """'Remind me later' button on the combined owner join DM
        (bot.py's _send_combined_owner_join_dm) — schedules that same DM
        to resend once, `hours` from now. join_dm_reminder_loop polls for
        due rows and clears remind_at after sending.

        Upsert, not a plain UPDATE: the discord_quickstart_dm row is
        normally created earlier by mark_quickstart_dm_sent, but that only
        happens if QuickstartCog is loaded and its embed-building section
        didn't raise first. If no row exists yet, a plain UPDATE would
        silently match zero rows — the button would tell the owner
        "I'll send this again in a day" while saving nothing. The upsert
        makes this button correct regardless of whether that row already
        exists."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO discord_quickstart_dm (guild_id, clone_id, sent_at, remind_at)
                VALUES ($1, $2, NOW(), NOW() + ($3 || ' hours')::INTERVAL)
                ON CONFLICT (guild_id, (COALESCE(clone_id, -1))) DO UPDATE
                SET remind_at = NOW() + ($3 || ' hours')::INTERVAL
                """,
                guild_id, clone_id, str(hours),
            )

    async def set_join_dm_dismissed(self, guild_id: int, clone_id: Optional[int] = None) -> None:
        """'Don't ask again' button — permanently suppresses the combined
        owner join DM for this guild, including any pending remind-later.

        Upsert for the same reason as set_join_dm_remind_later above: this
        must take effect even if the discord_quickstart_dm row was never
        created (e.g. QuickstartCog didn't load, or its section errored
        before mark_quickstart_dm_sent ran), otherwise "Don't ask again"
        silently fails to stick and the owner keeps getting the DM."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO discord_quickstart_dm (guild_id, clone_id, sent_at, dismissed, remind_at)
                VALUES ($1, $2, NOW(), TRUE, NULL)
                ON CONFLICT (guild_id, (COALESCE(clone_id, -1))) DO UPDATE
                SET dismissed = TRUE, remind_at = NULL
                """,
                guild_id, clone_id,
            )

    async def is_join_dm_dismissed(self, guild_id: int, clone_id: Optional[int] = None) -> bool:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT dismissed FROM discord_quickstart_dm WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2",
                guild_id, clone_id,
            )
            return bool(row["dismissed"]) if row else False

    async def list_due_join_dm_reminders(self, clone_id: Optional[int] = None) -> list:
        """Guilds with a past-due remind_at that haven't since been
        dismissed — polled by join_dm_reminder_loop."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT guild_id FROM discord_quickstart_dm
                WHERE clone_id IS NOT DISTINCT FROM $1
                  AND remind_at IS NOT NULL AND remind_at <= NOW()
                  AND dismissed IS FALSE
                """,
                clone_id,
            )
            return [r["guild_id"] for r in rows]

    async def clear_join_dm_remind_at(self, guild_id: int, clone_id: Optional[int] = None) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE discord_quickstart_dm SET remind_at = NULL
                WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2
                """,
                guild_id, clone_id,
            )

    # ─────────────────────────────────────────────────────────────────────
    # Discord: economy game (Phase 3)
    # ─────────────────────────────────────────────────────────────────────

    _ECONOMY_CONFIG_DEFAULTS = {
        "currency_name": "Coins", "currency_symbol": "🪙",
        "daily_amount": 100, "work_min": 20, "work_max": 80,
        "beg_min": 1, "beg_max": 20,
        "rob_cooldown_hours": 6, "rob_success_chance": 40,
        "vote_bonus_enabled": False, "vote_bonus_amount": 200,
        "vote_cooldown_hours": 12, "vote_url": None,
        "ad_bonus_enabled": False, "ad_bonus_amount": 50, "ad_cooldown_hours": 4,
        "ad_embed_title": None, "ad_embed_description": None, "ad_embed_url": None,
        "wizard_channel_id": None, "wizard_message_id": None, "wizard_invoker_id": None,
    }

    async def get_economy_config(self, guild_id: int, clone_id: Optional[int] = None) -> Dict:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM discord_economy_config WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2",
                guild_id, clone_id
            )
            if row:
                return dict(row)
            return {"guild_id": guild_id, "clone_id": clone_id, **self._ECONOMY_CONFIG_DEFAULTS}

    async def set_economy_config(self, guild_id: int, clone_id: Optional[int] = None, **fields) -> None:
        """fields may include any key from _ECONOMY_CONFIG_DEFAULTS. Upserts,
        same convention as set_automod_config/set_welcome_config."""
        current = await self.get_economy_config(guild_id, clone_id)
        merged = {**current, **fields}
        cols = list(self._ECONOMY_CONFIG_DEFAULTS.keys())
        placeholders = ", ".join(f"${i+3}" for i in range(len(cols)))
        update_clause = ", ".join(f"{c} = ${i+3}" for i, c in enumerate(cols))
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO discord_economy_config (guild_id, clone_id, {", ".join(cols)}, updated_at)
                VALUES ($1, $2, {placeholders}, NOW())
                ON CONFLICT (guild_id, (COALESCE(clone_id, -1))) DO UPDATE SET
                    {update_clause}, updated_at = NOW()
                """,
                guild_id, clone_id, *[merged[c] for c in cols]
            )

    async def get_economy_balance(self, guild_id: int, user_id: int, clone_id: Optional[int] = None) -> Dict:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM discord_economy_balances WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 AND user_id = $3",
                guild_id, clone_id, user_id
            )
            if row:
                return dict(row)
            return {
                "guild_id": guild_id, "clone_id": clone_id, "user_id": user_id, "balance": 0,
                "last_daily_at": None, "last_work_at": None, "last_beg_at": None,
                "last_rob_at": None, "last_vote_bonus_at": None, "last_ad_bonus_at": None,
            }

    async def adjust_economy_balance(self, guild_id: int, user_id: int, amount: int, reason: str,
                                      clone_id: Optional[int] = None, cooldown_field: Optional[str] = None) -> int:
        """Applies `amount` (positive or negative) to a member's balance,
        optionally stamping a cooldown column (e.g. last_daily_at) in the
        same statement, and logs the change to discord_economy_transactions.
        Never lets a balance go negative (a failed /rob still costs the
        robber via a separate negative call, but a victim's balance is
        floored at 0 rather than allowed to go negative from someone else's
        action). Returns the new balance.

        The read-then-write of an earlier version of this method (fetch
        balance, compute max(0, balance+amount) in Python, then write that
        literal number) was a lost-update race: two concurrent calls for
        the same user (e.g. two /rob's landing on the same victim, or a
        /work firing while a /rob against that same player resolves) could
        both read the same starting balance and each write a result that
        ignores the other's change, silently duplicating or losing
        currency. The arithmetic now happens inside the UPSERT itself —
        GREATEST(0, ...balance + $4) is evaluated atomically under the
        row's lock, so concurrent adjustments compose correctly instead of
        clobbering each other. RETURNING gives back the true post-write
        balance instead of a value computed from a possibly-stale read.
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                if cooldown_field:
                    row = await conn.fetchrow(
                        f"""
                        INSERT INTO discord_economy_balances (guild_id, clone_id, user_id, balance, {cooldown_field})
                        VALUES ($1, $2, $3, GREATEST(0, $4), NOW())
                        ON CONFLICT (guild_id, (COALESCE(clone_id, -1)), user_id) DO UPDATE SET
                            balance = GREATEST(0, discord_economy_balances.balance + $4),
                            {cooldown_field} = NOW()
                        RETURNING balance
                        """,
                        guild_id, clone_id, user_id, amount
                    )
                else:
                    row = await conn.fetchrow(
                        """
                        INSERT INTO discord_economy_balances (guild_id, clone_id, user_id, balance)
                        VALUES ($1, $2, $3, GREATEST(0, $4))
                        ON CONFLICT (guild_id, (COALESCE(clone_id, -1)), user_id) DO UPDATE SET
                            balance = GREATEST(0, discord_economy_balances.balance + $4)
                        RETURNING balance
                        """,
                        guild_id, clone_id, user_id, amount
                    )
                new_balance = row["balance"]
                await conn.execute(
                    """
                    INSERT INTO discord_economy_transactions (guild_id, clone_id, user_id, amount, reason)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    guild_id, clone_id, user_id, amount, reason
                )
        return new_balance

    async def resolve_clone_id_by_bot_user_id(self, bot_user_id: int) -> Optional[int]:
        """Maps a Discord bot's own user ID (what a vote webhook payload
        identifies the voted-for bot by) to our internal clone_id. Returns
        None if bot_user_id is the main bot (caller compares against
        config.DISCORD_BOT_USER_ID first) or if it matches no known clone —
        callers must treat "no match" as "reject the webhook", not "assume
        main bot", since that would silently misattribute a clone's votes."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT clone_id FROM discord_cloned_bots WHERE bot_user_id = $1 AND status = 'active'",
                bot_user_id
            )
            return row["clone_id"] if row else None

    async def grant_vote_bonus_for_voter(self, user_id: int, clone_id: Optional[int], reason: str = "vote_bonus_webhook") -> List[Dict]:
        """Called from api/vote_webhook.py once a vote is verified. A vote
        is bot-wide, not guild-specific, so this credits the bonus in every
        guild (for this clone) where: the voter already has an economy
        balance row (i.e. has engaged with /daily, /work etc. there before —
        we have no gateway/member-cache access from this HTTP-only webhook
        process to discover *every* shared guild), vote_bonus_enabled is on,
        and that guild's vote cooldown has elapsed. Returns a list of
        {guild_id, amount, new_balance} for whichever guilds were credited,
        so the caller can log exactly what happened. This mirrors
        adjust_economy_balance's floor-at-0 + transaction-log behavior per
        guild rather than duplicating that logic here."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT b.guild_id, b.last_vote_bonus_at, c.vote_bonus_enabled,
                       c.vote_bonus_amount, c.vote_cooldown_hours
                FROM discord_economy_balances b
                JOIN discord_economy_config c
                  ON c.guild_id = b.guild_id AND c.clone_id IS NOT DISTINCT FROM b.clone_id
                WHERE b.user_id = $1 AND b.clone_id IS NOT DISTINCT FROM $2
                  AND c.vote_bonus_enabled = TRUE
                """,
                user_id, clone_id
            )
        credited = []
        for row in rows:
            cooldown_seconds = row["vote_cooldown_hours"] * 3600
            elapsed = (
                float("inf") if row["last_vote_bonus_at"] is None
                else (datetime.now(timezone.utc) - row["last_vote_bonus_at"].replace(tzinfo=timezone.utc)).total_seconds()
            )
            if elapsed < cooldown_seconds:
                continue
            new_balance = await self.adjust_economy_balance(
                row["guild_id"], user_id, row["vote_bonus_amount"], reason,
                clone_id=clone_id, cooldown_field="last_vote_bonus_at"
            )
            credited.append({"guild_id": row["guild_id"], "amount": row["vote_bonus_amount"], "new_balance": new_balance})
        return credited

    async def get_economy_leaderboard(self, guild_id: int, clone_id: Optional[int] = None, limit: int = 10) -> List[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT user_id, balance FROM discord_economy_balances
                WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2
                ORDER BY balance DESC LIMIT $3
                """,
                guild_id, clone_id, limit
            )
            return [dict(r) for r in rows]

    async def add_shop_item(self, guild_id: int, name: str, description: str, price: int, created_by: int,
                             role_id: Optional[int] = None, clone_id: Optional[int] = None) -> int:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO discord_economy_shop_items (guild_id, clone_id, name, description, price, role_id, created_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING item_id
                """,
                guild_id, clone_id, name, description, price, role_id, created_by
            )
            return row["item_id"]

    async def remove_shop_item(self, guild_id: int, item_id: int, clone_id: Optional[int] = None) -> bool:
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM discord_economy_shop_items WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 AND item_id = $3",
                guild_id, clone_id, item_id
            )
            return result != "DELETE 0"

    async def get_shop_items(self, guild_id: int, clone_id: Optional[int] = None) -> List[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM discord_economy_shop_items WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 ORDER BY price ASC",
                guild_id, clone_id
            )
            return [dict(r) for r in rows]

    async def get_shop_item(self, guild_id: int, item_id: int, clone_id: Optional[int] = None) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM discord_economy_shop_items WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 AND item_id = $3",
                guild_id, clone_id, item_id
            )
            return dict(row) if row else None

    # ─────────────────────────────────────────────────────────────────────
    # Discord: automation polish (Phase 4) — autoresponders + scheduled posts
    # ─────────────────────────────────────────────────────────────────────

    async def add_autoresponder(self, guild_id: int, trigger: str, response: str, created_by: int,
                                 clone_id: Optional[int] = None) -> int:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO discord_autoresponders (guild_id, clone_id, trigger, response, created_by)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
                """,
                guild_id, clone_id, trigger.lower(), response, created_by
            )
            return row["id"]

    async def remove_autoresponder(self, guild_id: int, autoresponder_id: int, clone_id: Optional[int] = None) -> bool:
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM discord_autoresponders WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 AND id = $3",
                guild_id, clone_id, autoresponder_id
            )
            return result != "DELETE 0"

    async def get_autoresponders(self, guild_id: int, clone_id: Optional[int] = None) -> List[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM discord_autoresponders WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 ORDER BY id ASC",
                guild_id, clone_id
            )
            return [dict(r) for r in rows]

    async def add_scheduled_announcement(self, guild_id: int, channel_id: int, message: str,
                                          next_run_at: datetime, created_by: int,
                                          interval_minutes: Optional[int] = None,
                                          clone_id: Optional[int] = None) -> int:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO discord_scheduled_announcements
                    (guild_id, clone_id, channel_id, message, interval_minutes, next_run_at, created_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id
                """,
                guild_id, clone_id, channel_id, message, interval_minutes, next_run_at, created_by
            )
            return row["id"]

    async def remove_scheduled_announcement(self, guild_id: int, announcement_id: int,
                                             clone_id: Optional[int] = None) -> bool:
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE discord_scheduled_announcements SET active = FALSE "
                "WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 AND id = $3",
                guild_id, clone_id, announcement_id
            )
            return result != "UPDATE 0"

    async def get_scheduled_announcements(self, guild_id: int, clone_id: Optional[int] = None) -> List[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM discord_scheduled_announcements "
                "WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2 AND active = TRUE ORDER BY next_run_at ASC",
                guild_id, clone_id
            )
            return [dict(r) for r in rows]

    async def get_due_announcements(self) -> List[Dict]:
        """Polled by api/cron_discord_announcements.py. Returns every active
        announcement whose next_run_at has passed, across all guilds/clones
        — the cron endpoint is the only caller, and it fans out per-row
        rather than per-guild since rows already carry their own
        guild_id/clone_id/channel_id."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM discord_scheduled_announcements WHERE active = TRUE AND next_run_at <= NOW()"
            )
            return [dict(r) for r in rows]

    # --- Owner broadcasts (DM every user across main bot + all Discord clones) ---

    async def get_discord_bot_user_ids(self, clone_id: Optional[int]) -> List[int]:
        """Every distinct user_id who has ever touched leveling or the
        economy system under this bot (clone_id=None means the main bot),
        i.e. our best proxy for "has used the bot" since there's no
        single global discord_users table. UNION across both source
        tables dedupes automatically."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT user_id FROM discord_xp WHERE clone_id IS NOT DISTINCT FROM $1
                UNION
                SELECT user_id FROM discord_economy_balances WHERE clone_id IS NOT DISTINCT FROM $1
                """,
                clone_id
            )
            return [r["user_id"] for r in rows]

    async def create_owner_broadcast(self, created_by: int, message: str, image_url: Optional[str] = None,
                                      payment_button_type: Optional[str] = None) -> int:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO discord_owner_broadcasts (created_by, message, image_url, payment_button_type) "
                "VALUES ($1, $2, $3, $4) RETURNING id",
                created_by, message, image_url, payment_button_type
            )
            return row["id"]

    async def add_owner_broadcast_recipients(self, broadcast_id: int, clone_id: Optional[int], user_ids: List[int]) -> None:
        if not user_ids:
            return
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.executemany(
                "INSERT INTO discord_owner_broadcast_recipients (broadcast_id, clone_id, user_id) VALUES ($1, $2, $3)",
                [(broadcast_id, clone_id, uid) for uid in user_ids]
            )
            await conn.execute(
                "UPDATE discord_owner_broadcasts SET total_recipients = total_recipients + $2 WHERE id = $1",
                broadcast_id, len(user_ids)
            )

    async def get_owner_broadcast(self, broadcast_id: int) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM discord_owner_broadcasts WHERE id = $1", broadcast_id)
            return dict(row) if row else None

    async def get_latest_owner_broadcast(self, created_by: int) -> Optional[Dict]:
        """Used by /broadcaststatus when no id is given — the most recent
        broadcast THIS owner queued, so one owner checking status doesn't
        surface another owner's broadcast by accident."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM discord_owner_broadcasts WHERE created_by = $1 ORDER BY id DESC LIMIT 1",
                created_by
            )
            return dict(row) if row else None

    async def get_pending_owner_broadcasts(self) -> List[Dict]:
        """Polled by api/cron_discord_owner_broadcast.py."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM discord_owner_broadcasts WHERE status = 'pending' ORDER BY id ASC"
            )
            return [dict(r) for r in rows]

    async def get_owner_broadcast_recipient_batch(self, broadcast_id: int, limit: int = 200) -> List[Dict]:
        """Atomically claims a batch of unsent recipients (oldest first) by
        stamping claimed_at, rather than a plain SELECT — a plain SELECT
        let two overlapping cron invocations both grab and process the
        same rows, double-sending and double-counting them. A claim older
        than 2 minutes is treated as abandoned (e.g. the process crashed
        mid-batch) and becomes claimable again, so a stuck row can't block
        the broadcast forever."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                UPDATE discord_owner_broadcast_recipients
                SET claimed_at = NOW()
                WHERE id IN (
                    SELECT id FROM discord_owner_broadcast_recipients
                    WHERE broadcast_id = $1 AND sent = FALSE
                    AND (claimed_at IS NULL OR claimed_at < NOW() - INTERVAL '2 minutes')
                    ORDER BY id ASC
                    LIMIT $2
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING *
                """,
                broadcast_id, limit
            )
            return [dict(r) for r in rows]

    async def release_owner_broadcast_recipient_claims(self, recipient_ids: List[int]) -> None:
        """Un-claims rows that get_owner_broadcast_recipient_batch claimed
        but that this tick didn't actually attempt to send (e.g. the batch
        spanned more clones than the per-clone send cap could get through).
        Resets claimed_at to NULL so the next cron tick can pick them up
        immediately instead of waiting out the 2-minute abandoned-claim
        window — without this, over-claimed rows sat idle needlessly,
        stretching how long an image broadcast takes to fully drain and
        increasing the odds it outlives Discord's ~24h CDN URL."""
        if not recipient_ids:
            return
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE discord_owner_broadcast_recipients SET claimed_at = NULL WHERE id = ANY($1::int[])",
                recipient_ids
            )

    async def mark_owner_broadcast_recipient_sent(self, recipient_id: int, error: Optional[str] = None) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE discord_owner_broadcast_recipients SET sent = TRUE, sent_at = NOW(), error = $2 WHERE id = $1",
                recipient_id, error
            )
            if error:
                await conn.execute(
                    "UPDATE discord_owner_broadcasts SET failed_count = failed_count + 1 WHERE id = "
                    "(SELECT broadcast_id FROM discord_owner_broadcast_recipients WHERE id = $1)",
                    recipient_id
                )
            else:
                await conn.execute(
                    "UPDATE discord_owner_broadcasts SET sent_count = sent_count + 1 WHERE id = "
                    "(SELECT broadcast_id FROM discord_owner_broadcast_recipients WHERE id = $1)",
                    recipient_id
                )

    async def finalize_owner_broadcast_if_done(self, broadcast_id: int) -> None:
        """Flips status to 'completed' once every recipient row has been
        attempted. Cheap to call after every batch since it's a no-op
        (WHERE clause won't match) until the last batch finishes."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE discord_owner_broadcasts SET status = 'completed', completed_at = NOW()
                WHERE id = $1 AND status = 'pending'
                AND NOT EXISTS (
                    SELECT 1 FROM discord_owner_broadcast_recipients
                    WHERE broadcast_id = $1 AND sent = FALSE
                )
                """,
                broadcast_id
            )

    async def mark_announcement_sent(self, announcement_id: int) -> None:
        """One-off (interval_minutes IS NULL) announcements are deactivated;
        repeating ones get next_run_at pushed forward by their interval so
        the cron endpoint won't pick them up again until it's actually due."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE discord_scheduled_announcements SET
                    active = (interval_minutes IS NOT NULL),
                    next_run_at = CASE
                        WHEN interval_minutes IS NOT NULL
                        THEN NOW() + (interval_minutes || ' minutes')::INTERVAL
                        ELSE next_run_at
                    END
                WHERE id = $1
                """,
                announcement_id
            )

    async def get_or_create_dashboard_token(self, guild_id: int, clone_id: Optional[int] = None) -> str:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT token FROM discord_dashboard_tokens WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2",
                guild_id, clone_id
            )
            if row:
                return row["token"]
            token = secrets.token_urlsafe(24)
            await conn.execute(
                "INSERT INTO discord_dashboard_tokens (guild_id, clone_id, token) VALUES ($1, $2, $3)",
                guild_id, clone_id, token
            )
            return token

    async def resolve_dashboard_token(self, token: str) -> Optional[Dict]:
        """Returns {guild_id, clone_id} for a valid dashboard token, or None.
        The token IS the auth — see discord_dashboard_tokens' comment in
        _create_tables for the trust model."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT guild_id, clone_id FROM discord_dashboard_tokens WHERE token = $1", token
            )
            return dict(row) if row else None

    async def add_submission(self, user_id: int, anime_name: str, episodes: int, genres: str, synopsis: str, image_url: str):
        """Add a new submission"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO submissions (user_id, anime_name, episodes, genres, synopsis, image_url, status)
                VALUES ($1, $2, $3, $4, $5, $6, 'pending')
                RETURNING submission_id
            """, user_id, anime_name, episodes, genres, synopsis, image_url)
            return row["submission_id"]

    async def get_pending_submissions(self) -> List[Dict]:
        """Get all pending submissions"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM submissions WHERE status = 'pending' ORDER BY created_date DESC
            """)
            return [{
                "submission_id": row["submission_id"],
                "user_id": row["user_id"],
                "anime_name": row["anime_name"],
                "episodes": row["episodes"],
                "genres": row["genres"],
                "synopsis": row["synopsis"],
                "image_url": row["image_url"],
                "status": row["status"],
                "created_date": row["created_date"]
            } for row in rows]

    async def approve_submission(self, submission_id: int):
        """Approve a submission"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE submissions SET status = 'approved', approved_date = CURRENT_TIMESTAMP
                WHERE submission_id = $1
            """, submission_id)

    async def reject_submission(self, submission_id: int, reason: str):
        """Reject a submission"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE submissions SET status = 'rejected', rejection_reason = $1
                WHERE submission_id = $2
            """, reason, submission_id)

    async def increment_clone_referral(self, clone_id: int) -> None:
        """
        Bump referral_count for a clone whose 'Get your own bot' button
        (keyboards.py main_menu) sent someone to /start=fromclone_<clone_id>.
        No-ops quietly (just logs) if clone_id doesn't exist — a stale or
        tampered deep link shouldn't ever raise into the /start handler.
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE cloned_bots SET referral_count = referral_count + 1
                WHERE clone_id = $1
            """, clone_id)
            if result == "UPDATE 0":
                logger.warning(f"[v0] Referral deep link pointed at unknown clone_id={clone_id}")

    async def add_cloned_bot(self, owner_id: int, bot_name: str, bot_token: str, webhook_url: str,
                              custom_data: Dict, payment_id: str = None, payment_status: str = "verified",
                              bot_username: str = None, webhook_secret: str = None):
        """
        Add a cloned bot with encrypted token AND encrypted webhook secret (Part 3.2 Step C).

        bot_token here must be the REAL, validated (getMe-confirmed) BotFather token.
        webhook_secret is the unique per-clone secret used for setWebhook's secret_token,
        stored encrypted so a DB leak doesn't hand out live webhook secrets either.
        """
        encrypted_token = secret_manager.encrypt(bot_token)
        encrypted_secret = secret_manager.encrypt(webhook_secret) if webhook_secret else None

        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO cloned_bots
                    (owner_id, bot_name, bot_token, webhook_url, custom_data,
                     payment_id, payment_status, bot_username, webhook_secret, status)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'active')
                RETURNING clone_id
            """, owner_id, bot_name, encrypted_token, webhook_url, json.dumps(custom_data),
                 payment_id, payment_status, bot_username, encrypted_secret)
            return row["clone_id"]

    async def get_user_clones(self, user_id: int) -> List[Dict]:
        """Get all active cloned bots for a user (with decrypted tokens - Issue 1.2)"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM cloned_bots WHERE owner_id = $1 AND status = 'active'
            """, user_id)
            result = []
            for row in rows:
                decrypted_token = secret_manager.decrypt(row["bot_token"])
                if decrypted_token is None:
                    logger.error(
                        f"[v0] Failed to decrypt bot_token for clone_id={row.get('clone_id')}, "
                        f"owner_id={user_id}. Excluding from results rather than returning ciphertext."
                    )
                    continue  # skip this clone rather than return an unusable/garbage token
                result.append({
                    "clone_id": row["clone_id"],
                    "owner_id": row["owner_id"],
                    "bot_name": row["bot_name"],
                    "bot_token": decrypted_token,
                    "bot_username": row["bot_username"],
                    "webhook_url": row["webhook_url"],
                    "custom_data": json.loads(row["custom_data"]) if row["custom_data"] else {},
                    "created_date": row["created_date"]
                })
            return result

    async def get_clone_for_routing(self, clone_id: int) -> Optional[Dict]:
        """
        Look up a single clone by clone_id for the api/bot.py webhook router
        (Part 3.2 Step D). Returns decrypted bot_token + webhook_secret so the
        caller can build an Application and verify the per-clone secret header.

        Returns None if the clone doesn't exist, isn't active, or either secret
        fails to decrypt (fail closed — never hand back ciphertext as if it
        were usable, same principle as the get_user_clones fix in round 2/3).
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM cloned_bots WHERE clone_id = $1 AND status = 'active'
            """, clone_id)
            if row is None:
                return None

            decrypted_token = secret_manager.decrypt(row["bot_token"])
            if decrypted_token is None:
                logger.error(f"[v0] Failed to decrypt bot_token for clone_id={clone_id}; refusing to route.")
                return None

            decrypted_secret = None
            if row["webhook_secret"]:
                decrypted_secret = secret_manager.decrypt(row["webhook_secret"])
                if decrypted_secret is None:
                    logger.error(f"[v0] Failed to decrypt webhook_secret for clone_id={clone_id}; refusing to route.")
                    return None

            return {
                "clone_id": row["clone_id"],
                "owner_id": row["owner_id"],
                "bot_name": row["bot_name"],
                "bot_token": decrypted_token,
                "bot_username": row["bot_username"],
                "webhook_secret": decrypted_secret,
                "custom_data": json.loads(row["custom_data"]) if row["custom_data"] else {},
                "status": row["status"],
            }

    async def list_active_clones(self) -> List[Dict]:
        """Admin tooling (Part 3.2 Step E): list all active clones, no secrets included."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT clone_id, owner_id, bot_name, bot_username, status, created_date
                FROM cloned_bots
                WHERE status = 'active'
                ORDER BY created_date DESC
            """)
            return [dict(row) for row in rows]

    async def upsert_bot_chat(self, chat_id: int, bot_status: str, chat_title: str = None,
                               chat_type: str = None, chat_username: str = None, clone_id: int = 0) -> None:
        """Record/refresh a chat the bot is (or was) a member of, with enough
        metadata (title/type/username) to show it in the admin panel's
        remote group/channel picker. Called from
        handlers/feature_handlers.handle_my_chat_member on every join/leave/
        promote/demote event. clone_id (0 = main bot) keeps every bot's
        membership rows separate, since the main bot and every clone share
        this same table/database — without it a clone would upsert into (and
        read back) rows that belong to the main bot or another clone."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO bot_group_membership (group_id, clone_id, bot_status, chat_title, chat_type, chat_username)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (group_id, clone_id) DO UPDATE SET
                    bot_status = EXCLUDED.bot_status,
                    chat_title = COALESCE(EXCLUDED.chat_title, bot_group_membership.chat_title),
                    chat_type = COALESCE(EXCLUDED.chat_type, bot_group_membership.chat_type),
                    chat_username = EXCLUDED.chat_username,
                    status_changed_at = CURRENT_TIMESTAMP
            """, chat_id, clone_id, bot_status, chat_title, chat_type, chat_username)

    async def list_bot_chats(self, clone_id: int = 0) -> List[Dict]:
        """All groups/channels THIS bot (main bot when clone_id=0, otherwise
        that specific clone) currently believes itself to be a member of, for
        the admin panel's remote group/channel picker (handlers/admin_remote.py).
        Scoped by clone_id so a clone never sees the main bot's or another
        clone's chats. Ordered by title so the picker is actually browsable
        once there are more than a handful."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT group_id AS chat_id, chat_title, chat_type, chat_username
                FROM bot_group_membership
                WHERE bot_status IN ('member', 'administrator', 'creator') AND clone_id = $1
                ORDER BY COALESCE(chat_title, '') ASC, group_id ASC
            """, clone_id)
            return [dict(row) for row in rows]

    async def list_all_clones(self) -> List[Dict]:
        """Used by the data-audit step (Part 3.3): every clone row, active or not, no secrets."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT clone_id, owner_id, bot_name, bot_username, status, payment_id, payment_status, created_date
                FROM cloned_bots
                ORDER BY created_date DESC
            """)
            return [dict(row) for row in rows]

    async def update_clone_custom_data(self, clone_id: int, owner_id: int, updates: Dict) -> bool:
        """
        Merge `updates` into a clone's custom_data JSON (e.g. {"name": "New Name"}).
        Only succeeds if owner_id actually owns this active clone — callers don't
        need to re-check ownership themselves. Also keeps the bot_name column in
        sync when "name" is one of the updated fields, since some queries read
        bot_name directly rather than through custom_data.
        Returns True if a row was updated, False if not found / not owned.
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT custom_data FROM cloned_bots
                WHERE clone_id = $1 AND owner_id = $2 AND status = 'active'
            """, clone_id, owner_id)
            if row is None:
                return False

            current = json.loads(row["custom_data"]) if row["custom_data"] else {}
            current.update(updates)

            if "name" in updates:
                await conn.execute("""
                    UPDATE cloned_bots SET custom_data = $1, bot_name = $2 WHERE clone_id = $3
                """, json.dumps(current), updates["name"], clone_id)
            else:
                await conn.execute("""
                    UPDATE cloned_bots SET custom_data = $1 WHERE clone_id = $2
                """, json.dumps(current), clone_id)
            return True

    async def set_clone_payment_provider(self, clone_id: int, owner_id: int, provider: str, api_key: Optional[str] = None) -> bool:
        """Set how a clone routes payments: 'main' (default — payments go
        through the main bot's own Paystack account) or 'paystack'/'stripe'
        with the owner's own key, encrypted at rest with the same cipher
        used for clone bot tokens. Passing provider='main' also wipes any
        previously stored key."""
        updates = {"payment_provider": provider}
        if provider == "main":
            updates["payment_key_encrypted"] = None
        elif api_key:
            updates["payment_key_encrypted"] = secret_manager.encrypt(api_key)
        return await self.update_clone_custom_data(clone_id, owner_id, updates)

    async def get_clone_payment_config(self, clone_id: int) -> Dict:
        """Returns {'provider': 'main'|'paystack'|'stripe', 'api_key': str|None}
        (api_key decrypted, or None if not connected / on 'main'). Used at
        payment-initiation time to decide whose gateway credentials to use —
        clone owners' own keys are never sent anywhere except back to their
        own gateway call."""
        clone = await self.get_clone_for_routing(clone_id)
        cd = (clone or {}).get("custom_data") or {}
        provider = cd.get("payment_provider", "main")
        encrypted = cd.get("payment_key_encrypted")
        api_key = secret_manager.decrypt(encrypted) if (provider != "main" and encrypted) else None
        return {"provider": provider if api_key or provider == "main" else "main", "api_key": api_key}

    # ── Discord: LEGACY single-tier-per-guild config (superseded) ───────────
    # Superseded by discord_premium_groups (see the "Discord: multiple
    # premium groups per guild" section above), which supports any number
    # of independently-priced groups instead of exactly one per guild.
    # discord_guild_premium is still created and its data still migrated
    # into discord_premium_groups on every cold start (see _create_tables),
    # but nothing in the app writes to it anymore — /createpremium,
    # /editpremium, etc. all go through the group-based functions instead.
    # Kept only for a clean rollback path; safe to drop this table and these
    # two functions once you're confident you won't need to revert.
    async def get_discord_guild_premium(self, guild_id: int) -> Optional[Dict]:
        """Returns {'guild_id', 'role_id', 'fee_ghs'} or None if this guild
        has no premium tier configured yet."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT guild_id, role_id, fee_ghs FROM discord_guild_premium WHERE guild_id = $1",
                guild_id
            )
            return dict(row) if row else None

    async def set_discord_guild_premium(self, guild_id: int, role_id: int = None, fee_ghs: float = None) -> None:
        """Upsert a guild's premium tier config. Pass only the fields you
        want to set/update — existing values are preserved via COALESCE."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO discord_guild_premium (guild_id, role_id, fee_ghs)
                VALUES ($1, $2, $3)
                ON CONFLICT (guild_id) DO UPDATE SET
                    role_id = COALESCE(EXCLUDED.role_id, discord_guild_premium.role_id),
                    fee_ghs = COALESCE(EXCLUDED.fee_ghs, discord_guild_premium.fee_ghs)
                """,
                guild_id, role_id, fee_ghs
            )

    async def log_manual_verify(self, admin_id: int, user_id: int, payment_type: str, chat_id: int, reason: str) -> None:
        """Audit trail for /verify (admin-only manual grant that bypasses
        payment entirely). Written to admin_action_log so there's a
        who/what/when/why record — required per the Discord port spec since
        this command can hand out free access."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO admin_action_log (admin_id, target_user_id, action, chat_id, reason, created_at)
                VALUES ($1, $2, $3, $4, $5, NOW())
                """,
                admin_id, user_id, f"manual_verify:{payment_type}", chat_id, reason
            )

    # ── Per-clone custom pricing ────────────────────────────────────────────
    # Clone owners can override the price of any feature listed in
    # config.PRICE_REGISTRY, but only while their monetization subscription
    # (see below) is active. Overrides live in custom_data->'pricing' (same
    # JSON-blob pattern as payment_provider), so no schema migration is
    # needed to add a new priceable feature — just add it to PRICE_REGISTRY.
    async def get_clone_prices(self, clone_id: int) -> Dict[str, float]:
        """Every PRICE_REGISTRY key resolved for this clone: the owner's own
        override where set, else the registry default. clone_id=0 (the main
        bot) always gets registry defaults — the main bot doesn't monetize
        itself the way a clone does."""
        from config import PRICE_REGISTRY
        prices = {k: v["default"] for k, v in PRICE_REGISTRY.items()}
        if not clone_id:
            return prices
        clone = await self.get_clone_for_routing(clone_id)
        cd = (clone or {}).get("custom_data") or {}
        overrides = cd.get("pricing") or {}
        for k, v in overrides.items():
            if k in prices:
                try:
                    prices[k] = float(v)
                except (TypeError, ValueError):
                    pass
        return prices

    async def get_clone_price(self, clone_id: int, key: str) -> float:
        """Single-key convenience wrapper around get_clone_prices(), used at
        the point of charging (e.g. handlers/subscription.py)."""
        from config import PRICE_REGISTRY
        prices = await self.get_clone_prices(clone_id)
        return prices.get(key, PRICE_REGISTRY.get(key, {}).get("default", 0))

    async def set_clone_price(self, clone_id: int, owner_id: int, key: str, amount: float) -> bool:
        """Persist one custom price override. Callers (handlers/clone_bot.py)
        must confirm is_monetization_active(clone_id) before calling this —
        it does not check the subscription itself, so it stays reusable for
        the expiry-sweep's revert-to-defaults path too."""
        from config import PRICE_REGISTRY
        if key not in PRICE_REGISTRY:
            return False
        clone = await self.get_clone_for_routing(clone_id)
        cd = (clone or {}).get("custom_data") or {}
        pricing = dict(cd.get("pricing") or {})
        pricing[key] = amount
        return await self.update_clone_custom_data(clone_id, owner_id, {"pricing": pricing})

    async def clear_clone_prices(self, clone_id: int, owner_id: int) -> bool:
        """Wipe all custom price overrides back to registry defaults — called
        when a clone's monetization subscription lapses, same principle as
        set_clone_payment_provider(..., 'main') reverting the gateway."""
        return await self.update_clone_custom_data(clone_id, owner_id, {"pricing": {}})

    # ── Clone monetization subscription ─────────────────────────────────────
    # Gates BOTH connecting a clone owner's own Paystack/Stripe key
    # (set_clone_payment_provider above) AND setting custom prices
    # (set_clone_price above) behind one recurring fee
    # (config.CLONE_MONETIZATION_FEE_GHS / month).
    async def get_monetization_subscription(self, clone_id: int) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM clone_monetization_subscriptions WHERE clone_id = $1",
                clone_id
            )
            return dict(row) if row else None

    async def is_monetization_active(self, clone_id: int) -> bool:
        if not clone_id:
            return False
        sub = await self.get_monetization_subscription(clone_id)
        if not sub or sub.get("status") != "active" or not sub.get("expires_at"):
            return False
        return sub["expires_at"] > datetime.now()

    async def start_monetization_payment(self, clone_id: int, owner_id: int, reference: str) -> None:
        """Called right after initializing the Paystack transaction for the
        activation fee, so a stale/duplicate webhook can be matched back to
        the right clone_id even if metadata is ever incomplete."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO clone_monetization_subscriptions (clone_id, owner_id, status, payment_reference)
                VALUES ($1, $2, 'pending', $3)
                ON CONFLICT (clone_id) DO UPDATE SET
                    owner_id = EXCLUDED.owner_id,
                    status = 'pending',
                    payment_reference = EXCLUDED.payment_reference
            """, clone_id, owner_id, reference)

    async def activate_monetization_subscription(self, clone_id: int, days: int = 30) -> None:
        """Called from the Paystack webhook once charge.success confirms the
        activation-fee payment. Renewing before expiry simply extends from
        now (not from the old expiry) — matches the simpler "renew = 30 more
        days from today" model used elsewhere in this codebase."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE clone_monetization_subscriptions
                SET status = 'active', activated_at = NOW(),
                    expires_at = NOW() + ($2 || ' days')::INTERVAL
                WHERE clone_id = $1
            """, clone_id, str(int(days)))

    async def expire_monetization_subscriptions(self) -> List[int]:
        """Cron sweep (api/cron_expire_monetization.py): any clone whose
        activation fee lapsed gets auto-reverted — payment provider back to
        'main', custom prices wiped back to registry defaults. This is the
        safe default: payments keep flowing (through the main account,
        which still nets the owner their commission split via
        commission_tracking) instead of silently breaking if a key or price
        is left in a stale state. Returns the clone_ids that were reverted."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT clone_id, owner_id FROM clone_monetization_subscriptions
                WHERE status = 'active' AND expires_at < NOW()
            """)
            reverted = []
            for r in rows:
                await conn.execute(
                    "UPDATE clone_monetization_subscriptions SET status = 'expired' WHERE clone_id = $1",
                    r["clone_id"]
                )
                await self.set_clone_payment_provider(r["clone_id"], r["owner_id"], "main")
                await self.clear_clone_prices(r["clone_id"], r["owner_id"])
                reverted.append(r["clone_id"])
            return reverted

    # ── Discord clone monetization (pricing + payment provider) ─────────────
    # Discord equivalent of the block above + get_clone_prices/get_clone_price/
    # set_clone_price/update_clone_custom_data/set_clone_payment_provider/
    # get_clone_payment_config, all of which hard-query cloned_bots (Telegram
    # only). These mirror them one-for-one against discord_cloned_bots +
    # discord_clone_monetization_subscriptions instead.

    async def get_discord_clone_for_owner(self, clone_id: int, owner_id: int) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM discord_cloned_bots WHERE clone_id = $1 AND owner_id = $2 AND status = 'active'",
                clone_id, owner_id
            )
            return dict(row) if row else None

    async def update_discord_clone_custom_data(self, clone_id: int, owner_id: int, updates: Dict) -> bool:
        """Same shape as update_clone_custom_data, targeting discord_cloned_bots.
        Only succeeds if owner_id actually owns this active clone."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT custom_data FROM discord_cloned_bots WHERE clone_id = $1 AND owner_id = $2 AND status = 'active'",
                clone_id, owner_id
            )
            if row is None:
                return False
            raw = row["custom_data"]
            cd = (json.loads(raw) if isinstance(raw, str) else dict(raw or {})) if raw else {}
            cd.update(updates)
            await conn.execute(
                "UPDATE discord_cloned_bots SET custom_data = $2 WHERE clone_id = $1",
                clone_id, json.dumps(cd)
            )
            return True

    async def get_discord_clone_prices(self, clone_id: int) -> Dict[str, float]:
        from config import PRICE_REGISTRY
        prices = {k: v["default"] for k, v in PRICE_REGISTRY.items()}
        if not clone_id:
            return prices
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT custom_data FROM discord_cloned_bots WHERE clone_id = $1", clone_id)
        raw = row["custom_data"] if row else None
        cd = (json.loads(raw) if isinstance(raw, str) else dict(raw or {})) if raw else {}
        overrides = cd.get("pricing") or {}
        for k, v in overrides.items():
            if k in prices:
                try:
                    prices[k] = float(v)
                except (TypeError, ValueError):
                    pass
        return prices

    async def get_discord_clone_price(self, clone_id: int, key: str) -> float:
        from config import PRICE_REGISTRY
        prices = await self.get_discord_clone_prices(clone_id)
        return prices.get(key, PRICE_REGISTRY.get(key, {}).get("default", 0))

    async def set_discord_clone_price(self, clone_id: int, owner_id: int, key: str, amount: float) -> bool:
        from config import PRICE_REGISTRY
        if key not in PRICE_REGISTRY:
            return False
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT custom_data FROM discord_cloned_bots WHERE clone_id = $1", clone_id)
        raw = row["custom_data"] if row else None
        cd = (json.loads(raw) if isinstance(raw, str) else dict(raw or {})) if raw else {}
        pricing = dict(cd.get("pricing") or {})
        pricing[key] = amount
        return await self.update_discord_clone_custom_data(clone_id, owner_id, {"pricing": pricing})

    async def clear_discord_clone_prices(self, clone_id: int, owner_id: int) -> bool:
        return await self.update_discord_clone_custom_data(clone_id, owner_id, {"pricing": {}})

    async def set_discord_clone_payment_provider(self, clone_id: int, owner_id: int, provider: str, api_key: Optional[str] = None) -> bool:
        """Sets which provider NEW charges for this clone should use.

        Each provider's key is stored in its OWN slot (payment_key_paystack
        / payment_key_stripe) rather than one shared slot — switching
        providers used to overwrite a single key field, which meant
        switching back later required re-entering the key, AND meant an
        in-flight payment on the old provider couldn't be verified anymore
        the moment the owner switched (verify needs the same key that
        created the reference). Keeping both slots means switching is
        non-destructive and a pending payment stays verifiable regardless
        of what the owner does with their settings afterward — see
        get_discord_clone_provider_key(), which verify flows should use
        instead of re-reading whatever's "current."
        """
        updates = {"payment_provider": provider}
        if provider != "main" and api_key:
            updates[f"payment_key_{provider}"] = secret_manager.encrypt(api_key)
        return await self.update_discord_clone_custom_data(clone_id, owner_id, updates)

    async def get_discord_clone_payment_config(self, clone_id: int) -> Dict:
        """The provider + key NEW charges for this clone should use right
        now. For verifying a specific already-started payment, use
        get_discord_clone_provider_key(clone_id, provider) instead — the
        owner may have switched providers since that payment began."""
        if not clone_id:
            return {"provider": "main", "api_key": None}
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT custom_data FROM discord_cloned_bots WHERE clone_id = $1", clone_id)
        raw = row["custom_data"] if row else None
        cd = (json.loads(raw) if isinstance(raw, str) else dict(raw or {})) if raw else {}
        provider = cd.get("payment_provider", "main")
        api_key = self._decrypt_discord_clone_provider_key(cd, provider) if provider != "main" else None
        return {"provider": provider if api_key or provider == "main" else "main", "api_key": api_key}

    @staticmethod
    def _decrypt_discord_clone_provider_key(cd: Dict, provider: str) -> Optional[str]:
        """Looks up `provider`'s key from its own slot (payment_key_<provider>),
        falling back to the legacy single-slot field (payment_key_encrypted)
        for clones connected before per-provider slots existed — that old
        field only ever held whichever provider was active at the time, so
        it's only a valid fallback when payment_provider still matches."""
        encrypted = cd.get(f"payment_key_{provider}")
        if not encrypted and cd.get("payment_provider") == provider:
            encrypted = cd.get("payment_key_encrypted")  # pre-migration clones
        return secret_manager.decrypt(encrypted) if encrypted else None

    async def get_discord_clone_provider_key(self, clone_id: int, provider: str) -> Optional[str]:
        """The key stored for `provider` specifically, regardless of which
        provider is currently active for this clone. Use this (not
        get_discord_clone_payment_config) when verifying a payment that
        was already started under a known provider — e.g. payment_logs'
        stored `provider` column — so switching payment settings mid-flight
        can't break an in-progress checkout."""
        if not clone_id or provider == "main":
            return None
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT custom_data FROM discord_cloned_bots WHERE clone_id = $1", clone_id)
        raw = row["custom_data"] if row else None
        cd = (json.loads(raw) if isinstance(raw, str) else dict(raw or {})) if raw else {}
        return self._decrypt_discord_clone_provider_key(cd, provider)

    async def get_discord_monetization_subscription(self, clone_id: int) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM discord_clone_monetization_subscriptions WHERE clone_id = $1", clone_id
            )
            return dict(row) if row else None

    async def is_discord_monetization_active(self, clone_id: int) -> bool:
        if not clone_id:
            return False
        sub = await self.get_discord_monetization_subscription(clone_id)
        if not sub or sub.get("status") != "active" or not sub.get("expires_at"):
            return False
        return sub["expires_at"] > datetime.now()

    async def start_discord_monetization_payment(self, clone_id: int, owner_id: int, reference: str) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO discord_clone_monetization_subscriptions (clone_id, owner_id, status, payment_reference)
                VALUES ($1, $2, 'pending', $3)
                ON CONFLICT (clone_id) DO UPDATE SET
                    owner_id = EXCLUDED.owner_id,
                    status = 'pending',
                    payment_reference = EXCLUDED.payment_reference
            """, clone_id, owner_id, reference)

    async def activate_discord_monetization_subscription(self, clone_id: int, days: int = 30) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE discord_clone_monetization_subscriptions
                SET status = 'active', activated_at = NOW(),
                    expires_at = NOW() + ($2 || ' days')::INTERVAL
                WHERE clone_id = $1
            """, clone_id, str(int(days)))

    async def activate_discord_monetization_subscription_by_reference(self, reference: str, days: int = 30) -> Optional[int]:
        """Webhook backstop for /clonemonetize activate — mirrors
        activate_discord_monetization_subscription but looks the row up by
        payment_reference instead of trusting an in-memory pending dict, so
        a payment still activates even if the user never taps "Verify
        Payment" (or the bot restarted before they did). Only activates a
        row still 'pending' with a matching reference, so it can't
        re-activate/extend an already-completed or unrelated row. Returns
        the clone_id activated, or None if no matching pending row exists."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                UPDATE discord_clone_monetization_subscriptions
                SET status = 'active', activated_at = NOW(),
                    expires_at = NOW() + ($2 || ' days')::INTERVAL
                WHERE payment_reference = $1 AND status = 'pending'
                RETURNING clone_id
            """, reference, str(int(days)))
            return row["clone_id"] if row else None

    async def expire_discord_monetization_subscriptions(self) -> List[int]:
        """Cron-sweep parity with expire_monetization_subscriptions — wire
        this into api/cron_expire_monetization.py alongside the Telegram
        sweep if/when you want auto-revert-to-defaults on Discord too."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT clone_id, owner_id FROM discord_clone_monetization_subscriptions
                WHERE status = 'active' AND expires_at < NOW()
            """)
            reverted = []
            for r in rows:
                await conn.execute(
                    "UPDATE discord_clone_monetization_subscriptions SET status = 'expired' WHERE clone_id = $1",
                    r["clone_id"]
                )
                await self.set_discord_clone_payment_provider(r["clone_id"], r["owner_id"], "main")
                await self.clear_discord_clone_prices(r["clone_id"], r["owner_id"])
                reverted.append(r["clone_id"])
            return reverted

    # ── Yandex direct-search subscription (per-user, per-clone) ─────────────
    async def get_image_search_yandex_subscription(self, user_id: int, clone_id: int = 0) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM image_search_yandex_subscriptions WHERE user_id = $1 AND clone_id = $2",
                user_id, clone_id
            )
            return dict(row) if row else None

    async def is_image_search_yandex_active(self, user_id: int, clone_id: int = 0) -> bool:
        sub = await self.get_image_search_yandex_subscription(user_id, clone_id)
        if not sub or sub.get("status") != "active" or not sub.get("expires_at"):
            return False
        return sub["expires_at"] > datetime.now()

    async def start_image_search_yandex_payment(self, user_id: int, clone_id: int, reference: str, provider: str = "paystack") -> None:
        """provider is stored so verify (in-app Verify tap, the webhook
        backstop, or a post-restart re-check) reads the SAME provider's key
        this payment was created under, via payments.resolve_gateway_for_provider,
        rather than whatever the clone's payment settings say right now."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO image_search_yandex_subscriptions (user_id, clone_id, status, payment_reference, provider)
                VALUES ($1, $2, 'pending', $3, $4)
                ON CONFLICT (user_id, clone_id) DO UPDATE SET
                    status = 'pending',
                    payment_reference = EXCLUDED.payment_reference,
                    provider = EXCLUDED.provider
            """, user_id, clone_id, reference, provider)

    async def activate_image_search_yandex_subscription(self, user_id: int, clone_id: int = 0, days: int = 30) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE image_search_yandex_subscriptions
                SET status = 'active', activated_at = NOW(),
                    expires_at = NOW() + ($3 || ' days')::INTERVAL
                WHERE user_id = $1 AND clone_id = $2
            """, user_id, clone_id, str(int(days)))

    async def activate_image_search_yandex_subscription_by_reference(self, reference: str, days: int = 30) -> Optional[Dict]:
        """Webhook backstop for the Yandex direct-search subscription —
        mirrors activate_image_search_yandex_subscription but looks the row
        up by payment_reference (set by start_image_search_yandex_payment)
        rather than requiring the user to tap "Verify Payment" in-app.
        Only activates a row still 'pending' with a matching reference.
        Returns {"user_id", "clone_id"} of the row activated, or None if no
        matching pending row exists."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                UPDATE image_search_yandex_subscriptions
                SET status = 'active', activated_at = NOW(),
                    expires_at = NOW() + ($2 || ' days')::INTERVAL
                WHERE payment_reference = $1 AND status = 'pending'
                RETURNING user_id, clone_id
            """, reference, str(int(days)))
            return dict(row) if row else None

    async def save_image_search_yandex_authorization(self, user_id: int, clone_id: int, authorization_code: str) -> None:
        """Stores the reusable-card token from the first successful payment so
        auto_renew_image_search_yandex_subscriptions() can charge it again at
        the next billing cycle without the user re-entering card details."""
        if not authorization_code:
            return
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE image_search_yandex_subscriptions
                SET authorization_code = $3
                WHERE user_id = $1 AND clone_id = $2
            """, user_id, clone_id, authorization_code)

    async def expire_image_search_yandex_subscriptions(self) -> None:
        """Cron sweep companion to expire_monetization_subscriptions — just
        flips lapsed rows to 'expired', nothing else to revert since this
        subscription doesn't grant anything besides the direct-link button."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE image_search_yandex_subscriptions
                SET status = 'expired'
                WHERE status = 'active' AND expires_at < NOW()
            """)

    async def get_image_search_yandex_due_for_renewal(self) -> List[Dict]:
        """Active subscriptions with a saved card that expire within the next
        24 hours — the renewal window for the daily cron sweep."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT user_id, clone_id, authorization_code
                FROM image_search_yandex_subscriptions
                WHERE status = 'active'
                  AND authorization_code IS NOT NULL
                  AND expires_at < NOW() + INTERVAL '1 day'
            """)
            return [dict(r) for r in rows]
    async def mark_image_search_yandex_renewal_failed(self, user_id: int, clone_id: int) -> None:
        """Auto-charge failed (card declined/expired) — drop to 'expired' and
        clear the dead authorization_code so we stop retrying a card that
        won't work; the user has to subscribe again manually."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE image_search_yandex_subscriptions
                SET status = 'expired', authorization_code = NULL
                WHERE user_id = $1 AND clone_id = $2
            """, user_id, clone_id)

    async def cancel_image_search_yandex_autorenew(self, user_id: int, clone_id: int) -> None:
        """User-initiated cancel: stop future auto-charges but leave the
        subscription active until its current expires_at (standard 'cancel =
        no renewal, access continues until period end' behavior)."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE image_search_yandex_subscriptions
                SET authorization_code = NULL
                WHERE user_id = $1 AND clone_id = $2
            """, user_id, clone_id)

    async def start_image_search_unlock_payment(self, user_id: int, clone_id: int, reference: str, results: list, provider: str = "paystack") -> None:
        """Persists the pending "unlock source links" payment so the
        webhook (api/paystack_webhook.py's 'image_search_unlock' case) or a
        post-restart re-check of /imagesearch's Verify button can complete
        it even if the in-memory RevealView/VerifyUnlockView state on the
        cog is gone. results is stored so the links can still be delivered
        without re-running the reverse image search. provider is stored so
        a later verify (webhook or restart fallback) reads the SAME
        provider's key the payment was created under, via
        payments.resolve_gateway_for_provider, rather than whatever the
        clone's payment settings say right now."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO discord_image_search_unlock_payments
                    (user_id, clone_id, payment_reference, status, results_json, provider)
                VALUES ($1, $2, $3, 'pending', $4, $5)
                ON CONFLICT (payment_reference) DO NOTHING
            """, user_id, clone_id, reference, json.dumps(results), provider)

    async def get_image_search_unlock_payment(self, reference: str) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM discord_image_search_unlock_payments WHERE payment_reference = $1",
                reference
            )
            if not row:
                return None
            d = dict(row)
            if d.get("results_json"):
                d["results"] = json.loads(d["results_json"])
            return d

    async def get_image_search_unlock_payment_for_user(self, user_id: int) -> Optional[Dict]:
        """Most recent row for this user, regardless of status — used as
        the restart-safe fallback in handle_verify_unlock_button when the
        cog's in-memory pending dict has been lost."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM discord_image_search_unlock_payments WHERE user_id = $1 ORDER BY id DESC LIMIT 1",
                user_id
            )
            if not row:
                return None
            d = dict(row)
            d["results"] = json.loads(d["results_json"]) if d.get("results_json") else []
            return d

    async def complete_image_search_unlock_payment(self, reference: str) -> Optional[Dict]:
        """Webhook backstop: marks the pending row 'completed' server-to-
        server. Only fires for a row still 'pending' with a matching
        reference, so it can't re-fire on a row already completed via the
        in-app Verify button. Returns {"user_id", "clone_id", "results"} for
        the caller to best-effort DM the paid-for links, or None if no
        matching pending row exists."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                UPDATE discord_image_search_unlock_payments
                SET status = 'completed', completed_at = NOW()
                WHERE payment_reference = $1 AND status = 'pending'
                RETURNING user_id, clone_id, provider, results_json
            """, reference)
            if not row:
                return None
            d = dict(row)
            d["results"] = json.loads(d["results_json"]) if d.get("results_json") else []
            return d

    async def deactivate_clone(self, clone_id: int) -> Optional[Dict]:
        """
        Mark a clone inactive in the DB. Returns the pre-deactivation row (with
        decrypted token) so the caller can also call deleteWebhook on Telegram's
        side — this method only touches the database, it does not call Telegram.
        """
        clone = await self.get_clone_for_routing(clone_id)
        if clone is None:
            return None
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE cloned_bots SET status = 'inactive' WHERE clone_id = $1
            """, clone_id)
        return clone

    async def store_pending_clone_payment(self, user_id: int, payment_reference: str):
        """Store pending clone payment in clone_payments table"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO clone_payments (reference, user_id, status)
                VALUES ($1, $2, 'pending')
                ON CONFLICT (reference) DO NOTHING
            """, payment_reference, user_id)

    async def mark_clone_payment_paid(self, payment_reference: str):
        """Mark clone payment as paid when webhook confirms (Task 1)"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE clone_payments 
                SET status = 'paid'
                WHERE reference = $1
            """, payment_reference)

    async def get_clone_payment_status(self, payment_reference: str) -> str:
        """Get clone payment status from database"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            status = await conn.fetchval("""
                SELECT status FROM clone_payments WHERE reference = $1
            """, payment_reference)
            return status or "not_found"

    async def add_anime(self, title: str, episodes: int, genres: str, rating: float, status: str, synopsis: str, image_url: str, anilist_id: Optional[int] = None, mal_id: Optional[int] = None):
        """Add anime entry"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO anime_entries (anilist_id, mal_id, title, episodes, genres, rating, status, synopsis, image_url, source_api)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'mixed')
                RETURNING anime_id
            """, anilist_id, mal_id, title, episodes, genres, rating, status, synopsis, image_url)
            return row["anime_id"]

    async def search_anime(self, query: str, limit: int = 5) -> List[Dict]:
        """Search anime by title"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM anime_entries WHERE title ILIKE $1 LIMIT $2
            """, f"%{query}%", limit)
            return [{
                "anime_id": row["anime_id"],
                "title": row["title"],
                "episodes": row["episodes"],
                "genres": row["genres"],
                "rating": row["rating"],
                "status": row["status"],
                "synopsis": row["synopsis"],
                "image_url": row["image_url"]
            } for row in rows]

    async def update_config(self, key: str, value: Any) -> bool:
        """Update admin configuration - persists to database"""
        pool = await get_pool()
        try:
            async with pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO admin_config (key, value, updated_at)
                    VALUES ($1, $2, NOW())
                    ON CONFLICT (key) DO UPDATE SET
                        value = EXCLUDED.value,
                        updated_at = NOW()
                """, key, str(value))
            return True
        except Exception as e:
            print(f"[v0] Error updating config: {e}")
            return False

    async def get_config(self, key: str) -> Optional[str]:
        """Get admin configuration value from database"""
        pool = await get_pool()
        try:
            async with pool.acquire() as conn:
                value = await conn.fetchval(
                    "SELECT value FROM admin_config WHERE key = $1", key
                )
            return value
        except Exception as e:
            print(f"[v0] Error reading config: {e}")
            return None

    # ─────────────────────────────────────────────────────────────────
    # AI Store — credit wallet, sessions, listings, refunds, rate limits
    # ─────────────────────────────────────────────────────────────────

    async def ai_store_get_balance(self, user_id: int) -> float:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO ai_store_wallets (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING", user_id
            )
            balance = await conn.fetchval("SELECT credits FROM ai_store_wallets WHERE user_id = $1", user_id)
            return float(balance)

    async def ai_store_add_credits(self, user_id: int, amount: float, tx_type: str = "topup", meta: dict = None) -> float:
        """Atomic credit add (topup/refund). Returns new balance."""
        import json as _json
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO ai_store_wallets (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING", user_id
                )
                await conn.execute(
                    "UPDATE ai_store_wallets SET credits = credits + $2 WHERE user_id = $1", user_id, amount
                )
                balance = await conn.fetchval("SELECT credits FROM ai_store_wallets WHERE user_id = $1", user_id)
                await conn.execute(
                    "INSERT INTO ai_store_transactions (user_id, type, amount, balance_after, meta) "
                    "VALUES ($1, $2, $3, $4, $5)",
                    user_id, tx_type, amount, balance, _json.dumps(meta or {}),
                )
                return float(balance)

    async def ai_store_debit_credits(self, user_id: int, amount: float, meta: dict = None) -> float:
        """Atomic debit. Raises InsufficientCreditsError if balance too low —
        caller must catch it and block the AI call BEFORE spending API cost."""
        import json as _json
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO ai_store_wallets (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING", user_id
                )
                current = await conn.fetchval(
                    "SELECT credits FROM ai_store_wallets WHERE user_id = $1 FOR UPDATE", user_id
                )
                if float(current) < amount:
                    raise InsufficientCreditsError(amount, float(current))
                await conn.execute(
                    "UPDATE ai_store_wallets SET credits = credits - $2 WHERE user_id = $1", user_id, amount
                )
                balance = float(current) - amount
                await conn.execute(
                    "INSERT INTO ai_store_transactions (user_id, type, amount, balance_after, meta) "
                    "VALUES ($1, 'debit', $2, $3, $4)",
                    user_id, amount, balance, _json.dumps(meta or {}),
                )
                return balance

    async def ai_store_get_history(self, user_id: int, limit: int = 10) -> List[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM ai_store_transactions WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2",
                user_id, limit,
            )
            return [dict(r) for r in rows]

    async def ai_store_create_session(self, user_id: int, guild_id: Optional[int], provider: str, model: str,
                                       listing_id: Optional[int] = None) -> int:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("UPDATE ai_store_sessions SET active = FALSE WHERE user_id = $1 AND active = TRUE", user_id)
            row = await conn.fetchrow(
                "INSERT INTO ai_store_sessions (user_id, guild_id, provider, model, listing_id) "
                "VALUES ($1, $2, $3, $4, $5) RETURNING id",
                user_id, guild_id, provider, model, listing_id,
            )
            return row["id"]

    async def ai_store_get_active_session(self, user_id: int) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM ai_store_sessions WHERE user_id = $1 AND active = TRUE ORDER BY updated_at DESC LIMIT 1",
                user_id,
            )
            return dict(row) if row else None

    async def ai_store_end_session(self, session_id: int):
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("UPDATE ai_store_sessions SET active = FALSE WHERE id = $1", session_id)

    async def ai_store_touch_session(self, session_id: int):
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("UPDATE ai_store_sessions SET updated_at = NOW() WHERE id = $1", session_id)

    async def ai_store_get_messages(self, session_id: int, limit: int = 40) -> List[Dict]:
        """Oldest-first, ready to feed to the provider as conversation history."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT role, content FROM ai_store_messages WHERE session_id = $1 "
                "ORDER BY created_at DESC LIMIT $2",
                session_id, limit,
            )
            return [dict(r) for r in reversed(rows)]

    async def ai_store_add_message(self, session_id: int, role: str, content: str,
                                    input_tokens: int = 0, output_tokens: int = 0, cost_credits: float = 0) -> int:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO ai_store_messages (session_id, role, content, input_tokens, output_tokens, cost_credits) "
                "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
                session_id, role, content, input_tokens, output_tokens, cost_credits,
            )
            await conn.execute("UPDATE ai_store_sessions SET updated_at = NOW() WHERE id = $1", session_id)
            return row["id"]

    async def ai_store_get_last_assistant_message(self, session_id: int) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM ai_store_messages WHERE session_id = $1 AND role = 'assistant' "
                "ORDER BY created_at DESC LIMIT 1",
                session_id,
            )
            return dict(row) if row else None

    # --- Listings -----------------------------------------------------

    async def ai_store_create_listing(self, seller_id: int, guild_id: Optional[int], name: str, description: str,
                                       category: str, system_prompt: str, provider: str, model: str) -> int:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO ai_store_listings (seller_id, guild_id, name, description, category, system_prompt, provider, model) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id",
                seller_id, guild_id, name, description, category, system_prompt, provider, model,
            )
            return row["id"]

    async def ai_store_set_review_result(self, listing_id: int, status: str, reason: str):
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE ai_store_listings SET review_status = $2, review_reason = $3 WHERE id = $1",
                listing_id, status, reason,
            )

    async def ai_store_get_pending_reviews(self, limit: int = 20) -> List[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM ai_store_listings WHERE review_status = 'needs_human' ORDER BY created_at ASC LIMIT $1",
                limit,
            )
            return [dict(r) for r in rows]

    async def ai_store_human_review_decision(self, listing_id: int, approve: bool):
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE ai_store_listings SET review_status = $2 WHERE id = $1",
                listing_id, "approved" if approve else "rejected",
            )

    async def ai_store_get_listing(self, listing_id: int) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM ai_store_listings WHERE id = $1", listing_id)
            return dict(row) if row else None

    async def ai_store_search_listings(self, guild_id: Optional[int], query: str = "", category: str = None,
                                        limit: int = 10) -> List[Dict]:
        """Visible listings = platform-wide (guild_id IS NULL) UNION this
        guild's own listings, approved + active + not-expired-boost only."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            sql = """
                SELECT * FROM ai_store_listings
                WHERE active = TRUE AND review_status = 'approved'
                  AND (guild_id IS NULL OR guild_id = $1)
                  AND (placement_tier = 'free' OR placement_expires_at > NOW())
            """
            params = [guild_id]
            if query:
                params.append(f"%{query}%")
                sql += f" AND (name ILIKE ${len(params)} OR description ILIKE ${len(params)})"
            if category:
                params.append(category)
                sql += f" AND category = ${len(params)}"
            sql += """
                ORDER BY
                  CASE placement_tier WHEN 'top' THEN 2 WHEN 'featured' THEN 1 ELSE 0 END DESC,
                  uses_count DESC, created_at DESC
                LIMIT ${}
            """.format(len(params) + 1)
            params.append(limit)
            rows = await conn.fetch(sql, *params)
            return [dict(r) for r in rows]

    async def ai_store_set_placement(self, listing_id: int, tier: str, days: Optional[int]):
        pool = await get_pool()
        async with pool.acquire() as conn:
            if days:
                await conn.execute(
                    "UPDATE ai_store_listings SET placement_tier = $2, "
                    "placement_expires_at = NOW() + ($3 || ' days')::interval WHERE id = $1",
                    listing_id, tier, str(days),
                )
            else:
                await conn.execute(
                    "UPDATE ai_store_listings SET placement_tier = $2, placement_expires_at = NULL WHERE id = $1",
                    listing_id, tier,
                )

    async def ai_store_increment_uses(self, listing_id: int):
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("UPDATE ai_store_listings SET uses_count = uses_count + 1 WHERE id = $1", listing_id)

    async def ai_store_list_seller_listings(self, seller_id: int) -> List[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM ai_store_listings WHERE seller_id = $1 ORDER BY created_at DESC", seller_id
            )
            return [dict(r) for r in rows]

    # --- Refunds --------------------------------------------------------

    async def ai_store_file_refund_request(self, user_id: int, message_id: int, session_id: int,
                                            amount_credits: float, reason: str, auto_approve: bool) -> Tuple[int, str]:
        pool = await get_pool()
        status = "auto_approved" if auto_approve else "pending"
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO ai_store_refund_requests (user_id, message_id, session_id, amount_credits, reason, status) "
                "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
                user_id, message_id, session_id, amount_credits, reason, status,
            )
            return row["id"], status

    async def ai_store_get_pending_refunds(self, limit: int = 20) -> List[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM ai_store_refund_requests WHERE status = 'pending' ORDER BY created_at ASC LIMIT $1",
                limit,
            )
            return [dict(r) for r in rows]

    async def ai_store_decide_refund(self, refund_id: int, approve: bool) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM ai_store_refund_requests WHERE id = $1", refund_id)
            if not row or row["status"] != "pending":
                return None
            await conn.execute(
                "UPDATE ai_store_refund_requests SET status = $2 WHERE id = $1",
                refund_id, "approved" if approve else "denied",
            )
            return dict(row)

    # --- Rate limiting ----------------------------------------------------

    async def ai_store_check_and_consume_rate_limit(self, user_id: int, min_gap_seconds: int = 3,
                                                      window_seconds: int = 60, max_per_window: int = 15) -> Tuple[bool, int]:
        """Returns (allowed, retry_after_seconds)."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO ai_store_rate_limits (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING", user_id
                )
                row = await conn.fetchrow(
                    "SELECT * FROM ai_store_rate_limits WHERE user_id = $1 FOR UPDATE", user_id
                )
                now = await conn.fetchval("SELECT NOW()")

                if row["last_ask_at"] is not None:
                    gap = (now - row["last_ask_at"]).total_seconds()
                    if gap < min_gap_seconds:
                        return False, int(min_gap_seconds - gap) + 1

                window_started = row["window_started_at"]
                count = row["ask_count_window"]
                elapsed = (now - window_started).total_seconds()
                if elapsed > window_seconds:
                    count = 0
                    window_started = now

                if count >= max_per_window:
                    return False, int(window_seconds - elapsed) + 1

                await conn.execute(
                    "UPDATE ai_store_rate_limits SET last_ask_at = $2, ask_count_window = $3, window_started_at = $4 "
                    "WHERE user_id = $1",
                    user_id, now, count + 1, window_started,
                )
                return True, 0

    # --- AI chat sessions (persistent "active conversation") --------------
    # Backs the /newchat, /aichat, /endchat flow in discord_bot/cogs/
    # ai_tools.py — same session shape as ai_store's buyer chat sessions,
    # applied to the free/tier-capped Groq chat in modules/ai_features.py.

    async def get_active_ai_chat_session(self, user_id: int) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM ai_chat_sessions WHERE user_id = $1 AND ended_at IS NULL "
                "ORDER BY started_at DESC LIMIT 1",
                user_id,
            )
            return dict(row) if row else None

    async def start_ai_chat_session(self, user_id: int) -> int:
        """Ends any existing active session first (one active session per
        user at a time), then opens a fresh one.

        ai_chat_sessions.user_id has a FK to users(user_id). /newchat (and
        /aichat's get_or_create_active_session) can be the very first
        command a user ever runs, before add_user() has ever inserted their
        row — same gap log_payment() already guards against. Upsert a
        minimal users row first (same ON CONFLICT DO NOTHING pattern) so
        this insert never trips the FK constraint."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO users (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
                    user_id,
                )
                await conn.execute(
                    "UPDATE ai_chat_sessions SET ended_at = NOW() WHERE user_id = $1 AND ended_at IS NULL",
                    user_id,
                )
                session_id = await conn.fetchval(
                    "INSERT INTO ai_chat_sessions (user_id) VALUES ($1) RETURNING id", user_id
                )
                return session_id

    async def end_ai_chat_session(self, user_id: int) -> bool:
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE ai_chat_sessions SET ended_at = NOW() WHERE user_id = $1 AND ended_at IS NULL",
                user_id,
            )
            return result != "UPDATE 0"

    async def set_ai_chat_session_last_bot_message(self, session_id: int, message_id: int) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE ai_chat_sessions SET last_bot_message_id = $2 WHERE id = $1",
                session_id, message_id,
            )

    async def get_ai_chat_session_by_last_bot_message(self, message_id: int) -> Optional[Dict]:
        """Used for reply-to-continue: given the id of a message the bot
        sent, find the still-active session it belongs to (if any)."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM ai_chat_sessions WHERE last_bot_message_id = $1 AND ended_at IS NULL",
                message_id,
            )
            return dict(row) if row else None

    # --- Bump network -------------------------------------------------
    # Backs discord_bot/cogs/bump.py. Guild config (channel/filters) and
    # listings (server + bots) are separate tables — see bump_guild_config
    # / bump_listings comments in _create_tables for why.

    async def bump_clear_guild_config(self, guild_id: int, clone_id: Optional[int]) -> None:
        """Explicitly wipes bump_channel_id/receives_bumps for a departed
        guild — bump_set_guild_config can't do this because its UPDATE
        branch uses COALESCE($3, bump_channel_id) for every field, so
        passing bump_channel_id=None there is a no-op on an existing row
        (COALESCE just keeps the current value). on_guild_remove needs an
        unconditional clear, not an upsert-with-null."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE bump_guild_config
                SET bump_channel_id = NULL, receives_bumps = FALSE, updated_at = NOW()
                WHERE guild_id = $1 AND COALESCE(clone_id, -1) = COALESCE($2, -1)
                """,
                guild_id, clone_id,
            )

    async def bump_get_guild_config(self, guild_id: int, clone_id: Optional[int]) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM bump_guild_config WHERE guild_id = $1 AND COALESCE(clone_id, -1) = COALESCE($2, -1)",
                guild_id, clone_id,
            )
            return dict(row) if row else None

    async def bump_set_guild_config(self, guild_id: int, clone_id: Optional[int], configured_by: int,
                                     bump_channel_id: Optional[int] = None, language: Optional[str] = None,
                                     nsfw_opt_in: Optional[bool] = None, intensity_level: Optional[int] = None,
                                     receives_bumps: Optional[bool] = None) -> Dict:
        pool = await get_pool()
        async with pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT * FROM bump_guild_config WHERE guild_id = $1 AND COALESCE(clone_id, -1) = COALESCE($2, -1)",
                guild_id, clone_id,
            )
            if existing is None:
                row = await conn.fetchrow(
                    """
                    INSERT INTO bump_guild_config (guild_id, clone_id, bump_channel_id, language, nsfw_opt_in, intensity_level, receives_bumps, configured_by)
                    VALUES ($1, $2, $3, COALESCE($4, 'any'), COALESCE($5, FALSE), COALESCE($6, 3), COALESCE($7, TRUE), $8)
                    RETURNING *
                    """,
                    guild_id, clone_id, bump_channel_id, language, nsfw_opt_in, intensity_level, receives_bumps, configured_by,
                )
            else:
                row = await conn.fetchrow(
                    """
                    UPDATE bump_guild_config SET
                        bump_channel_id = COALESCE($3, bump_channel_id),
                        language = COALESCE($4, language),
                        nsfw_opt_in = COALESCE($5, nsfw_opt_in),
                        intensity_level = COALESCE($6, intensity_level),
                        receives_bumps = COALESCE($7, receives_bumps),
                        configured_by = $8,
                        updated_at = NOW()
                    WHERE guild_id = $1 AND COALESCE(clone_id, -1) = COALESCE($2, -1)
                    RETURNING *
                    """,
                    guild_id, clone_id, bump_channel_id, language, nsfw_opt_in, intensity_level, receives_bumps, configured_by,
                )
            return dict(row)

    async def bump_list_configured_guilds(self, clone_id: Optional[int]) -> List[Dict]:
        """All guilds that have a bump channel configured for this clone,
        newest-configured first. Backs /bumpadmin list — each row has
        guild_id, bump_channel_id, receives_bumps, is_premium."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT guild_id, bump_channel_id, receives_bumps, is_premium
                FROM bump_guild_config
                WHERE COALESCE(clone_id, -1) = COALESCE($1, -1) AND bump_channel_id IS NOT NULL
                ORDER BY updated_at DESC
                """,
                clone_id,
            )
            return [dict(r) for r in rows]

    async def create_bump_oauth_state(self, state: str, guild_id: int, clone_id: Optional[int], invoker_id: int,
                                       application_id: int, bot_name: str, bot_icon_url: Optional[str],
                                       description: str, tags: List[str], existing_listing_id: Optional[int] = None) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO bump_oauth_states
                    (state, guild_id, clone_id, invoker_id, application_id, bot_name, bot_icon_url, description, tags, existing_listing_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                state, guild_id, clone_id, invoker_id, application_id, bot_name, bot_icon_url, description, tags, existing_listing_id,
            )

    async def pop_bump_oauth_state(self, state: str) -> Optional[Dict]:
        """Single-use: deleted on first lookup so a replayed OAuth callback
        can't finalize the same submission twice."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("DELETE FROM bump_oauth_states WHERE state = $1 RETURNING *", state)
            return dict(row) if row else None

    async def bump_finalize_bot_listing(self, guild_id: int, clone_id: Optional[int], created_by: int,
                                         application_id: int, name: str, description: str, tags: List[str],
                                         verified_owner_id: int, listing_id: Optional[int] = None) -> Dict:
        """Creates/updates a bot listing once OAuth ownership-verification
        has succeeded. invite_url is always derived from application_id
        here — never accepted from user input — so a verified submission
        can't be used to smuggle in an invite link for a different bot.
        New listings land as 'pending' so a moderator can spot-check before
        it goes out in a bump (see bump_review_listing); edits to an
        already-approved listing keep their status."""
        invite_url = (
            f"https://discord.com/api/oauth2/authorize?client_id={application_id}"
            f"&permissions=0&scope=bot%20applications.commands"
        )
        pool = await get_pool()
        async with pool.acquire() as conn:
            if listing_id is not None:
                row = await conn.fetchrow(
                    """
                    UPDATE bump_listings SET name = $2, description = $3, invite_url = $4, tags = $5,
                        application_id = $6, verified_owner_id = $7
                    WHERE id = $1 RETURNING *
                    """,
                    listing_id, name, description, invite_url, tags, application_id, verified_owner_id,
                )
            else:
                row = await conn.fetchrow(
                    """
                    INSERT INTO bump_listings
                        (guild_id, clone_id, listing_type, name, description, invite_url, tags, created_by, application_id, verified_owner_id, status)
                    VALUES ($1, $2, 'bot', $3, $4, $5, $6, $7, $8, $9, 'pending')
                    RETURNING *
                    """,
                    guild_id, clone_id, name, description, invite_url, tags, created_by, application_id, verified_owner_id,
                )
            return dict(row)

    async def bump_review_listing(self, listing_id: int, approve: bool) -> Optional[Dict]:
        """Moderator approve/reject for a pending bot listing. Rejecting
        deletes it outright rather than leaving a dead 'rejected' row
        around — RETURNING * on the delete so the caller can still DM the
        submitter what happened before the row is gone."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            if approve:
                row = await conn.fetchrow(
                    "UPDATE bump_listings SET status = 'approved' WHERE id = $1 RETURNING *", listing_id
                )
            else:
                row = await conn.fetchrow("DELETE FROM bump_listings WHERE id = $1 RETURNING *", listing_id)
            return dict(row) if row else None

    async def bump_list_pending_listings(self, clone_id: Optional[int]) -> List[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM bump_listings WHERE status = 'pending' AND COALESCE(clone_id, -1) = COALESCE($1, -1) ORDER BY created_at",
                clone_id,
            )
            return [dict(r) for r in rows]

    async def bump_get_listing(self, guild_id: int, clone_id: Optional[int], listing_type: str = "server",
                                listing_id: Optional[int] = None) -> Optional[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            if listing_id is not None:
                row = await conn.fetchrow("SELECT * FROM bump_listings WHERE id = $1", listing_id)
            else:
                # Default "server" listing per guild — first match. Bot
                # listings are looked up by id since a guild may own several.
                row = await conn.fetchrow(
                    """
                    SELECT * FROM bump_listings
                    WHERE guild_id = $1 AND COALESCE(clone_id, -1) = COALESCE($2, -1) AND listing_type = $3
                    ORDER BY id LIMIT 1
                    """,
                    guild_id, clone_id, listing_type,
                )
            return dict(row) if row else None

    async def bump_list_listings(self, guild_id: int, clone_id: Optional[int]) -> List[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM bump_listings WHERE guild_id = $1 AND COALESCE(clone_id, -1) = COALESCE($2, -1) ORDER BY id",
                guild_id, clone_id,
            )
            return [dict(r) for r in rows]

    async def bump_upsert_listing(self, guild_id: int, clone_id: Optional[int], created_by: int,
                                   listing_type: str, name: str, description: str, invite_url: Optional[str],
                                   tags: List[str], receives_ads: bool = True, listing_id: Optional[int] = None,
                                   perks: Optional[List[str]] = None, support_url: Optional[str] = None) -> Dict:
        pool = await get_pool()
        async with pool.acquire() as conn:
            if listing_id is not None:
                if perks is not None:
                    row = await conn.fetchrow(
                        """
                        UPDATE bump_listings SET name = $2, description = $3, invite_url = $4, tags = $5,
                            receives_ads = $6, perks = $7, support_url = $8
                        WHERE id = $1 RETURNING *
                        """,
                        listing_id, name, description, invite_url, tags, receives_ads, perks, support_url,
                    )
                else:
                    row = await conn.fetchrow(
                        """
                        UPDATE bump_listings SET name = $2, description = $3, invite_url = $4, tags = $5, receives_ads = $6
                        WHERE id = $1 RETURNING *
                        """,
                        listing_id, name, description, invite_url, tags, receives_ads,
                    )
            else:
                row = await conn.fetchrow(
                    """
                    INSERT INTO bump_listings (guild_id, clone_id, listing_type, name, description, invite_url, tags, receives_ads, created_by, perks, support_url)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                    RETURNING *
                    """,
                    guild_id, clone_id, listing_type, name, description, invite_url, tags, receives_ads, created_by,
                    perks or [], support_url,
                )
            return dict(row)

    async def bump_rate_listing(self, listing_id: int, user_id: int, rating: int) -> Tuple[float, int]:
        """Upserts one rater's score (a re-rate replaces their old one,
        it doesn't stack) and returns the refreshed (average, count) —
        bump_listings.rating_sum/rating_count are kept as a denormalized
        cache of that same aggregate so the ad-card embed can read them
        without a join on every send."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO bump_ratings (listing_id, user_id, rating)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (listing_id, user_id) DO UPDATE SET rating = $3, created_at = NOW()
                    """,
                    listing_id, user_id, rating,
                )
                agg = await conn.fetchrow(
                    "SELECT COALESCE(SUM(rating), 0) AS s, COUNT(*) AS c FROM bump_ratings WHERE listing_id = $1",
                    listing_id,
                )
                await conn.execute(
                    "UPDATE bump_listings SET rating_sum = $2, rating_count = $3 WHERE id = $1",
                    listing_id, agg["s"], agg["c"],
                )
            count = agg["c"]
            avg = (agg["s"] / count) if count else 0.0
            return avg, count

    async def bump_check_cooldown(self, listing_id: int, cooldown_seconds: int) -> Tuple[bool, int]:
        """Returns (can_bump, seconds_remaining)."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT last_bump_at FROM bump_listings WHERE id = $1", listing_id)
            if row is None or row["last_bump_at"] is None:
                return True, 0
            elapsed = (datetime.now(timezone.utc) - row["last_bump_at"]).total_seconds()
            if elapsed >= cooldown_seconds:
                return True, 0
            return False, int(cooldown_seconds - elapsed) + 1

    async def bump_record(self, listing_id: int, streak_window_seconds: int) -> int:
        """Updates last_bump_at, bumps or resets the streak, increments the
        lifetime total_bumps counter (this one never resets — it's the
        "Total bumps" stat on the ad card, distinct from the streak),
        and returns the new streak count."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT last_bump_at, streak_count FROM bump_listings WHERE id = $1", listing_id)
            now = datetime.now(timezone.utc)
            if row and row["last_bump_at"] and (now - row["last_bump_at"]).total_seconds() <= streak_window_seconds:
                new_streak = (row["streak_count"] or 0) + 1
            else:
                new_streak = 1
            await conn.execute(
                "UPDATE bump_listings SET last_bump_at = $2, streak_count = $3, total_bumps = total_bumps + 1 WHERE id = $1",
                listing_id, now, new_streak,
            )
            return new_streak

    async def bump_find_targets(self, exclude_guild_id: int, clone_id: Optional[int], language: str,
                                 include_nsfw: bool, limit: int = 200) -> List[Dict]:
        """Other guilds that have EXPLICITLY opted in to receiving bumps
        (receives_bumps = TRUE — a separate consent from just having a
        bump channel set, see bump_guild_config comment), filtered by
        language (guild's own setting of 'any' always matches) and NSFW
        opt-in."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT guild_id, bump_channel_id FROM bump_guild_config
                WHERE COALESCE(clone_id, -1) = COALESCE($1, -1)
                  AND guild_id != $2
                  AND bump_channel_id IS NOT NULL
                  AND receives_bumps = TRUE
                  AND (language = 'any' OR $3 = 'any' OR language = $3)
                  AND (NOT $4 OR nsfw_opt_in = TRUE)
                LIMIT $5
                """,
                clone_id, exclude_guild_id, language, include_nsfw, limit,
            )
            return [dict(r) for r in rows]

    async def bump_enqueue(self, listing_id: int, clone_id: Optional[int], targets: List[Dict],
                            drip_seconds: int) -> int:
        """Schedules one send per target, staggered by drip_seconds apart."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            now = datetime.now(timezone.utc)
            for i, target in enumerate(targets):
                await conn.execute(
                    """
                    INSERT INTO bump_queue (listing_id, target_guild_id, target_channel_id, clone_id, scheduled_at)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    listing_id, target["guild_id"], target["bump_channel_id"], clone_id,
                    now + timedelta(seconds=i * drip_seconds),
                )
            return len(targets)

    async def bump_get_due_queue(self, clone_id: Optional[int], limit: int = 10) -> List[Dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT q.*, l.name, l.description, l.invite_url, l.support_url, l.tags, l.listing_type,
                       l.guild_id AS listing_guild_id, l.created_at AS listing_created_at,
                       l.total_bumps, l.perks, l.rating_sum, l.rating_count, l.streak_count
                FROM bump_queue q
                JOIN bump_listings l ON l.id = q.listing_id
                WHERE COALESCE(q.clone_id, -1) = COALESCE($1, -1)
                  AND q.sent_at IS NULL
                  AND q.scheduled_at <= NOW()
                ORDER BY q.scheduled_at
                LIMIT $2
                """,
                clone_id, limit,
            )
            return [dict(r) for r in rows]

    async def bump_mark_sent(self, queue_id: int) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("UPDATE bump_queue SET sent_at = NOW() WHERE id = $1", queue_id)

    async def bump_get_listings_needing_reminder(self, clone_id: Optional[int], cooldown_seconds: int,
                                                  limit: int = 25) -> List[Dict]:
        """Listings whose cooldown has expired and haven't been reminded
        since their last bump. reminder_sent_at < last_bump_at (or NULL)
        is what lets exactly one reminder fire per cooldown window — once
        bump_mark_reminder_sent stamps it, this row drops out of the
        query until the next bump_record() moves last_bump_at forward
        again."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, guild_id, clone_id, name, listing_type, last_bump_at
                FROM bump_listings
                WHERE COALESCE(clone_id, -1) = COALESCE($1, -1)
                  AND last_bump_at IS NOT NULL
                  AND last_bump_at <= NOW() - ($2 * INTERVAL '1 second')
                  AND (reminder_sent_at IS NULL OR reminder_sent_at < last_bump_at)
                ORDER BY last_bump_at
                LIMIT $3
                """,
                clone_id, cooldown_seconds, limit,
            )
            return [dict(r) for r in rows]

    async def bump_mark_reminder_sent(self, listing_id: int) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("UPDATE bump_listings SET reminder_sent_at = NOW() WHERE id = $1", listing_id)




class InsufficientCreditsError(Exception):
    def __init__(self, needed: float, have: float):
        self.needed = needed
        self.have = have
        super().__init__(f"Needed {needed}, have {have}")


# Global database instance
db = Database()
