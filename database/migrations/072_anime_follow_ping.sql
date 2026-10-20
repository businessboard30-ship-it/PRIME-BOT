-- Anime release feed, slice C: optional ping role per server and personal follows. Additive and idempotent.
ALTER TABLE anime_feed_config ADD COLUMN IF NOT EXISTS ping_role_id BIGINT;

-- A person's followed anime (only their id, the AniList id and the title; nothing else). Unfollow deletes the row.
CREATE TABLE IF NOT EXISTS anime_follows (
    user_id     BIGINT NOT NULL,
    anime_id    BIGINT NOT NULL,
    title       TEXT NOT NULL,
    followed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, anime_id)
);
CREATE INDEX IF NOT EXISTS anime_follows_anime_idx ON anime_follows (anime_id);
