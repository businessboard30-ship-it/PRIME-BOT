-- path: database/migrations/090_card_trading.sql

-- ============================================================================
-- Migration 090: card trading + coin transfers (phase 3). Additive and idempotent.
--   card_trades / card_trade_items: offers (global or local), history of completed/cancelled/expired.
--   card_coin_transfers: user-to-user coin transfers (amounts also appear in card_coin_ledger).
-- Offered cards are locked (card_copies.trade_locked) while a trade is pending so a copy can sit in only one offer.
-- Required by: modules/card_trade.py
-- ============================================================================

CREATE TABLE IF NOT EXISTS card_trades (
    trade_id     BIGSERIAL PRIMARY KEY,
    clone_id     INTEGER,
    scope        TEXT   NOT NULL CHECK (scope IN ('global', 'local')),
    guild_id     BIGINT,
    initiator_id BIGINT NOT NULL,
    recipient_id BIGINT NOT NULL,
    give_coins   BIGINT NOT NULL DEFAULT 0 CHECK (give_coins >= 0),
    get_coins    BIGINT NOT NULL DEFAULT 0 CHECK (get_coins >= 0),
    status       TEXT   NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','completed','cancelled','expired')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at   TIMESTAMPTZ NOT NULL,
    resolved_at  TIMESTAMPTZ,
    CHECK (initiator_id <> recipient_id)
);
CREATE INDEX IF NOT EXISTS idx_card_trades_pending ON card_trades (status, expires_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_card_trades_user ON card_trades (initiator_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_card_trades_recipient ON card_trades (recipient_id, created_at DESC);

CREATE TABLE IF NOT EXISTS card_trade_items (
    trade_id BIGINT NOT NULL REFERENCES card_trades(trade_id) ON DELETE CASCADE,
    copy_id  BIGINT NOT NULL,
    side     TEXT   NOT NULL CHECK (side IN ('initiator', 'recipient')),
    PRIMARY KEY (trade_id, copy_id)
);

CREATE TABLE IF NOT EXISTS card_coin_transfers (
    transfer_id BIGSERIAL PRIMARY KEY,
    clone_id    INTEGER,
    from_user   BIGINT NOT NULL,
    to_user     BIGINT NOT NULL,
    amount      BIGINT NOT NULL CHECK (amount > 0),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (from_user <> to_user)
);
CREATE INDEX IF NOT EXISTS idx_card_coin_transfers_from ON card_coin_transfers (from_user, created_at DESC);
