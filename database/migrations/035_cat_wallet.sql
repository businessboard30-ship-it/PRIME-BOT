-- Cat wallet (play coins and diamonds earned by adopting cats). Per person, per bot (main bot vs each clone).
-- Play currency only: nothing here is bought with, or sold for, real money. Additive and idempotent.
CREATE TABLE IF NOT EXISTS cat_wallets (
    owner_id   BIGINT      NOT NULL,
    clone_id   BIGINT,
    coins      BIGINT      NOT NULL DEFAULT 0 CHECK (coins >= 0),
    diamonds   INTEGER     NOT NULL DEFAULT 0 CHECK (diamonds >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_cat_wallets ON cat_wallets (owner_id, (COALESCE(clone_id, -1)));
