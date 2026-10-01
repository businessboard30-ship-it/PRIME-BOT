-- path: database/migrations/022_admin_safety.sql
-- Owner-panel Batch 3 (moderation & safety). Additive and idempotent like 001-021.

-- Review state for public "report this server" submissions. Existing rows
-- become 'new'; the poller in report_notifications.py only uses `notified`
-- and is untouched by this.
ALTER TABLE server_listing_reports ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'new';
ALTER TABLE server_listing_reports ADD COLUMN IF NOT EXISTS reviewed_by BIGINT;
ALTER TABLE server_listing_reports ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ;

-- Custom status rotation entries. No rows = the built-in rotation in bot.py.
CREATE TABLE IF NOT EXISTS bot_status_entries (
    id          BIGSERIAL PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('playing', 'watching', 'listening', 'competing')),
    text        TEXT NOT NULL,
    created_by  BIGINT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Tiny key/value settings for the status editor (currently just 'presence').
CREATE TABLE IF NOT EXISTS bot_status_config (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_by  BIGINT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
