-- Welcome Card Studio Phase 5a: saved designs (the look only, never the pictures). Additive and idempotent.
-- clone_key is -1 for the main bot (a NULL clone_id cannot be part of a unique key). Limit of 5 per server is enforced in code.
CREATE TABLE IF NOT EXISTS discord_welcome_designs (
    id            BIGSERIAL   PRIMARY KEY,
    guild_id      BIGINT      NOT NULL,
    clone_key     BIGINT      NOT NULL DEFAULT -1,
    name          TEXT        NOT NULL,
    options_json  TEXT        NOT NULL,
    avatar_shape  TEXT        NOT NULL DEFAULT 'circle',
    created_by    BIGINT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS discord_welcome_designs_name_uq ON discord_welcome_designs (guild_id, clone_key, lower(name));

ALTER TABLE discord_welcome_config ADD COLUMN IF NOT EXISTS active_design_id BIGINT;
