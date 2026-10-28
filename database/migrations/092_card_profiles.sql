-- path: database/migrations/092_card_profiles.sql

-- ============================================================================
-- Migration 092: collector profiles + achievements (phase 5). Additive and idempotent.
--   card_profiles:     privacy toggles, featured cards, signature card, chosen title (one row per user per bot).
--   card_achievements: earned achievement keys (rewards are badges/titles only, never coins or boosts).
-- All statistics are derived live from card_copies / card_trades; nothing here caches counts.
-- Required by: modules/card_profile.py
-- ============================================================================

CREATE TABLE IF NOT EXISTS card_profiles (
    user_id           BIGINT NOT NULL,
    clone_id          INTEGER,
    show_coins        BOOLEAN NOT NULL DEFAULT FALSE,
    show_collection   BOOLEAN NOT NULL DEFAULT TRUE,
    show_trades       BOOLEAN NOT NULL DEFAULT TRUE,
    featured_copy_ids BIGINT[] NOT NULL DEFAULT '{}',
    signature_copy_id BIGINT,
    title_key         TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS card_profiles_key ON card_profiles (user_id, COALESCE(clone_id, -1));

CREATE TABLE IF NOT EXISTS card_achievements (
    user_id   BIGINT NOT NULL,
    clone_id  INTEGER,
    key       TEXT   NOT NULL,
    earned_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS card_achievements_key ON card_achievements (user_id, COALESCE(clone_id, -1), key);
CREATE INDEX IF NOT EXISTS idx_card_copies_owner_perm ON card_copies (owner_id, COALESCE(clone_id, -1))
    WHERE rental_expires_at IS NULL;
