-- Cat adoption: every automatic cat spawn can be adopted once (virtual only, no real money), adopted cats can be
-- renamed, released and traded between members (modules/cat_adopt.py). Additive, idempotent.
CREATE TABLE IF NOT EXISTS cat_spawns (
    id          BIGSERIAL PRIMARY KEY,
    guild_id    BIGINT NOT NULL,
    clone_id    BIGINT,
    channel_id  BIGINT NOT NULL,
    message_id  BIGINT,
    pool_id     BIGINT NOT NULL,
    adopted_by  BIGINT,
    adopted_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cat_spawns_created ON cat_spawns (created_at);

CREATE TABLE IF NOT EXISTS cat_adoptions (
    id                BIGSERIAL PRIMARY KEY,
    owner_id          BIGINT NOT NULL,
    clone_id          BIGINT,
    pool_id           BIGINT NOT NULL,
    name              TEXT NOT NULL,
    rarity            TEXT NOT NULL DEFAULT 'common',
    trait             TEXT NOT NULL DEFAULT 'Playful',
    adopted_guild_id  BIGINT,
    adopted_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    traded_count      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cat_adoptions_owner ON cat_adoptions (owner_id, (COALESCE(clone_id, -1)));
CREATE INDEX IF NOT EXISTS idx_cat_adoptions_pool ON cat_adoptions (pool_id);
