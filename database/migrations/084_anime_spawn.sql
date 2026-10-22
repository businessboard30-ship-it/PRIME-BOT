-- path: database/migrations/084_anime_spawn.sql

-- Anime spawns: the feed now posts an anime card in the server's channel on an interval the admin picks in setup
-- (like the cat game's spawns). spawn_interval_minutes is that interval; last_spawn_at is when the last card posted,
-- so the worker only claims a server whose interval has passed. anime_feed_spawned remembers which titles a server
-- saw recently so a spawn never repeats one. Additive and idempotent.
ALTER TABLE anime_feed_config ADD COLUMN IF NOT EXISTS spawn_interval_minutes INTEGER NOT NULL DEFAULT 60;
ALTER TABLE anime_feed_config ADD COLUMN IF NOT EXISTS last_spawn_at TIMESTAMPTZ;
CREATE TABLE IF NOT EXISTS anime_feed_spawned (
    guild_id   BIGINT NOT NULL,
    clone_key  BIGINT NOT NULL DEFAULT -1,
    anime_id   BIGINT NOT NULL,
    spawned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, clone_key, anime_id)
);
CREATE INDEX IF NOT EXISTS idx_anime_feed_spawned_at ON anime_feed_spawned (guild_id, clone_key, spawned_at DESC);
