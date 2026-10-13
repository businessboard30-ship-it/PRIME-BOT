-- Cat Life: earning cats, daily home rent, walks, heists and lucky games (play coins only, never real money).
-- Additive/idempotent.

-- Per person, per bot (the cats are per bot, the wallet is shared): the home rent and the last earnings pickup.
CREATE TABLE IF NOT EXISTS cat_life_state (
    owner_id     BIGINT NOT NULL,
    clone_key    BIGINT NOT NULL DEFAULT -1,
    home_since   TIMESTAMPTZ,            -- start of the current paid-rent streak
    home_until   TIMESTAMPTZ,            -- rent is paid up to here
    collected_at TIMESTAMPTZ,            -- earnings were last picked up
    PRIMARY KEY (owner_id, clone_key)
);

-- Per person per UTC day (the wallet is shared, so the daily limits are too).
CREATE TABLE IF NOT EXISTS cat_life_daily (
    owner_id   BIGINT NOT NULL,
    day        DATE   NOT NULL,
    gamble_net BIGINT NOT NULL DEFAULT 0,      -- coins won minus coins bet today (negative = a loss)
    heists     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (owner_id, day)
);

-- A cat out on a walk or a heist. The result is rolled when it leaves and paid when the owner claims it.
CREATE TABLE IF NOT EXISTS cat_outings (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL,
    clone_key   BIGINT NOT NULL DEFAULT -1,
    adoption_id BIGINT NOT NULL,
    kind        TEXT   NOT NULL CHECK (kind IN ('walk', 'heist')),
    route       TEXT   NOT NULL,
    fee         BIGINT NOT NULL CHECK (fee >= 0),
    payout      BIGINT NOT NULL DEFAULT 0 CHECK (payout >= 0),
    diamonds    INTEGER NOT NULL DEFAULT 0 CHECK (diamonds >= 0),
    success     BOOLEAN NOT NULL DEFAULT TRUE,
    story       TEXT   NOT NULL DEFAULT '',
    started_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ends_at     TIMESTAMPTZ NOT NULL,
    claimed_at  TIMESTAMPTZ
);
-- A cat can only be out once at a time, and a person's open outings are looked up often.
CREATE UNIQUE INDEX IF NOT EXISTS ux_cat_outings_open_cat ON cat_outings (adoption_id) WHERE claimed_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_cat_outings_open_owner ON cat_outings (owner_id, clone_key) WHERE claimed_at IS NULL;
