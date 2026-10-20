-- path: database/migrations/074_anime_alerts.sql

-- Anime feed D2: personal alerts. Additive and idempotent.
-- A follow can now be told how to alert its owner: 'mention' (default, today's behaviour), 'dm' (opt-in DM), 'off' (muted).
ALTER TABLE anime_follows ADD COLUMN IF NOT EXISTS alert_mode TEXT NOT NULL DEFAULT 'mention';
CREATE INDEX IF NOT EXISTS anime_follows_dm_idx ON anime_follows (anime_id) WHERE alert_mode = 'dm';

-- One row per person: timezone ('UTC+1' style), optional quiet hours (local hour 0-23), and the closed-DM counter.
CREATE TABLE IF NOT EXISTS anime_user_prefs (
    user_id     BIGINT PRIMARY KEY,
    tz          TEXT NOT NULL DEFAULT 'UTC+0',
    quiet_start SMALLINT,
    quiet_end   SMALLINT,
    dm_failures SMALLINT NOT NULL DEFAULT 0,
    fell_back   BOOLEAN NOT NULL DEFAULT FALSE,    -- DM alerts were switched back to mention after repeated closed DMs; shown once
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- DM dedupe: a restart or a second bot process never DMs the same person the same episode twice.
CREATE TABLE IF NOT EXISTS anime_alerts_sent (
    user_id  BIGINT NOT NULL,
    anime_id BIGINT NOT NULL,
    episode  INTEGER NOT NULL,
    sent_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, anime_id, episode)
);
