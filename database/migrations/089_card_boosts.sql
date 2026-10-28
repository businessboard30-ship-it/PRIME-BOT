-- path: database/migrations/089_card_boosts.sql

-- ============================================================================
-- Migration 089: card boosts and boost items (phase 2). Additive and idempotent.
--   user_boost_items: stackable boost items per user (never negative).
--   card_boost_log:   every boost applied to a copy (audit + history).
-- Boost rules/costs live in modules/card_boost.py (data-driven tables); the boost level itself is
-- card_copies.boost_level (migration 088), so it travels with the copy when traded.
-- Required by: modules/card_boost.py
-- ============================================================================

CREATE TABLE IF NOT EXISTS user_boost_items (
    user_id   BIGINT  NOT NULL,
    clone_id  INTEGER,
    item_key  TEXT    NOT NULL,
    quantity  INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0)
);
CREATE UNIQUE INDEX IF NOT EXISTS user_boost_items_key
    ON user_boost_items (user_id, COALESCE(clone_id, -1), item_key);

CREATE TABLE IF NOT EXISTS card_boost_log (
    log_id      BIGSERIAL PRIMARY KEY,
    copy_id     BIGINT  NOT NULL,
    user_id     BIGINT  NOT NULL,
    from_level  INTEGER NOT NULL,
    to_level    INTEGER NOT NULL,
    coins_spent BIGINT  NOT NULL DEFAULT 0,
    item_key    TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_card_boost_log_copy ON card_boost_log (copy_id, created_at DESC);
