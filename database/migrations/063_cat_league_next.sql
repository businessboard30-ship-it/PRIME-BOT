-- Cat fights Phase 5B: the division a player starts the next League season in (set when a season closes). Additive.
CREATE TABLE IF NOT EXISTS cat_league_next (
    clone_key BIGINT NOT NULL,
    owner_id  BIGINT NOT NULL,
    division  INT NOT NULL CHECK (division BETWEEN 0 AND 4),
    PRIMARY KEY (clone_key, owner_id)
);
