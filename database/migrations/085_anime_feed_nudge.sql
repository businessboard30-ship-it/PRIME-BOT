-- Anime setup nudge: a server that never set the anime feed up gets ONE reminder, posted in its mod-log channel (never a DM)
-- with a Set up button. This table records that it was sent, so it is never repeated. Additive and idempotent.
CREATE TABLE IF NOT EXISTS anime_feed_nudge (
    guild_id   BIGINT NOT NULL,
    clone_key  BIGINT NOT NULL DEFAULT -1,
    nudged_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, clone_key)
);
