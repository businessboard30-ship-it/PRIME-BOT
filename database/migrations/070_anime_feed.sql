-- Anime release feed, slice A: per-server setup (channel + on/off). Additive and idempotent.
CREATE TABLE IF NOT EXISTS anime_feed_config (
    guild_id   BIGINT NOT NULL,
    clone_key  BIGINT NOT NULL DEFAULT -1,
    enabled    BOOLEAN NOT NULL DEFAULT FALSE,
    channel_id BIGINT,
    updated_by BIGINT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, clone_key)
);
