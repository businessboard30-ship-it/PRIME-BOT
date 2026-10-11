-- Cat game Phase 2: daily quests, timed auctions, and collection badges.
CREATE TABLE IF NOT EXISTS cat_daily_quests (
    owner_id BIGINT NOT NULL,
    clone_id BIGINT,
    quest_date DATE NOT NULL,
    quest_key TEXT NOT NULL,
    progress INTEGER NOT NULL DEFAULT 0 CHECK (progress >= 0),
    target INTEGER NOT NULL CHECK (target > 0),
    diamonds_reward INTEGER NOT NULL CHECK (diamonds_reward > 0),
    claimed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    clone_key BIGINT GENERATED ALWAYS AS (COALESCE(clone_id, -1)) STORED,
    PRIMARY KEY (owner_id, clone_key, quest_date, quest_key)
);

CREATE TABLE IF NOT EXISTS cat_auctions (
    id BIGSERIAL PRIMARY KEY,
    adoption_id BIGINT NOT NULL REFERENCES cat_adoptions(id) ON DELETE CASCADE,
    seller_id BIGINT NOT NULL,
    seller_clone_id BIGINT,
    guild_id BIGINT,
    channel_id BIGINT,
    message_id BIGINT,
    starting_price BIGINT NOT NULL CHECK (starting_price > 0),
    ends_at TIMESTAMPTZ NOT NULL,
    highest_bid BIGINT,
    highest_bidder_id BIGINT,
    highest_bidder_clone_id BIGINT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','sold','cancelled','void')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_cat_auctions_open ON cat_auctions(adoption_id) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_cat_auctions_open_end ON cat_auctions(status, ends_at);

CREATE TABLE IF NOT EXISTS cat_badges (
    slug TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    emoji TEXT NOT NULL,
    description TEXT NOT NULL,
    reward_diamonds INTEGER NOT NULL DEFAULT 0 CHECK (reward_diamonds >= 0)
);
CREATE TABLE IF NOT EXISTS cat_badge_awards (
    owner_id BIGINT NOT NULL,
    clone_id BIGINT,
    badge_slug TEXT NOT NULL REFERENCES cat_badges(slug) ON DELETE CASCADE,
    awarded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    clone_key BIGINT GENERATED ALWAYS AS (COALESCE(clone_id, -1)) STORED,
    PRIMARY KEY (owner_id, clone_key, badge_slug)
);
INSERT INTO cat_badges(slug,name,emoji,description,reward_diamonds) VALUES
 ('coat-collector','Coat Collector','🎨','Adopt five different traits.',2),
 ('rare-hunter','Rare Hunter','🌟','Own a rare or legendary cat.',3),
 ('full-house','Full House','🏡','Keep ten cats in your collection.',5)
ON CONFLICT (slug) DO NOTHING;

COMMENT ON TABLE cat_auctions IS '24-hour cat auctions; the listed cat remains owned by the seller until settlement.';
COMMENT ON TABLE cat_badge_awards IS 'Idempotent collection achievements and their one-time diamond rewards.';
-- database.py schema guard: 88 -> 89.
