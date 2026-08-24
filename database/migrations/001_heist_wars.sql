-- Heist Wars v1 — initial schema.
--
-- Siloed per (guild_id, clone_id) the same way discord_economy_balances is
-- (see sql/discord_economy.sql) — a clone owner running the bot across many
-- guilds gets a separate heist progression per guild.
--
-- heist_locations is reference/seed data mirroring game/locations.py.
-- Content lives in code (LOCATIONS dict) as the source of truth for game
-- logic; this table exists so SQL-level constraints and admin tooling have
-- something to query/join against, and is reseeded idempotently below.

BEGIN;

CREATE TABLE IF NOT EXISTS heist_locations (
    location_key        TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    required_level       INTEGER NOT NULL CHECK (required_level >= 1),
    difficulty_penalty   INTEGER NOT NULL CHECK (difficulty_penalty >= 0),
    base_chance          INTEGER NOT NULL CHECK (base_chance BETWEEN 0 AND 100),
    min_reward_cash      BIGINT NOT NULL CHECK (min_reward_cash >= 0),
    max_reward_cash      BIGINT NOT NULL,
    min_reward_xp        INTEGER NOT NULL CHECK (min_reward_xp >= 0),
    max_reward_xp        INTEGER NOT NULL,
    cooldown_seconds     INTEGER NOT NULL CHECK (cooldown_seconds >= 0),
    min_crew             INTEGER NOT NULL DEFAULT 1 CHECK (min_crew >= 1),
    max_crew             INTEGER NOT NULL DEFAULT 1,
    CONSTRAINT heist_locations_cash_range CHECK (max_reward_cash >= min_reward_cash),
    CONSTRAINT heist_locations_xp_range CHECK (max_reward_xp >= min_reward_xp),
    CONSTRAINT heist_locations_crew_range CHECK (max_crew >= min_crew)
);

CREATE TABLE IF NOT EXISTS heist_players (
    guild_id       BIGINT NOT NULL,
    clone_id       INTEGER,
    user_id        BIGINT NOT NULL,
    level          INTEGER NOT NULL DEFAULT 1 CHECK (level >= 1),
    xp             BIGINT NOT NULL DEFAULT 0 CHECK (xp >= 0),
    intel          BIGINT NOT NULL DEFAULT 0 CHECK (intel >= 0),
    reputation     BIGINT NOT NULL DEFAULT 0 CHECK (reputation >= 0),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS heist_players_identity_key
    ON heist_players (guild_id, COALESCE(clone_id, -1), user_id);

-- One row per attempted heist run. status is the state machine
-- (game.models.HeistState). The partial unique index below is the
-- server-side cooldown/"one active run at a time" guarantee: a player
-- cannot have two non-terminal runs at once, enforced by Postgres itself
-- rather than only application code.
CREATE TABLE IF NOT EXISTS heist_runs (
    id                      BIGSERIAL PRIMARY KEY,
    guild_id                BIGINT NOT NULL,
    clone_id                INTEGER,
    user_id                 BIGINT NOT NULL,
    location_key            TEXT NOT NULL REFERENCES heist_locations(location_key),
    approach                TEXT NOT NULL CHECK (approach IN ('stealth', 'loud', 'technical')),
    level_at_start          INTEGER NOT NULL CHECK (level_at_start >= 1),
    status                  TEXT NOT NULL CHECK (
                                 status IN ('PLANNING','INFILTRATION','OBJECTIVE','LOOT','ESCAPE',
                                            'COMPLETED','FAILED','EXPIRED')
                             ),
    decision_modifier_total INTEGER NOT NULL DEFAULT 0,
    random_modifier         INTEGER CHECK (random_modifier IS NULL OR random_modifier BETWEEN 0 AND 99),
    final_success_chance    INTEGER CHECK (final_success_chance IS NULL OR final_success_chance BETWEEN 0 AND 100),
    succeeded               BOOLEAN,
    reward_cash             BIGINT CHECK (reward_cash IS NULL OR reward_cash >= 0),
    reward_xp               INTEGER CHECK (reward_xp IS NULL OR reward_xp >= 0),
    reward_intel            INTEGER CHECK (reward_intel IS NULL OR reward_intel >= 0),
    reward_reputation       INTEGER CHECK (reward_reputation IS NULL OR reward_reputation >= 0),
    reward_granted          BOOLEAN NOT NULL DEFAULT FALSE,
    idempotency_key         TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at            TIMESTAMPTZ
);

-- Enforces "one active (non-terminal) run per player" server-side —
-- cooldowns and "already have an active heist" checks fall back on this
-- even if application logic is bypassed.
CREATE UNIQUE INDEX IF NOT EXISTS heist_runs_one_active_per_player
    ON heist_runs (guild_id, COALESCE(clone_id, -1), user_id)
    WHERE status NOT IN ('COMPLETED', 'FAILED', 'EXPIRED');

CREATE INDEX IF NOT EXISTS idx_heist_runs_cooldown_lookup
    ON heist_runs (guild_id, COALESCE(clone_id, -1), user_id, location_key, completed_at DESC)
    WHERE status IN ('COMPLETED', 'FAILED');

CREATE UNIQUE INDEX IF NOT EXISTS heist_runs_idempotency_key
    ON heist_runs (idempotency_key) WHERE idempotency_key IS NOT NULL;

-- Participants — v1 is solo-only (LocationDefinition.max_crew defaults to
-- 1 for every seeded location), but the table exists per the brief's
-- minimum schema and future multi-crew content only needs new rows here,
-- not a schema change.
CREATE TABLE IF NOT EXISTS heist_participants (
    run_id     BIGINT NOT NULL REFERENCES heist_runs(id) ON DELETE CASCADE,
    user_id    BIGINT NOT NULL,
    role       TEXT NOT NULL DEFAULT 'owner' CHECK (role IN ('owner', 'crew')),
    joined_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (run_id, user_id)
);

-- Auditable event history. event_key/choice_key are nullable because
-- non-event rows (heist_started, phase_changed, reward_granted, ...) don't
-- have one. No free-text player input is ever stored here (see brief §20).
CREATE TABLE IF NOT EXISTS heist_events_log (
    id           BIGSERIAL PRIMARY KEY,
    run_id       BIGINT NOT NULL REFERENCES heist_runs(id) ON DELETE CASCADE,
    action       TEXT NOT NULL,
    phase        TEXT,
    event_key    TEXT,
    choice_key   TEXT,
    modifier     INTEGER,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_heist_events_log_run
    ON heist_events_log (run_id, created_at);

-- Reward ledger. One row per completed run's payout, keyed by run_id so a
-- duplicate/retried completion request can never insert a second payout
-- for the same run (see heist_service.complete_heist).
CREATE TABLE IF NOT EXISTS heist_reward_ledger (
    id           BIGSERIAL PRIMARY KEY,
    run_id       BIGINT NOT NULL UNIQUE REFERENCES heist_runs(id) ON DELETE CASCADE,
    guild_id     BIGINT NOT NULL,
    clone_id     INTEGER,
    user_id      BIGINT NOT NULL,
    cash         BIGINT NOT NULL CHECK (cash >= 0),
    xp           INTEGER NOT NULL CHECK (xp >= 0),
    intel        INTEGER NOT NULL CHECK (intel >= 0),
    reputation   INTEGER NOT NULL CHECK (reputation >= 0),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed data mirroring game/locations.py LOCATIONS. Kept in sync manually;
-- game logic reads from code, not this table, so a mismatch cannot break
-- gameplay, only admin-facing SQL views.
INSERT INTO heist_locations
    (location_key, name, required_level, difficulty_penalty, base_chance,
     min_reward_cash, max_reward_cash, min_reward_xp, max_reward_xp, cooldown_seconds)
VALUES
    ('convenience_store', 'Convenience Store', 1, 5, 70, 50, 200, 10, 25, 1800),
    ('jewelry_store', 'Jewelry Store', 5, 20, 55, 300, 900, 40, 80, 7200),
    ('bank_vault', 'Bank Vault', 12, 35, 45, 1000, 3000, 100, 200, 21600),
    ('casino_vault', 'Casino Vault', 20, 45, 40, 2500, 7000, 200, 400, 43200)
ON CONFLICT (location_key) DO UPDATE SET
    name = EXCLUDED.name,
    required_level = EXCLUDED.required_level,
    difficulty_penalty = EXCLUDED.difficulty_penalty,
    base_chance = EXCLUDED.base_chance,
    min_reward_cash = EXCLUDED.min_reward_cash,
    max_reward_cash = EXCLUDED.max_reward_cash,
    min_reward_xp = EXCLUDED.min_reward_xp,
    max_reward_xp = EXCLUDED.max_reward_xp,
    cooldown_seconds = EXCLUDED.cooldown_seconds;

COMMIT;
