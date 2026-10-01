-- path: database/migrations/020_admin_controls.sql
-- Owner-panel controls (Batch 1): persistent audit log, kill switches and a
-- global user/server blacklist. Additive and idempotent like 001-019.

-- One row per owner-panel action (written by _views_admin_panel.audit()).
CREATE TABLE IF NOT EXISTS admin_panel_audit (
    id          BIGSERIAL PRIMARY KEY,
    admin_id    BIGINT NOT NULL,
    action      TEXT NOT NULL,
    guild_id    BIGINT,
    details     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_admin_panel_audit_created ON admin_panel_audit (created_at DESC);

-- Kill switches. A row with engaged = TRUE means the switch is PULLED: for a
-- feature key that feature's slash commands are off, for the special key
-- 'maintenance' every slash command is off (owners always bypass).
CREATE TABLE IF NOT EXISTS bot_kill_switches (
    switch      TEXT PRIMARY KEY,
    engaged     BOOLEAN NOT NULL DEFAULT TRUE,
    updated_by  BIGINT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Global blacklist (applies to the main bot and every clone).
CREATE TABLE IF NOT EXISTS bot_blacklist (
    kind        TEXT NOT NULL CHECK (kind IN ('user', 'guild')),
    target_id   BIGINT NOT NULL,
    reason      TEXT,
    added_by    BIGINT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (kind, target_id)
);
