-- Cat market + one cat, one owner (main bot AND every clone share the same pictures). Additive, idempotent.
--
-- 1) A picture (pool_id) can be adopted only ONCE across the main bot and all clones. Duplicates that exist from before
--    this rule are reset here: the earliest adoption keeps the cat, every later copy is removed (its owner can simply
--    adopt another cat). Any open listing of a removed copy goes with it (ON DELETE CASCADE below).
DELETE FROM cat_adoptions a
 USING cat_adoptions b
 WHERE a.pool_id = b.pool_id AND (a.adopted_at, a.id) > (b.adopted_at, b.id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_cat_adoptions_pool ON cat_adoptions (pool_id);

-- 2) Cats put up for sale for play coins (never real money). The listing is posted like a spawn; the first person to
--    pay takes the cat and the seller gets the coins. seller_clone_id / buyer_clone_id = which bot's wallet moves.
CREATE TABLE IF NOT EXISTS cat_listings (
    id             BIGSERIAL PRIMARY KEY,
    adoption_id    BIGINT NOT NULL REFERENCES cat_adoptions (id) ON DELETE CASCADE,
    seller_id      BIGINT NOT NULL,
    seller_clone_id BIGINT,
    price          BIGINT NOT NULL CHECK (price > 0),
    guild_id       BIGINT,
    channel_id     BIGINT,
    message_id     BIGINT,
    status         TEXT NOT NULL DEFAULT 'open',          -- open | sold | cancelled | void
    buyer_id       BIGINT,
    buyer_clone_id BIGINT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at      TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_cat_listings_open ON cat_listings (adoption_id) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_cat_listings_status ON cat_listings (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cat_listings_seller ON cat_listings (seller_id) WHERE status = 'open';
