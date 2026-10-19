-- General Marketplace (R1): one listings table for every item type (cat now; house, cosmetic later). Additive.
CREATE TABLE IF NOT EXISTS market_listings (
    id              BIGSERIAL PRIMARY KEY,
    item_type       TEXT NOT NULL,                          -- 'cat', later 'house', 'cosmetic', ...
    item_ref        BIGINT NOT NULL,                        -- id of the item in its own table
    seller_id       BIGINT NOT NULL,
    seller_clone_id BIGINT,
    price           BIGINT NOT NULL CHECK (price > 0),
    fee             BIGINT NOT NULL DEFAULT 0,              -- taken from the price when sold (a coin sink)
    guild_id        BIGINT,
    channel_id      BIGINT,
    message_id      BIGINT,
    status          TEXT NOT NULL DEFAULT 'open',           -- open | sold | cancelled | void | expired
    buyer_id        BIGINT,
    buyer_clone_id  BIGINT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL,
    closed_at       TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_market_listings_open ON market_listings (item_type, item_ref) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_market_listings_status ON market_listings (status, item_type, price);
CREATE INDEX IF NOT EXISTS idx_market_listings_seller ON market_listings (seller_id, status);
