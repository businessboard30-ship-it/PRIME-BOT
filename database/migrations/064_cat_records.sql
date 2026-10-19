-- Cat fights R3: each cat keeps a fight record (Arena and request fights). Additive.
CREATE TABLE IF NOT EXISTS cat_records (
    cat_id        BIGINT PRIMARY KEY,
    fights        INT NOT NULL DEFAULT 0,
    wins          INT NOT NULL DEFAULT 0,
    losses        INT NOT NULL DEFAULT 0,
    kos           INT NOT NULL DEFAULT 0,
    streak        INT NOT NULL DEFAULT 0,
    best_streak   INT NOT NULL DEFAULT 0,
    last_fight_at TIMESTAMPTZ
);
