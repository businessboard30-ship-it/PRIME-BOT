-- Phase 8 Homes: one bought home per player per bot (tier, vault, what was paid). Additive and idempotent.
-- exposure / protection_until / off_until are filled by Phase 9 (upgrades, raids); they exist now so that phase needs no ALTER.
CREATE TABLE IF NOT EXISTS cat_homes (
    owner_id          BIGINT      NOT NULL,
    clone_key         BIGINT      NOT NULL DEFAULT -1,
    tier              TEXT        NOT NULL,
    vault             BIGINT      NOT NULL DEFAULT 0 CHECK (vault >= 0),
    invested_coins    BIGINT      NOT NULL DEFAULT 0,
    invested_primole  BIGINT      NOT NULL DEFAULT 0,
    exposure          SMALLINT    NOT NULL DEFAULT 0,
    protection_until  TIMESTAMPTZ,
    off_until         TIMESTAMPTZ,
    bought_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (owner_id, clone_key)
);
