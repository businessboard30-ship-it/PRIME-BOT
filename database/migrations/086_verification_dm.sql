-- path: database/migrations/086_verification_dm.sql

-- ============================================================================
-- Migration 086: verify by DM
-- ============================================================================
-- discord_verification_config.dm_verify: when TRUE the bot also DMs each new member a verify button (free for every
-- server, not a Premium extra). The verify channel stays as the fallback for members whose DMs are closed.
-- Additive and idempotent.
--
-- Required by: database.py -> get_verification_config() / set_verification_config(),
--   discord_bot/cogs/verification.py (on_member_join), discord_bot/cogs/_views_verification.py,
--   discord_bot/cogs/_views_verify_setup.py
-- ============================================================================

ALTER TABLE discord_verification_config ADD COLUMN IF NOT EXISTS dm_verify BOOLEAN NOT NULL DEFAULT FALSE;
