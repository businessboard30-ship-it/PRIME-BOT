-- Cat fights Phase 4, slice A: the Global Request Board data layer. Additive and idempotent. Play coins only.
-- A request escrows the poster's wager when it is posted; cancel/expire refunds it in the same transaction.
CREATE TABLE IF NOT EXISTS cat_fight_requests (
    id          BIGSERIAL PRIMARY KEY,
    clone_key   BIGINT NOT NULL DEFAULT -1,
    guild_id    BIGINT,
    server_name TEXT,
    poster_id   BIGINT NOT NULL,
    cat_id      BIGINT NOT NULL,
    wager       INT NOT NULL CHECK (wager > 0),
    power       INT NOT NULL DEFAULT 1,
    cross_server BOOLEAN NOT NULL DEFAULT FALSE,
    show_server  BOOLEAN NOT NULL DEFAULT FALSE,
    status      TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open','accepted','locked','fought','settled','cancelled','expired')),
    accepter_id BIGINT,
    fight_id    BIGINT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at  TIMESTAMPTZ NOT NULL,
    settled_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS cat_fight_requests_open_idx ON cat_fight_requests (clone_key, status, expires_at);
CREATE INDEX IF NOT EXISTS cat_fight_requests_poster_idx ON cat_fight_requests (poster_id, status);
CREATE INDEX IF NOT EXISTS cat_fight_requests_cat_idx ON cat_fight_requests (cat_id, status);

-- Per-player block list: blocked people never see each other's requests and can't accept them.
CREATE TABLE IF NOT EXISTS cat_blocks (
    owner_id   BIGINT NOT NULL,
    blocked_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (owner_id, blocked_id),
    CHECK (owner_id <> blocked_id)
);
CREATE INDEX IF NOT EXISTS cat_blocks_blocked_idx ON cat_blocks (blocked_id);
