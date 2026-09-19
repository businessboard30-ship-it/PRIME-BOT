-- path: database/migrations/015_guild_premium.sql

-- ============================================================================
-- Migration 015: Per-server Premium subscription ($5/month)
-- ============================================================================
-- One row per (guild, clone). expires_at is the end of the paid window;
-- database.py's is_guild_premium_active() adds config.PREMIUM_GRACE_DAYS on
-- top. subscription_id is set for Gumroad memberships so renewal pings
-- (which carry no order reference) can be matched back to the guild.
-- reminded_for stores the expires_at value a renewal reminder was already
-- sent for, so the reminder loop never DMs twice for the same window.
--
-- Also adds discord_custom_roles.via_premium: a Custom Role entitlement that
-- was granted automatically because the guild had Premium, and therefore
-- stops counting once Premium lapses (a purchased entitlement never does).
-- ============================================================================

CREATE TABLE IF NOT EXISTS discord_guild_subscriptions (
    guild_id BIGINT NOT NULL,
    clone_id INTEGER,
    expires_at TIMESTAMPTZ NOT NULL,
    activated_by BIGINT,
    subscription_id TEXT,
    reminded_for TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS discord_guild_subscriptions_guild_clone_key
    ON discord_guild_subscriptions (guild_id, COALESCE(clone_id, -1));

CREATE INDEX IF NOT EXISTS idx_discord_guild_subscriptions_sub
    ON discord_guild_subscriptions (subscription_id);

ALTER TABLE discord_custom_roles ADD COLUMN IF NOT EXISTS via_premium BOOLEAN NOT NULL DEFAULT FALSE;
