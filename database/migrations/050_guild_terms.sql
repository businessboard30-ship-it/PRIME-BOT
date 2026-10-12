-- Terms & Privacy acceptance, one row per server and bot (main bot or clone). Written by the wizard the bot posts when it
-- joins a server. Additive, idempotent.
CREATE TABLE IF NOT EXISTS guild_terms_acceptance (
    guild_id      BIGINT NOT NULL,
    clone_id      BIGINT,
    accepted_by   BIGINT NOT NULL,
    accepted_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    terms_version TEXT NOT NULL,
    clone_key     BIGINT GENERATED ALWAYS AS (COALESCE(clone_id, -1)) STORED,
    PRIMARY KEY (guild_id, clone_key)
);
