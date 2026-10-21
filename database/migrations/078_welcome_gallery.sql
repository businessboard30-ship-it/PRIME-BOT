-- Welcome Card Studio Phase 2a: picture gallery (up to 10 per server) + rotation state. Additive and idempotent.
-- clone_key is -1 for the main bot (a NULL clone_id cannot be part of a unique key).
-- The old single picture (discord_welcome_config.custom_bg_*) is NOT touched; it stays as picture 1 fallback.
CREATE TABLE IF NOT EXISTS discord_welcome_backgrounds (
    id                    BIGSERIAL   PRIMARY KEY,
    guild_id              BIGINT      NOT NULL,
    clone_key             BIGINT      NOT NULL DEFAULT -1,
    slot                  SMALLINT    NOT NULL CHECK (slot BETWEEN 1 AND 10),
    channel_id            BIGINT,
    message_id            BIGINT,
    url_cache             TEXT,
    label                 TEXT,
    options_override_json TEXT,
    needs_new_picture     BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (guild_id, clone_key, slot)
);

ALTER TABLE discord_welcome_config ADD COLUMN IF NOT EXISTS rotation_mode TEXT;
ALTER TABLE discord_welcome_config ADD COLUMN IF NOT EXISTS rotation_counter BIGINT NOT NULL DEFAULT 0;
ALTER TABLE discord_welcome_config ADD COLUMN IF NOT EXISTS last_slot_used SMALLINT;
