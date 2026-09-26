-- path: database/migrations/017_clan_cards.sql

-- ============================================================================
-- Migration 017: Clan cards (level-9-multiple flavor message)
-- ============================================================================
-- Every 9 levels a member gains (9, 18, 27, ...), leveling.py sends them one
-- of the "clan" banner cards (modules/clan_cards.py) with a flavor message
-- ("your clan is proud of you, level up more to become a god") as the
-- message content/mention — NOT baked onto the card image itself.
--
-- The clan a member gets is random the first time they cross a multiple of
-- 9, then locked forever (same member always gets the same clan card from
-- then on) — this table is that lock. Guild-scoped + clone_id, same shape
-- as discord_xp_boosts (013_xp_boost.sql), since a member's level (and so
-- their clan) is already per-guild-per-clone via discord_xp.
--
-- Required by:
--   database.py -> get_or_assign_clan_card()
--   discord_bot/cogs/leveling.py -> on_message (_send_clan_message)
--   modules/clan_cards.py -> CLAN_CARD_FILENAMES (the random-pick pool)
-- ============================================================================

CREATE TABLE IF NOT EXISTS discord_clan_cards (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    clone_id INTEGER,
    clan_card TEXT NOT NULL,
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Same COALESCE(clone_id, -1) trick as discord_xp_boosts_guild_clone_user_key
-- (013_xp_boost.sql) — one locked assignment per (guild, clone, user), and
-- get_or_assign_clan_card's INSERT ... ON CONFLICT DO NOTHING relies on this
-- exact index to make the lock atomic across concurrent on_message calls.
CREATE UNIQUE INDEX IF NOT EXISTS discord_clan_cards_guild_clone_user_key
    ON discord_clan_cards (guild_id, COALESCE(clone_id, -1), user_id);

-- ============================================================================
-- Migration Complete — this file is auto-applied on bot startup by
-- database.py's init routine (same pattern as 001-016 in this folder).
-- Remember: SCHEMA_VERSION in database.py must be bumped in the same change
-- that wires this file into the startup runner, or it silently never runs
-- (see the comment block above SCHEMA_VERSION in that file).
-- ============================================================================
