-- Additive migration for the Discord port. Safe to run against the same
-- Postgres database the Telegram bot uses — does not touch any existing
-- table, column, or row.

-- Per-guild premium tier config: each Discord server that wants its own
-- paywalled premium role gets one row. fee_ghs falls back to
-- config.PREMIUM_GROUP_FEE_GHS in application code when NULL, so this row
-- doesn't need to exist until an admin actually customizes it via /setprice.
CREATE TABLE IF NOT EXISTS discord_guild_premium (
    guild_id   BIGINT PRIMARY KEY,
    role_id    BIGINT,
    fee_ghs    NUMERIC,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Generic admin-action audit log. Used first by /verify (manually marking a
-- user as paid, bypassing payment) but written generically enough to record
-- other admin bypass actions later without another migration.
CREATE TABLE IF NOT EXISTS admin_action_log (
    id              SERIAL PRIMARY KEY,
    admin_id        BIGINT NOT NULL,
    target_user_id  BIGINT NOT NULL,
    action          TEXT NOT NULL,
    chat_id         BIGINT,
    reason          TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_admin_action_log_target ON admin_action_log (target_user_id);
