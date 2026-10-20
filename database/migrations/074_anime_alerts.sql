-- Anime feed, slice D2: per-anime alert mode, personal alert preferences, DM dedupe. Additive and idempotent.
ALTER TABLE anime_follows ADD COLUMN IF NOT EXISTS alert_mode TEXT NOT NULL DEFAULT 'mention';          -- 'mention' (in the server) | 'dm' | 'mute'
ALTER TABLE anime_follows ADD COLUMN IF NOT EXISTS alert_changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(); -- a DM alert never covers an episode that aired before this
CREATE INDEX IF NOT EXISTS anime_follows_dm_idx ON anime_follows (anime_id) WHERE alert_mode = 'dm';

-- A person's alert settings: timezone offset, quiet hours (local hours, end exclusive; NULL = no quiet hours) and how many DMs in a row failed.
CREATE TABLE IF NOT EXISTS anime_user_prefs (
    user_id      BIGINT PRIMARY KEY,
    tz_minutes   INT NOT NULL DEFAULT 0,
    quiet_start  SMALLINT,
    quiet_end    SMALLINT,
    dm_failures  INT NOT NULL DEFAULT 0,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- One row per DM alert already sent (claimed BEFORE sending, so a restart or a second bot never double sends).
CREATE TABLE IF NOT EXISTS anime_alerts_sent (
    user_id  BIGINT NOT NULL,
    anime_id BIGINT NOT NULL,
    episode  INT NOT NULL,
    sent_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, anime_id, episode)
);
