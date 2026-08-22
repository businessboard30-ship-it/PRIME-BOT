-- Reference copy of the schema database.py's _create_tables() creates
-- automatically on cold start. You do not need to run this by hand against
-- a normal deployment — it's here for anyone who prefers to provision the
-- database manually, and as documentation of the shape.
--
-- Phase 3 of the Discord expansion (Dank Memer parity). Deliberately its
-- own point pool, separate from discord_xp — see
-- discord-bot-expansion-spec.md's decision log. No real-money purchase
-- path anywhere in this schema: earn boosts are cooldown-gated (vote /
-- sponsor-embed), not purchasable.

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
);
CREATE UNIQUE INDEX IF NOT EXISTS discord_economy_balances_guild_clone_user_key
    ON discord_economy_balances (guild_id, COALESCE(clone_id, -1), user_id);
CREATE INDEX IF NOT EXISTS idx_discord_economy_balances_leaderboard
    ON discord_economy_balances (guild_id, clone_id, balance DESC);

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
);
CREATE INDEX IF NOT EXISTS idx_discord_economy_shop_guild
    ON discord_economy_shop_items (guild_id, clone_id);

-- Audit trail only — NOT the real-money payment_logs table. This is fake
-- in-guild currency, kept strictly separate so payment_logs stays reliable
-- for actual revenue reporting/reconciliation.
CREATE TABLE IF NOT EXISTS discord_economy_transactions (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    clone_id INTEGER,
    user_id BIGINT NOT NULL,
    amount BIGINT NOT NULL,
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_discord_economy_transactions_user
    ON discord_economy_transactions (guild_id, clone_id, user_id, created_at DESC);

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
);
CREATE UNIQUE INDEX IF NOT EXISTS discord_economy_config_guild_clone_key
    ON discord_economy_config (guild_id, COALESCE(clone_id, -1));
