-- Cat stats and skills (Phase 1 of the fights plan). Additive and idempotent. Stats are keyed by cat id, so they stay with the cat when it is traded.
-- Points to spend are derived from rarity + rank (not stored), so ranking up gives more points with no extra write.
CREATE TABLE IF NOT EXISTS cat_stats (
    cat_id        BIGINT PRIMARY KEY REFERENCES cat_adoptions(id) ON DELETE CASCADE,
    strength      INT NOT NULL DEFAULT 0 CHECK (strength >= 0),
    guard         INT NOT NULL DEFAULT 0 CHECK (guard >= 0),
    speed         INT NOT NULL DEFAULT 0 CHECK (speed >= 0),
    spirit        INT NOT NULL DEFAULT 0 CHECK (spirit >= 0),
    respec_count  INT NOT NULL DEFAULT 0 CHECK (respec_count >= 0),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS cat_skills (
    cat_id     BIGINT NOT NULL REFERENCES cat_adoptions(id) ON DELETE CASCADE,
    slot       INT NOT NULL CHECK (slot BETWEEN 1 AND 3),
    skill_key  TEXT NOT NULL,
    PRIMARY KEY (cat_id, slot)
);
