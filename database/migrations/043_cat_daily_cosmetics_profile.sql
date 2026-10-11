-- Cat game Phase 1: daily care, cosmetics, and public profiles.
CREATE TABLE IF NOT EXISTS cat_daily_care (
    owner_id BIGINT NOT NULL,
    clone_id BIGINT,
    care_date DATE NOT NULL,
    streak INTEGER NOT NULL DEFAULT 1 CHECK (streak > 0),
    coins_reward INTEGER NOT NULL DEFAULT 0 CHECK (coins_reward >= 0),
    diamonds_reward INTEGER NOT NULL DEFAULT 0 CHECK (diamonds_reward >= 0),
    cared_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    clone_key BIGINT GENERATED ALWAYS AS (COALESCE(clone_id, -1)) STORED,
    PRIMARY KEY (owner_id, clone_key, care_date)
);

CREATE TABLE IF NOT EXISTS cat_cosmetics (
    id BIGSERIAL PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('hat', 'collar', 'background')),
    name TEXT NOT NULL,
    emoji TEXT NOT NULL,
    price_coins INTEGER NOT NULL DEFAULT 0 CHECK (price_coins >= 0),
    price_diamonds INTEGER NOT NULL DEFAULT 0 CHECK (price_diamonds >= 0),
    UNIQUE (kind, name)
);

CREATE TABLE IF NOT EXISTS cat_cosmetic_ownership (
    owner_id BIGINT NOT NULL,
    clone_id BIGINT,
    cosmetic_id BIGINT NOT NULL REFERENCES cat_cosmetics(id) ON DELETE CASCADE,
    purchased_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    clone_key BIGINT GENERATED ALWAYS AS (COALESCE(clone_id, -1)) STORED,
    PRIMARY KEY (owner_id, clone_key, cosmetic_id)
);

CREATE TABLE IF NOT EXISTS cat_profiles (
    owner_id BIGINT NOT NULL,
    clone_id BIGINT,
    bio TEXT NOT NULL DEFAULT '',
    pinned_cat_ids BIGINT[] NOT NULL DEFAULT '{}',
    show_profile BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    clone_key BIGINT GENERATED ALWAYS AS (COALESCE(clone_id, -1)) STORED,
    PRIMARY KEY (owner_id, clone_key)
);

INSERT INTO cat_cosmetics (slug, kind, name, emoji, price_coins, price_diamonds) VALUES
 ('strawberry-hat', 'hat', 'Strawberry Hat', '🍓', 500, 0),
 ('moon-collar', 'collar', 'Moon Collar', '🌙', 1000, 2),
 ('royal-crown', 'hat', 'Royal Crown', '👑', 0, 8),
 ('sunset-card', 'background', 'Sunset Card', '🌅', 1500, 0),
 ('galaxy-card', 'background', 'Galaxy Card', '🌌', 0, 12)
ON CONFLICT (slug) DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_cat_daily_care_owner ON cat_daily_care(owner_id, clone_id, care_date DESC);
CREATE INDEX IF NOT EXISTS idx_cat_cosmetic_ownership_owner ON cat_cosmetic_ownership(owner_id, clone_id);
CREATE INDEX IF NOT EXISTS idx_cat_profiles_public ON cat_profiles(show_profile) WHERE show_profile;

COMMENT ON TABLE cat_daily_care IS 'Idempotent UTC daily care claims and streak rewards.';
COMMENT ON TABLE cat_cosmetic_ownership IS 'Cosmetics are account-owned, never attached to a cat that can be sold.';
COMMENT ON TABLE cat_profiles IS 'Public showcase metadata; pinned cat IDs are validated against current ownership before display.';

-- The database.py schema guard must be bumped from 87 to 88 for this migration.
