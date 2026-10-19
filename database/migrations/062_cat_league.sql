-- Cat fights Phase 5A: League data layer (seasons, entries). Additive and idempotent. Play coins only.
CREATE TABLE IF NOT EXISTS cat_league_seasons (
    id         BIGSERIAL PRIMARY KEY,
    clone_key  BIGINT NOT NULL DEFAULT -1,
    starts_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ends_at    TIMESTAMPTZ NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','closed'))
);
CREATE UNIQUE INDEX IF NOT EXISTS cat_league_one_active_idx ON cat_league_seasons (clone_key) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS cat_league_entries (
    season_id  BIGINT NOT NULL REFERENCES cat_league_seasons(id) ON DELETE CASCADE,
    owner_id   BIGINT NOT NULL,
    division   INT NOT NULL CHECK (division BETWEEN 0 AND 4),
    points     INT NOT NULL DEFAULT 0,
    wins       INT NOT NULL DEFAULT 0,
    losses     INT NOT NULL DEFAULT 0,
    fee        INT NOT NULL DEFAULT 0,
    joined_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (season_id, owner_id)
);
CREATE INDEX IF NOT EXISTS cat_league_entries_rank_idx ON cat_league_entries (season_id, division, points DESC);
