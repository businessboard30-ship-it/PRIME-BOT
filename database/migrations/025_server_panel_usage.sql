-- path: database/migrations/025_server_panel_usage.sql
-- Server Owners Panel (Phase 4): one row per server (per bot) that has opened
-- the panel, so the owner Health screen can show how many servers use it.
-- Additive and idempotent like 001-024. Counts opens only; no user data.
CREATE TABLE IF NOT EXISTS server_panel_usage (
    guild_id        BIGINT NOT NULL,
    clone_id        INTEGER,
    first_opened_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_opened_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    opens           INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_server_panel_usage
    ON server_panel_usage (guild_id, (COALESCE(clone_id, -1)));
