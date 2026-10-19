-- Cat Arena (Phase 3 of the fights plan): opt-in PvP, challenges, fight log, effects. Additive and idempotent. Play coins only.
-- Rating and the Arena switch are per bot (bot_key = -1 for the main bot, the clone id for a clone); daily limits are per person.
CREATE TABLE IF NOT EXISTS cat_arena (
    owner_id    BIGINT NOT NULL,
    bot_key     BIGINT NOT NULL DEFAULT -1,
    arena_on    BOOLEAN NOT NULL DEFAULT FALSE,
    rating      INT NOT NULL DEFAULT 1000 CHECK (rating >= 0),
    wins        INT NOT NULL DEFAULT 0 CHECK (wins >= 0),
    losses      INT NOT NULL DEFAULT 0 CHECK (losses >= 0),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (owner_id, bot_key)
);
CREATE TABLE IF NOT EXISTS cat_arena_daily (
    owner_id  BIGINT NOT NULL,
    day       DATE NOT NULL,
    fights    INT NOT NULL DEFAULT 0 CHECK (fights >= 0),
    net       BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (owner_id, day)
);
CREATE TABLE IF NOT EXISTS cat_fights (
    id              BIGSERIAL PRIMARY KEY,
    mode            TEXT NOT NULL DEFAULT 'local',
    clone_key       BIGINT NOT NULL DEFAULT -1,
    guild_id        BIGINT,
    challenger_id   BIGINT NOT NULL,
    challenged_id   BIGINT NOT NULL,
    challenger_cat  BIGINT NOT NULL,
    challenged_cat  BIGINT,
    wager           BIGINT NOT NULL CHECK (wager > 0),
    fee             BIGINT NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'pending',   -- pending | settled | declined | cancelled | expired | void
    seed            BIGINT,
    winner_id       BIGINT,
    ended_by        TEXT,
    snap            TEXT,                              -- both fighter snapshots (JSON), so the seed replays the fight
    day             DATE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL,
    settled_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_cat_fights_challenged ON cat_fights (challenged_id, status);
CREATE INDEX IF NOT EXISTS ix_cat_fights_challenger ON cat_fights (challenger_id, status);
CREATE TABLE IF NOT EXISTS cat_effects (
    cat_id      BIGINT NOT NULL REFERENCES cat_adoptions(id) ON DELETE CASCADE,
    effect_key  TEXT NOT NULL,                         -- tired | hurt (items come in a later phase)
    ends_at     TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (cat_id, effect_key)
);
