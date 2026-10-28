-- path: database/migrations/094_card_trade_drafts.sql

-- ============================================================================
-- Migration 094: trade drafts for the Card Hub trade screen. Additive and idempotent.
--   card_trade_drafts: the offer a player is still putting together (one per person per bot). Kept in the database, not
--   in memory, so a half-built offer survives a restart. Nothing here moves cards or coins; card_trade.propose_trade
--   re-validates everything when the draft is sent. Stale drafts are overwritten or discarded, never trusted.
-- Required by: modules/card_trade.py (draft helpers), discord_bot/cogs/_views_card_trade.py
-- ============================================================================

CREATE TABLE IF NOT EXISTS card_trade_drafts (
    user_id      BIGINT  NOT NULL,
    clone_id     INTEGER,
    recipient_id BIGINT  NOT NULL,
    scope        TEXT    NOT NULL CHECK (scope IN ('global', 'local')),
    guild_id     BIGINT,
    give         BIGINT[] NOT NULL DEFAULT '{}',
    get          BIGINT[] NOT NULL DEFAULT '{}',
    give_coins   BIGINT  NOT NULL DEFAULT 0 CHECK (give_coins >= 0),
    get_coins    BIGINT  NOT NULL DEFAULT 0 CHECK (get_coins >= 0),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (user_id <> recipient_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS card_trade_drafts_key
    ON card_trade_drafts (user_id, COALESCE(clone_id, -1));
