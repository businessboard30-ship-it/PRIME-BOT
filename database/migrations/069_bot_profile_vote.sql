-- Edit bot profile: a server unlocks it once, after one of its admins has voted on DiscordThings (dsc.sh).
-- Additive and idempotent.
CREATE TABLE IF NOT EXISTS bot_profile_vote_unlocks (
    guild_id    BIGINT PRIMARY KEY,
    unlocked_by BIGINT,
    unlocked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
