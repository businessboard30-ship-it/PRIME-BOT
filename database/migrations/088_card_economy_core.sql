-- path: database/migrations/088_card_economy_core.sql

-- ============================================================================
-- Migration 088: card economy core (global store, limited stock, per-copy ownership, coin ledger)
-- ============================================================================
-- Phase 1 of the integrated economy / collection / trading rework. ADDITIVE and idempotent:
--   * card_catalog gains definition columns (strength, level gates, global stock, trade rules). Existing rows keep
--     working: stock_total NULL = unlimited, store_enabled FALSE = not in the new store until an admin opts it in.
--   * card_copies: one row per individually owned copy (single valid owner, optional serial "#07 of 100",
--     rental expiry, boost state). Existing user_cards (fungible quantity rows) are NOT touched or migrated here.
--   * card_offers: what the store sells (permanent / rental) per card definition.
--   * card_purchases: purchase history. card_stock_events: replenishment history.
--   * card_coin_ledger: audit trail for every coin movement in the new flows.
-- Coins are the existing global card_coins pool (per clone) so one balance works across servers.
--
-- Required by: modules/card_economy.py
-- ============================================================================

ALTER TABLE card_catalog ADD COLUMN IF NOT EXISTS base_strength   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE card_catalog ADD COLUMN IF NOT EXISTS min_level       INTEGER NOT NULL DEFAULT 0;
ALTER TABLE card_catalog ADD COLUMN IF NOT EXISTS max_level       INTEGER;
ALTER TABLE card_catalog ADD COLUMN IF NOT EXISTS stock_total     INTEGER;          -- NULL = unlimited
ALTER TABLE card_catalog ADD COLUMN IF NOT EXISTS stock_remaining INTEGER;          -- NULL when unlimited
ALTER TABLE card_catalog ADD COLUMN IF NOT EXISTS tradeable       BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE card_catalog ADD COLUMN IF NOT EXISTS store_enabled   BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE card_catalog ADD COLUMN IF NOT EXISTS skills          JSONB   NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE card_catalog ADD COLUMN IF NOT EXISTS max_boost_level INTEGER NOT NULL DEFAULT 10;

DO $$ BEGIN
    ALTER TABLE card_catalog ADD CONSTRAINT card_catalog_stock_chk
        CHECK (stock_total IS NULL OR (stock_remaining IS NOT NULL AND stock_remaining >= 0
                                       AND stock_remaining <= stock_total)) NOT VALID;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS card_offers (
    offer_id     SERIAL PRIMARY KEY,
    card_id      INTEGER NOT NULL REFERENCES card_catalog(card_id),
    kind         TEXT NOT NULL CHECK (kind IN ('permanent', 'rental')),
    rental_days  INTEGER CHECK (rental_days IS NULL OR rental_days > 0),
    price        BIGINT  NOT NULL CHECK (price >= 0),
    active       BOOLEAN NOT NULL DEFAULT TRUE,
    CHECK ((kind = 'rental') = (rental_days IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS card_offers_card_kind_days_key
    ON card_offers (card_id, kind, COALESCE(rental_days, 0));

CREATE TABLE IF NOT EXISTS card_copies (
    copy_id           BIGSERIAL PRIMARY KEY,
    card_id           INTEGER NOT NULL REFERENCES card_catalog(card_id),
    clone_id          INTEGER,
    owner_id          BIGINT  NOT NULL,
    serial_no         INTEGER,
    source            TEXT    NOT NULL DEFAULT 'store',
    acquired_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    rental_expires_at TIMESTAMPTZ,
    boost_level       INTEGER NOT NULL DEFAULT 0 CHECK (boost_level >= 0),
    trade_locked      BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE UNIQUE INDEX IF NOT EXISTS card_copies_card_serial_key
    ON card_copies (card_id, serial_no) WHERE serial_no IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_card_copies_owner ON card_copies (owner_id, COALESCE(clone_id, -1), acquired_at DESC);
CREATE INDEX IF NOT EXISTS idx_card_copies_card  ON card_copies (card_id);
CREATE INDEX IF NOT EXISTS idx_card_copies_rental ON card_copies (rental_expires_at) WHERE rental_expires_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS card_purchases (
    purchase_id BIGSERIAL PRIMARY KEY,
    user_id     BIGINT  NOT NULL,
    clone_id    INTEGER,
    card_id     INTEGER NOT NULL REFERENCES card_catalog(card_id),
    offer_id    INTEGER REFERENCES card_offers(offer_id),
    copy_id     BIGINT  REFERENCES card_copies(copy_id) ON DELETE SET NULL,
    kind        TEXT    NOT NULL,
    price       BIGINT  NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_card_purchases_user ON card_purchases (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_card_purchases_card ON card_purchases (card_id, created_at DESC);

CREATE TABLE IF NOT EXISTS card_stock_events (
    event_id   BIGSERIAL PRIMARY KEY,
    card_id    INTEGER NOT NULL REFERENCES card_catalog(card_id),
    delta      INTEGER NOT NULL,
    reason     TEXT    NOT NULL,
    actor_id   BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS card_coin_ledger (
    entry_id   BIGSERIAL PRIMARY KEY,
    user_id    BIGINT  NOT NULL,
    clone_id   INTEGER,
    delta      BIGINT  NOT NULL,
    balance    BIGINT  NOT NULL,
    reason     TEXT    NOT NULL,
    ref        TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_card_coin_ledger_user ON card_coin_ledger (user_id, created_at DESC);

DO $$ BEGIN
    ALTER TABLE card_coins ADD CONSTRAINT card_coins_nonneg_chk CHECK (balance >= 0) NOT VALID;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
