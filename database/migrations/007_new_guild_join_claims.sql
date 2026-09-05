-- Fixes owner-join-DM (and anything else gated by "first time this
-- process has seen this guild join") firing twice when two bot
-- processes are briefly connected to Discord with the same token at
-- once (e.g. a Railway redeploy where the old container hasn't fully
-- exited before the new one starts). The previous guard,
-- PrimeBot._new_guild_seen in discord_bot/bot.py, was an in-memory
-- Python set — it only protects against a same-process race (two
-- on_ready/on_guild_join firings inside one running bot), not against
-- two *separate* processes each running their own independent copy of
-- that set. This table makes the "have I already claimed this join"
-- check atomic and visible across every process sharing the database,
-- via INSERT ... ON CONFLICT DO NOTHING — whichever process's insert
-- actually lands is the only one that proceeds to send the join DM.

CREATE TABLE IF NOT EXISTS discord_new_guild_claims (
    guild_id BIGINT NOT NULL,
    clone_id INTEGER,
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS discord_new_guild_claims_guild_clone_key
    ON discord_new_guild_claims (guild_id, (COALESCE(clone_id, -1)));
