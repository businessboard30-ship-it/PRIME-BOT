-- path: database/migrations/053_guild_plus.sql

-- ============================================================================
-- Migration 053: Premium Plus (per-server upper tier)
-- ============================================================================
-- Plus is an overlay on the same row as Premium: plus_expires_at is the end of the paid Plus window.
-- Buying Plus also pushes the Premium expiry (expires_at) out to at least that date, so every existing
-- Premium check keeps working untouched. plus_subscription_id is set for Gumroad memberships so renewal
-- pings (which carry no order reference) can be matched back to the server.
-- Additive / idempotent.
-- ============================================================================

ALTER TABLE discord_guild_subscriptions ADD COLUMN IF NOT EXISTS plus_expires_at TIMESTAMPTZ;
ALTER TABLE discord_guild_subscriptions ADD COLUMN IF NOT EXISTS plus_subscription_id TEXT;

CREATE INDEX IF NOT EXISTS idx_discord_guild_subscriptions_plus_sub
    ON discord_guild_subscriptions (plus_subscription_id);
