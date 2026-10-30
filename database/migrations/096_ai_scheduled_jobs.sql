-- path: database/migrations/096_ai_scheduled_jobs.sql

-- ============================================================================
-- Migration 096: persistent AI scheduler (Phase 3 of docs/PRIME-BOT_Autonomous_AI_Phased_Plan.md). Additive and idempotent.
--   ai_scheduled_jobs : one row per scheduled job. `steps` is a JSON list of allow-listed steps (see modules/ai_jobs.py).
--                       status 'running' + a past locked_until = the bot died mid-run; that run is marked 'interrupted' and
--                       NEVER re-run automatically (at-most-once).
--   ai_job_runs       : execution history (latest 50 kept per job by the application).
-- Required by: modules/ai_jobs.py, discord_bot/cogs/ai_jobs.py, database.py (ai job methods)
-- ============================================================================

CREATE TABLE IF NOT EXISTS ai_scheduled_jobs (
    id                   BIGSERIAL PRIMARY KEY,
    guild_id             BIGINT      NOT NULL,
    clone_id             INTEGER,
    created_by           BIGINT      NOT NULL,
    title                TEXT        NOT NULL,
    steps                JSONB       NOT NULL,
    schedule_kind        TEXT        NOT NULL CHECK (schedule_kind IN ('once', 'interval', 'daily')),
    interval_seconds     INTEGER,
    daily_at             TEXT,
    next_run_at          TIMESTAMPTZ NOT NULL,
    enabled              BOOLEAN     NOT NULL DEFAULT TRUE,
    status               TEXT        NOT NULL DEFAULT 'scheduled',
    consecutive_failures INTEGER     NOT NULL DEFAULT 0,
    run_count            INTEGER     NOT NULL DEFAULT 0,
    last_run_at          TIMESTAMPTZ,
    last_status          TEXT,
    last_error           TEXT,
    locked_until         TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ai_scheduled_jobs_due_idx   ON ai_scheduled_jobs (enabled, next_run_at);
CREATE INDEX IF NOT EXISTS ai_scheduled_jobs_guild_idx ON ai_scheduled_jobs (guild_id, clone_id);

CREATE TABLE IF NOT EXISTS ai_job_runs (
    run_id      BIGSERIAL PRIMARY KEY,
    job_id      BIGINT      NOT NULL REFERENCES ai_scheduled_jobs(id) ON DELETE CASCADE,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status      TEXT        NOT NULL,
    report      TEXT
);
CREATE INDEX IF NOT EXISTS ai_job_runs_job_idx ON ai_job_runs (job_id, started_at DESC);
