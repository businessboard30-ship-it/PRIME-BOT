-- path: database/migrations/093_card_levelup_rewards.sql

-- ============================================================================
-- Migration 093: level-up coin rewards (economy wiring). Additive and idempotent.
--   card_levelup_rewards: one row per (user, bot, level) that has already paid out coins. The unique key is what makes a
--   payout happen at most ONCE per level per person, however many servers they level in, so leveling can't be farmed.
-- Required by: modules/card_rewards.py
-- ============================================================================

CREATE TABLE IF NOT EXISTS card_levelup_rewards (
    user_id    BIGINT  NOT NULL,
    clone_id   INTEGER,
    level      INTEGER NOT NULL CHECK (level > 0),
    coins      BIGINT  NOT NULL CHECK (coins >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS card_levelup_rewards_key
    ON card_levelup_rewards (user_id, COALESCE(clone_id, -1), level);
