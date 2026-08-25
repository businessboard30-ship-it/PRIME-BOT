# FULL PATH: PRIME-BOT-main/database/migrations/005_roast_arena_outbox.sql

-- path: database/migrations/005_roast_arena_outbox.sql
--
-- Cross-clone roast arena relay (see discord_bot/cogs/roast_arena.py
-- _drain_arena_actions and database.py enqueue/claim/complete_roast_arena_action).
--
-- Each clone (and the main bot) is a separate Discord gateway connection in
-- its own process, so self.bot.get_guild(...) only returns a guild that
-- THIS process is actually connected to. list_optedin_roast_arena_guilds now
-- supports any_clone=True to pool candidates across every clone + the main
-- bot, but pooling the data alone doesn't let one process act on a guild only
-- another process holds. This table is the relay: whichever process needs to
-- act on a guild it can't see writes a row here instead of calling
-- self.bot.get_guild() directly, and every process's existing 30s _poller
-- tick claims + executes any row targeting a guild it currently has cached.
--
-- Same idempotent, additive-only pattern as the other migrations.
--
--   discord_roast_arena_outbox — one row per cross-process action:
--     target_guild_id — the guild the action must be performed in.
--     action_type      — 'dm_challenge_approval' | 'notify_decline' | 'event_invite'.
--     payload          — jsonb blob of whatever that action needs (challenge_id,
--                         display names, etc.) — a snapshot taken at enqueue time
--                         so the executing process never has to re-derive state.
--     status           — pending -> claimed -> completed | failed. Claimed via
--                         FOR UPDATE SKIP LOCKED so two processes racing on the
--                         same tick can't both execute the same row.
--     expires_at        — 2-hour default expiry so an orphaned row (bot kicked
--                         from target_guild_id, clone deactivated, no process
--                         ever holds that guild) eventually gets marked failed
--                         instead of polling forever.

CREATE TABLE IF NOT EXISTS discord_roast_arena_outbox (
    id SERIAL PRIMARY KEY,
    target_guild_id BIGINT NOT NULL,
    action_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'pending', -- pending -> claimed -> completed | failed
    result JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '2 hours'),
    completed_at TIMESTAMPTZ
);

-- The poller's claim query filters on exactly this shape: pending rows for a
-- specific set of guild ids.
CREATE INDEX IF NOT EXISTS discord_roast_arena_outbox_pending_idx
    ON discord_roast_arena_outbox (target_guild_id, status)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS discord_roast_arena_outbox_status_idx
    ON discord_roast_arena_outbox (status, expires_at);
