-- Cat fights Phase 4D: per-server board toggles and fight reports. Additive and idempotent. Play coins only.
ALTER TABLE cat_guild_config ADD COLUMN IF NOT EXISTS fights_cross_server BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE cat_guild_config ADD COLUMN IF NOT EXISTS fights_show_server  BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS cat_fight_reports (
    id          BIGSERIAL PRIMARY KEY,
    fight_id    BIGINT NOT NULL,
    reporter_id BIGINT NOT NULL,
    reported_id BIGINT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (fight_id, reporter_id)
);
CREATE INDEX IF NOT EXISTS cat_fight_reports_reported_idx ON cat_fight_reports (reported_id, created_at);
