-- path: database/migrations/026_topgg_votes.sql

-- ============================================================================
-- Migration 026: Top.gg vote XP boost
-- ============================================================================
-- One row per voter, GLOBAL (no guild_id / clone_id): every clone and the
-- main bot share one Top.gg listing, so a vote boosts the user's XP in every
-- server they chat in. Deliberately separate from discord_xp_boosts so a vote
-- can never overwrite or shorten a paid boost (activate_xp_boost replaces the
-- row on conflict). leveling.py takes the LARGER of the two multipliers.
--
-- Required by:
--   database.py -> record_topgg_vote() / has_active_topgg_vote() /
--     get_active_topgg_voters()
--   api/topgg_webhook.py
-- ============================================================================

CREATE TABLE IF NOT EXISTS topgg_votes (
    user_id BIGINT PRIMARY KEY,
    expires_at TIMESTAMPTZ NOT NULL,
    last_vote_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    vote_count INTEGER NOT NULL DEFAULT 1,
    last_vote_id TEXT
);

CREATE INDEX IF NOT EXISTS topgg_votes_expires_idx ON topgg_votes (expires_at);
