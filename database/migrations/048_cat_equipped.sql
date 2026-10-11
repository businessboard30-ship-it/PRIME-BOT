-- Cat cosmetics: which owned hat / collar / card background a player currently wears on their profile card.
-- One row per player and kind. Additive, idempotent. Ownership is re-checked whenever it is read.
CREATE TABLE IF NOT EXISTS cat_equipped (
    owner_id    BIGINT NOT NULL,
    clone_id    BIGINT,
    kind        TEXT NOT NULL CHECK (kind IN ('hat', 'collar', 'background')),
    cosmetic_id BIGINT NOT NULL REFERENCES cat_cosmetics(id) ON DELETE CASCADE,
    equipped_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    clone_key   BIGINT GENERATED ALWAYS AS (COALESCE(clone_id, -1)) STORED,
    PRIMARY KEY (owner_id, clone_key, kind)
);
