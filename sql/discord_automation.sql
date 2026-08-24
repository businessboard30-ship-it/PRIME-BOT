-- Reference copy of the schema database.py's _create_tables() creates
-- automatically on cold start. You do not need to run this by hand against
-- a normal deployment — it's here for anyone who prefers to provision the
-- database manually, and as documentation of the shape.
--
-- Phase 4 of the Discord expansion (automation polish). Scheduled
-- announcements are sent by api/cron_discord_announcements.py over
-- Discord's REST API, not the gateway process — see that file's docstring.

CREATE TABLE IF NOT EXISTS discord_autoresponders (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    clone_id INTEGER,
    trigger TEXT NOT NULL,
    response TEXT NOT NULL,
    created_by BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_discord_autoresponders_guild
    ON discord_autoresponders (guild_id, clone_id);

CREATE TABLE IF NOT EXISTS discord_scheduled_announcements (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    clone_id INTEGER,
    channel_id BIGINT NOT NULL,
    message TEXT NOT NULL,
    interval_minutes INTEGER,
    next_run_at TIMESTAMPTZ NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_by BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_discord_scheduled_announcements_due
    ON discord_scheduled_announcements (active, next_run_at);

-- Dashboard access tokens (spec §4 open question #5). No user-account/OAuth
-- system exists elsewhere in this repo — see database.py's
-- discord_dashboard_tokens comment for the capability-token trust model.
CREATE TABLE IF NOT EXISTS discord_dashboard_tokens (
    guild_id BIGINT NOT NULL,
    clone_id INTEGER,
    token TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS discord_dashboard_tokens_guild_clone_key
    ON discord_dashboard_tokens (guild_id, COALESCE(clone_id, -1));

