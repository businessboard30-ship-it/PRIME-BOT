-- path: database/migrations/021_admin_access.sql
-- Owner-panel Batch 2: helper accounts with per-section access. Additive and
-- idempotent like 001-020. Real owners stay in config.py (DISCORD_CLONE_ADMIN_IDS)
-- and are never stored or editable here; this table only holds *helpers*.
CREATE TABLE IF NOT EXISTS admin_panel_helpers (
    user_id     BIGINT PRIMARY KEY,
    sections    TEXT NOT NULL DEFAULT '',          -- comma-separated section keys
    added_by    BIGINT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
