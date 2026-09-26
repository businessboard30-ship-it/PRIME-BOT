-- path: database/migrations/018_clan_chiefs.sql

-- ============================================================================
-- Migration 018: Clan chiefs (Option B — 5 exclusive clan-chief seats)
-- ============================================================================
-- Decision log (confirmed by project owner): rank #1-5 on the per-server XP
-- leaderboard each claim ONE of the server's 5 clan-chief seats. A seat's
-- clan_slug is a PERMANENT label on the seat itself (assigned once, from
-- modules.clan_cards.CLAN_CARDS, in that fixed order) — it is NOT the same
-- thing as a member's own locked clan card (discord_clan_cards /
-- get_or_assign_clan_card). When someone is overtaken on the leaderboard,
-- they lose whichever seat they held and the overtaking member inherits
-- that exact seat (and its clan_slug) — so a chief's displayed "clan" can
-- differ from the clan they personally roll every 9 levels. This is the
-- explicit tradeoff the owner picked over Option A (rank-only, duplicate
-- clan chiefs allowed) specifically so no clan can go without a chief and
-- no clan can have two.
--
-- Guild-scoped + clone_id, same shape as discord_xp/discord_clan_cards.
-- seat_rank is 1-5 and is the seat's OWN identity (not derived at query
-- time) — user_id is swapped in place as the leaderboard shuffles, since
-- of the seat, not the person.
--
-- Required by:
--   database.py -> ensure_clan_seats() / recompute_clan_chiefs() /
--                   get_clan_seats() / get_chief_seat_for_user()
--   discord_bot/cogs/leveling.py -> on_message (chief recompute + announce,
--                   checked on level-up only, never polled)
--   discord_bot/cogs/_views_leveling_leaderboard.py -> crown badge per row
-- ============================================================================

CREATE TABLE IF NOT EXISTS discord_clan_chiefs (
    guild_id BIGINT NOT NULL,
    clone_id INTEGER,
    seat_rank SMALLINT NOT NULL,
    clan_slug TEXT NOT NULL,
    user_id BIGINT,
    since TIMESTAMPTZ
);

-- One row per (guild, clone, seat_rank) — same COALESCE(clone_id, -1) trick
-- as discord_xp_boosts/discord_clan_cards.
CREATE UNIQUE INDEX IF NOT EXISTS discord_clan_chiefs_guild_clone_seat_key
    ON discord_clan_chiefs (guild_id, COALESCE(clone_id, -1), seat_rank);

-- Fast "am I a chief / which seat" lookup for /rank and the leaderboard
-- crown badge (WHERE guild_id = ... AND clone_id IS NOT DISTINCT FROM ...
-- AND user_id = ...).
CREATE INDEX IF NOT EXISTS discord_clan_chiefs_user_lookup
    ON discord_clan_chiefs (guild_id, user_id);

-- ============================================================================
-- Migration Complete — auto-applied on bot startup by database.py's init
-- routine (same pattern as 001-017). SCHEMA_VERSION in database.py must be
-- bumped in the same change that wires this file in, or it silently never
-- runs (see the comment block above SCHEMA_VERSION in that file).
-- ============================================================================
