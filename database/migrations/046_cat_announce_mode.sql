-- Cat game: per-server choice for the public cat posts (all / milestones only / off). Additive/idempotent.
-- Default 'all' keeps today's behaviour for every existing server.
ALTER TABLE cat_guild_config ADD COLUMN IF NOT EXISTS announce_mode TEXT NOT NULL DEFAULT 'all'
    CHECK (announce_mode IN ('all', 'milestones', 'off'));
