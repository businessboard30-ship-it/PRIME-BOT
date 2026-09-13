-- path: database/migrations/013_xp_boost.sql

-- ============================================================================
-- Migration 013: Per-user XP boost (paid, via Selar)
-- ============================================================================
-- Backs the "⚡ Boost XP" button on the level-up card (levels < 3 only) and
-- under /leaderboard — see leveling-boost-build-prompt.md §2 and
-- payments_manual.py's UNLOCK_HANDLERS["xp_boost"].
--
-- Guild-scoped (not account-wide): a member buys a boost that only applies
-- in the server they bought it from, since XP itself is already per-guild
-- (discord_xp_guild_clone_user_key). clone_id disambiguates the same way
-- discord_xp does, so a clone and the main bot running in the same guild
-- never share a boost.
--
-- Required by:
--   database.py -> get_active_xp_boost() / activate_xp_boost()
--   discord_bot/cogs/leveling.py -> on_message (multiplier check),
--     _send_level_up_card (button gate)
--   payments_manual.py -> UNLOCK_HANDLERS["xp_boost"]
-- ============================================================================

CREATE TABLE IF NOT EXISTS discord_xp_boosts (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    clone_id INTEGER,
    multiplier NUMERIC NOT NULL DEFAULT 2.0,
    expires_at TIMESTAMPTZ NOT NULL,
    activated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Same COALESCE(clone_id, -1) trick as discord_xp_guild_clone_user_key —
-- a second purchase in the same guild/clone re-activates (extends/replaces)
-- the existing row rather than creating a duplicate, via ON CONFLICT in
-- activate_xp_boost().
CREATE UNIQUE INDEX IF NOT EXISTS discord_xp_boosts_guild_clone_user_key
    ON discord_xp_boosts (guild_id, COALESCE(clone_id, -1), user_id);

-- get_active_xp_boost() filters on expires_at every message a boosted
-- member sends, so this needs to be cheap.
CREATE INDEX IF NOT EXISTS idx_discord_xp_boosts_expires
    ON discord_xp_boosts (expires_at);

-- Tracks whether a member has already been shown the "⚡ Boost XP" pitch on
-- their level-up card, so it's shown once (while they're under level 3),
-- not on every single level-up in that range. Lives on discord_xp directly
-- rather than a separate table — one boolean per (guild, clone, user), same
-- key shape as the row that already exists for them by the time they've
-- leveled up at all.
ALTER TABLE discord_xp
    ADD COLUMN IF NOT EXISTS boost_pitched BOOLEAN NOT NULL DEFAULT FALSE;

-- ============================================================================
-- Migration Complete — this file is auto-applied on bot startup by
-- database.py's init routine (same pattern as 001-012 in this folder).
-- Remember: SCHEMA_VERSION in database.py must be bumped in the same change
-- that wires this file into the startup runner, or it silently never runs
-- (see the comment block above SCHEMA_VERSION in that file).
-- ============================================================================
-- Still manual:
-- 1. Create the real Selar product for "xp_boost" and swap its URL into
--    config.SELAR_PRODUCT_LINKS (currently a placeholder).
-- 2. Set that product's "Redirect URL after purchase" to
--      https://dash-production-c237.up.railway.app/unlock?payment_type=xp_boost
--    and add the same two required Custom Checkout Form fields ("Discord
--    username", "Discord server name") as every other manual-payment
--    product in this flow.
-- ============================================================================
