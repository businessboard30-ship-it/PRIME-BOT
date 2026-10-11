-- Cat care: water, bed, food, random hunger, and cats that wander off after being hungry too long.
-- Additive/idempotent. Every existing cat starts "fine" (no hunger, no bed, no water).
ALTER TABLE cat_adoptions ADD COLUMN IF NOT EXISTS hungry_since  TIMESTAMPTZ;   -- NULL = not hungry
ALTER TABLE cat_adoptions ADD COLUMN IF NOT EXISTS hunger_warned TIMESTAMPTZ;   -- the one gentle heads-up was sent
ALTER TABLE cat_adoptions ADD COLUMN IF NOT EXISTS fed_at        TIMESTAMPTZ;
ALTER TABLE cat_adoptions ADD COLUMN IF NOT EXISTS watered_until TIMESTAMPTZ;
ALTER TABLE cat_adoptions ADD COLUMN IF NOT EXISTS bed_until     TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS ix_cat_adoptions_hungry ON cat_adoptions (hungry_since) WHERE hungry_since IS NOT NULL;

-- A cat that left home waits here until it is posted again in a spawn. The picture (pool_id) is unique: one row each.
CREATE TABLE IF NOT EXISTS cat_runaways (
    pool_id     BIGINT PRIMARY KEY,
    guild_id    BIGINT,                 -- the server it was adopted in (where it comes back)
    cat_name    TEXT NOT NULL,
    rarity      TEXT NOT NULL,
    trait       TEXT NOT NULL,
    left_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    returned_at TIMESTAMPTZ,            -- posted in a spawn again
    claimed_at  TIMESTAMPTZ             -- adopted again
);
CREATE INDEX IF NOT EXISTS ix_cat_runaways_open ON cat_runaways (guild_id, left_at) WHERE returned_at IS NULL;
