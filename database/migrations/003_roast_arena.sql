-- path: database/migrations/003_roast_arena.sql
--
-- Inter-server roast battles (see discord_bot/cogs/roast_arena.py). This is a
-- SEPARATE feature from the single-server bot-vs-member roast in
-- discord_bot/cogs/roast.py / discord_roast_* tables — nothing here touches
-- those. Same idempotent, additive-only pattern as 001_heist_wars.sql and
-- 002_heist_items.sql: every statement is IF NOT EXISTS / ON CONFLICT safe so
-- a cold start provisions it exactly once and re-runs are no-ops.
--
-- Three tables:
--   discord_roast_arena_config    — per-guild opt-in state (consent gate,
--                                    dont_ask_again / remind_after suppression
--                                    for event invites, and an optional custom
--                                    battleground channel; NULL there means the
--                                    support server is used as the neutral
--                                    battleground instead).
--   discord_roast_arena_challenges — one row per challenge/battle lifecycle,
--                                    from 'pending_approval' through to
--                                    'completed' / 'expired' / 'declined'.
--   discord_roast_arena_votes      — one row per (challenge, voter). The unique
--                                    index enforces one vote per user per
--                                    challenge; the cog upserts on it so a voter
--                                    can CHANGE their pick up until 0:00.
--
-- clone_id is carried everywhere for the same reason as the discord_roast_*
-- tables: each clone is a separate bot instance and must never see another
-- clone's arena state.

CREATE TABLE IF NOT EXISTS discord_roast_arena_config (
    guild_id BIGINT NOT NULL,
    clone_id INTEGER,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    consent_prompted BOOLEAN NOT NULL DEFAULT FALSE,
    dont_ask_again BOOLEAN NOT NULL DEFAULT FALSE,
    remind_after TIMESTAMPTZ,
    battleground_channel_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- COALESCE(clone_id, -1) unique key mirrors discord_roast_config so main-bot
-- rows (clone_id IS NULL) and clone rows never collide.
CREATE UNIQUE INDEX IF NOT EXISTS discord_roast_arena_config_guild_clone_key
    ON discord_roast_arena_config (guild_id, COALESCE(clone_id, -1));

CREATE TABLE IF NOT EXISTS discord_roast_arena_challenges (
    id SERIAL PRIMARY KEY,
    clone_id INTEGER,
    challenger_guild_id BIGINT NOT NULL,
    challenger_user_id BIGINT NOT NULL,
    challenged_guild_id BIGINT NOT NULL,
    -- The member who ran /roast challenge defaults to being the challenger's
    -- contestant; the challenged contestant is filled in when someone in the
    -- challenged server clicks "accept".
    challenger_contestant_id BIGINT,
    challenged_contestant_id BIGINT,
    -- Where the showdown is actually hosted (resolved at battle start): a
    -- guild's custom channel or the support server fallback.
    battleground_guild_id BIGINT,
    battleground_channel_id BIGINT,
    panel_message_id BIGINT,
    -- pending_approval -> awaiting_accept -> active -> completed
    --                  \-> declined        \-> expired
    status TEXT NOT NULL DEFAULT 'pending_approval',
    winner_side TEXT,  -- 'challenger' | 'challenged' | 'draw' | NULL
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    battle_ends_at TIMESTAMPTZ,
    resolved_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS discord_roast_arena_challenges_status_idx
    ON discord_roast_arena_challenges (status, expires_at);
CREATE INDEX IF NOT EXISTS discord_roast_arena_challenges_channel_idx
    ON discord_roast_arena_challenges (battleground_channel_id, status);

CREATE TABLE IF NOT EXISTS discord_roast_arena_votes (
    id SERIAL PRIMARY KEY,
    challenge_id INTEGER NOT NULL REFERENCES discord_roast_arena_challenges(id) ON DELETE CASCADE,
    voter_user_id BIGINT NOT NULL,
    choice TEXT NOT NULL CHECK (choice IN ('challenger', 'challenged')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- One vote per user per challenge. The cog does an upsert on this key so a
-- voter changing their mind updates their existing row instead of stacking.
CREATE UNIQUE INDEX IF NOT EXISTS discord_roast_arena_votes_unique
    ON discord_roast_arena_votes (challenge_id, voter_user_id);
