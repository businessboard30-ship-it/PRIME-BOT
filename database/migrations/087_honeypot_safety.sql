-- path: database/migrations/087_honeypot_safety.sql

-- ============================================================================
-- Migration 087: honeypot Discord-safety options
-- ============================================================================
-- discord_honeypot_config.dm_offender: DM the caught account why it was actioned (default TRUE = unchanged behaviour).
-- discord_honeypot_config.hide_from_members: deny View Channel to every normal role so only role-less accounts
-- (fresh joins, unverified) can see the trap (default FALSE = unchanged behaviour).
-- Additive and idempotent.
--
-- Required by: database.py -> set_honeypot_config(), discord_bot/cogs/honeypot.py
-- ============================================================================

ALTER TABLE discord_honeypot_config ADD COLUMN IF NOT EXISTS dm_offender BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE discord_honeypot_config ADD COLUMN IF NOT EXISTS hide_from_members BOOLEAN NOT NULL DEFAULT FALSE;
