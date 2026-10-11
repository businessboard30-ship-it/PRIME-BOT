-- Cat pool: remember which pooled pictures each server has already been shown, so a server is given one it has
-- not seen recently and the cat API is only called when it has seen them all (modules/cat_pool.py).
-- Additive, idempotent.
CREATE TABLE IF NOT EXISTS cat_pool_seen (
    guild_id BIGINT      NOT NULL,
    pool_id  BIGINT      NOT NULL,
    seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, pool_id)
);
CREATE INDEX IF NOT EXISTS idx_cat_pool_seen_at ON cat_pool_seen (seen_at);
