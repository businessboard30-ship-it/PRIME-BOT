-- path: database/migrations/004_roast_arena_host.sql
--
-- Adds the single shared battleground + apply-to-host flow on top of
-- 003_roast_arena.sql (see discord_bot/cogs/roast_arena.py and
-- discord_bot/cogs/_views_roast_arena_host_wizard.py). Servers no longer set
-- their own battleground channel directly — they apply, an owner/
-- DISCORD_CLONE_ADMIN_IDS admin approves, and that becomes the one shared
-- host for every battle across every clone. Same idempotent, additive-only
-- pattern as the other migrations.
--
-- Two tables:
--   discord_roast_arena_host          — singleton (id always 1) holding the
--                                        currently-approved host guild/channel.
--                                        No row yet = no host approved, the
--                                        cog falls back to OWNER_BROADCAST_CHANNEL_ID.
--   discord_roast_arena_host_requests — one row per apply click. A guild can
--                                        have at most one 'pending' row at a
--                                        time (partial unique index below);
--                                        re-applying upserts it instead of
--                                        stacking duplicates.

CREATE TABLE IF NOT EXISTS discord_roast_arena_host (
    id SMALLINT PRIMARY KEY DEFAULT 1,
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    approved_by_user_id BIGINT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT discord_roast_arena_host_singleton CHECK (id = 1)
);

CREATE TABLE IF NOT EXISTS discord_roast_arena_host_requests (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    applicant_user_id BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', -- pending -> approved | denied | superseded
    reviewed_by_user_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ
);

-- At most one live 'pending' application per guild — the apply button
-- upserts against this instead of creating duplicates on repeat clicks.
CREATE UNIQUE INDEX IF NOT EXISTS discord_roast_arena_host_requests_pending_guild_key
    ON discord_roast_arena_host_requests (guild_id)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS discord_roast_arena_host_requests_status_idx
    ON discord_roast_arena_host_requests (status);
