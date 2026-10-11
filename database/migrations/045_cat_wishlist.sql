-- Cat wishlist: "looking for a legendary" posts that traders can see. Additive/idempotent.
-- Wants expire after 14 days (expires_at); each person can keep a few open at once (enforced in modules/cat_wishlist.py).
CREATE TABLE IF NOT EXISTS cat_wishlist (
    id         BIGSERIAL PRIMARY KEY,
    owner_id   BIGINT NOT NULL,
    clone_id   BIGINT,
    rarity     TEXT NOT NULL CHECK (rarity IN ('common', 'uncommon', 'rare', 'legendary')),
    trait      TEXT,
    note       TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'removed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '14 days',
    clone_key  BIGINT GENERATED ALWAYS AS (COALESCE(clone_id, -1)) STORED
);
-- The same person can't post the same want twice while it is open.
CREATE UNIQUE INDEX IF NOT EXISTS ux_cat_wishlist_open
    ON cat_wishlist (owner_id, clone_key, rarity, (COALESCE(trait, ''))) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_cat_wishlist_board ON cat_wishlist (status, expires_at, created_at DESC);
