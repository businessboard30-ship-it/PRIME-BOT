-- path: database/migrations/024_server_panel.sql
-- Server Owners Panel (Phase 1): per-server change history. Additive and
-- idempotent like 001-023. Every row is scoped by guild_id + clone_id so a
-- clone's panel never reads or shows the main bot's history.
CREATE TABLE IF NOT EXISTS server_panel_audit (
    id          BIGSERIAL PRIMARY KEY,
    guild_id    BIGINT NOT NULL,
    clone_id    INTEGER,
    actor_id    BIGINT NOT NULL,
    setting_key TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_server_panel_audit_guild
    ON server_panel_audit (guild_id, clone_id, created_at DESC);
