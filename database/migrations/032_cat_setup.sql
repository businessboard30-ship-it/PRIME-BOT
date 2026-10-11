-- Cat setup: per-server auto-cat spawning + the shared, quality-filtered cat image pool
-- (modules/cat_setup.py, modules/cat_pool.py, discord_bot/cogs/cat.py). Additive, idempotent.
CREATE TABLE IF NOT EXISTS cat_guild_config (
    guild_id         BIGINT NOT NULL,
    clone_id         BIGINT,
    enabled          BOOLEAN NOT NULL DEFAULT FALSE,
    channel_ids      BIGINT[] NOT NULL DEFAULT '{}',
    interval_minutes INTEGER NOT NULL DEFAULT 720,
    next_at          TIMESTAMPTZ,
    last_post_at     TIMESTAMPTZ,
    updated_by       BIGINT,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_cat_guild_config ON cat_guild_config (guild_id, (COALESCE(clone_id, -1)));
CREATE INDEX IF NOT EXISTS idx_cat_guild_config_due ON cat_guild_config (next_at) WHERE enabled;

-- One row per good cat picture. The picture itself lives in the image-hosting channel (same channel as welcome
-- backgrounds); only where it is stored is kept here. Shared by the main bot and every clone.
CREATE TABLE IF NOT EXISTS cat_image_pool (
    id             BIGSERIAL PRIMARY KEY,
    sha256         TEXT NOT NULL UNIQUE,
    source         TEXT NOT NULL,
    source_id      TEXT,
    width          INTEGER NOT NULL,
    height         INTEGER NOT NULL,
    sharpness      REAL NOT NULL,
    bytes          INTEGER NOT NULL,
    channel_id     BIGINT NOT NULL,
    message_id     BIGINT NOT NULL,
    added_by_clone BIGINT,
    uses           INTEGER NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cat_pool_source ON cat_image_pool (source, source_id);
CREATE INDEX IF NOT EXISTS idx_cat_pool_uses ON cat_image_pool (uses);
