-- path: database/migrations/073_verification_pro.sql

-- ============================================================================
-- Migration 073: join verification premium extras
-- ============================================================================
-- All columns are nullable (NULL = off / default look). A server that is not
-- premium keeps whatever it saved here, but modules/verification_pro.py ignores
-- it until premium is active again (same "saved but paused" rule as anti-raid).
--
-- Required by:
--   database.py -> get_verification_config() / set_verification_config() /
--     get_verification_kick_configs()
--   modules/verification_pro.py, discord_bot/cogs/verification.py
-- ============================================================================

ALTER TABLE discord_verification_config ADD COLUMN IF NOT EXISTS panel_title TEXT;
ALTER TABLE discord_verification_config ADD COLUMN IF NOT EXISTS panel_text TEXT;
ALTER TABLE discord_verification_config ADD COLUMN IF NOT EXISTS panel_color INTEGER;
ALTER TABLE discord_verification_config ADD COLUMN IF NOT EXISTS panel_image_url TEXT;
ALTER TABLE discord_verification_config ADD COLUMN IF NOT EXISTS log_channel_id BIGINT;
ALTER TABLE discord_verification_config ADD COLUMN IF NOT EXISTS kick_after_minutes INTEGER;
ALTER TABLE discord_verification_config ADD COLUMN IF NOT EXISTS min_account_age_days INTEGER;
ALTER TABLE discord_verification_config ADD COLUMN IF NOT EXISTS welcome_text TEXT;

CREATE INDEX IF NOT EXISTS discord_verification_config_kick_idx
    ON discord_verification_config (clone_id) WHERE enabled AND kick_after_minutes IS NOT NULL;
