-- path: database/migrations/095_card_chief_seats.sql

-- ============================================================================
-- Migration 095: buy a Chief Seat with coins (leases). Additive and idempotent.
--   discord_clan_chiefs.lease_until : a purchased seat is a time-limited LEASE. While lease_until is in the future the XP
--     leaderboard recompute leaves the seat alone; when it passes the seat goes back to the normal top-5 rules.
--   card_chief_purchases            : history of every purchase / renewal (who, which seat, price, who was displaced).
-- Must run AFTER 018_clan_chiefs.sql. Required by: modules/card_chief.py, database.recompute_clan_chiefs
-- ============================================================================

ALTER TABLE discord_clan_chiefs ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS card_chief_purchases (
    purchase_id  BIGSERIAL PRIMARY KEY,
    guild_id     BIGINT      NOT NULL,
    clone_id     INTEGER,
    seat_rank    SMALLINT    NOT NULL,
    buyer_id     BIGINT      NOT NULL,
    displaced_id BIGINT,
    price        BIGINT      NOT NULL CHECK (price >= 0),
    lease_days   INTEGER     NOT NULL CHECK (lease_days > 0),
    lease_until  TIMESTAMPTZ NOT NULL,
    kind         TEXT        NOT NULL DEFAULT 'buy' CHECK (kind IN ('buy', 'renew')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS card_chief_purchases_guild_idx ON card_chief_purchases (guild_id, created_at DESC);
CREATE INDEX IF NOT EXISTS card_chief_purchases_buyer_idx ON card_chief_purchases (buyer_id, created_at DESC);
