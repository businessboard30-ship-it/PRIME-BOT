-- Cat trade IDs: every cat owner (per bot) gets one fixed ID made of 7 random words, used to trade across servers
-- (Premium). Additive, idempotent.
CREATE TABLE IF NOT EXISTS cat_owner_ids (
    owner_id   BIGINT      NOT NULL,
    clone_id   BIGINT,
    trade_id   TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_cat_owner_ids_owner ON cat_owner_ids (COALESCE(clone_id, -1), owner_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_cat_owner_ids_trade ON cat_owner_ids (COALESCE(clone_id, -1), trade_id);
