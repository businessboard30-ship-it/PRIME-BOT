-- path: database/migrations/091_card_coin_games.sql

-- ============================================================================
-- Migration 091: coin games state + log (phase 4). Additive and idempotent.
--   card_game_state: per-user cooldown / shield timestamps (game = 'coinflip','leagueflip','rob','robbed').
--   card_game_log:   every play (wager, outcome, coin delta) for audit and balance tuning.
-- Required by: modules/card_games.py
-- ============================================================================

CREATE TABLE IF NOT EXISTS card_game_state (
    user_id  BIGINT NOT NULL,
    clone_id INTEGER,
    game     TEXT   NOT NULL,
    last_at  TIMESTAMPTZ NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS card_game_state_key ON card_game_state (user_id, COALESCE(clone_id, -1), game);

CREATE TABLE IF NOT EXISTS card_game_log (
    log_id     BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL,
    clone_id   INTEGER,
    game       TEXT   NOT NULL,
    target_id  BIGINT,
    wager      BIGINT NOT NULL DEFAULT 0,
    won        BOOLEAN NOT NULL,
    delta      BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_card_game_log_user ON card_game_log (user_id, created_at DESC);
