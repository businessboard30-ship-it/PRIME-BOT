-- Anime release feed, slice B: automatic posting. Additive and idempotent.
ALTER TABLE anime_feed_config ADD COLUMN IF NOT EXISTS enabled_at TIMESTAMPTZ;   -- when the feed was last switched on; older episodes are never posted

-- One row per episode already posted in a server: the duplicate guard (claimed BEFORE sending, so a restart or retry can't double post).
CREATE TABLE IF NOT EXISTS anime_feed_posted (
    guild_id  BIGINT NOT NULL,
    clone_key BIGINT NOT NULL DEFAULT -1,
    anime_id  BIGINT NOT NULL,
    episode   INT NOT NULL,
    posted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, clone_key, anime_id, episode)
);
