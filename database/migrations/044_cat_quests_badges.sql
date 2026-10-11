-- Cat game: daily quests (diamond rewards) and collection badges. Additive/idempotent.
CREATE TABLE IF NOT EXISTS cat_daily_quests (
    owner_id        BIGINT NOT NULL,
    clone_id        BIGINT,
    quest_date      DATE NOT NULL,
    quest_key       TEXT NOT NULL,
    progress        INTEGER NOT NULL DEFAULT 0 CHECK (progress >= 0),
    target          INTEGER NOT NULL CHECK (target > 0),
    diamonds_reward INTEGER NOT NULL CHECK (diamonds_reward > 0),
    claimed_at      TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    clone_key       BIGINT GENERATED ALWAYS AS (COALESCE(clone_id, -1)) STORED,
    PRIMARY KEY (owner_id, clone_key, quest_date, quest_key)
);

-- One row per badge a person has earned on one bot. The reward is paid in the same transaction as this insert, so it
-- can never be paid twice.
CREATE TABLE IF NOT EXISTS cat_badge_awards (
    owner_id   BIGINT NOT NULL,
    clone_id   BIGINT,
    badge_slug TEXT NOT NULL,
    awarded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    clone_key  BIGINT GENERATED ALWAYS AS (COALESCE(clone_id, -1)) STORED,
    PRIMARY KEY (owner_id, clone_key, badge_slug)
);
