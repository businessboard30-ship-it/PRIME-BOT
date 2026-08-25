-- FULL PATH: PRIME-BOT-main/database/migrations/006_roast_arena_mirror_panels.sql

-- path: database/migrations/006_roast_arena_mirror_panels.sql
--
-- Fixes the roast arena posting only its live vote panel in ONE guild (the
-- single shared "battleground" resolved by _resolve_battleground). Members
-- of the other contesting server have no Discord permission to see that
-- guild's channel at all, so they could never watch the countdown or vote —
-- only members of whichever guild happens to host the battleground could.
--
-- The fix (see discord_bot/cogs/roast_arena.py on_member_accept / _edit_panel
-- / _announce_winner) is to ALSO mirror the same live panel into a channel in
-- each of the two contesting guilds. The vote buttons are keyed by
-- challenge_id, not by message/channel (see _views_roast_arena_challenge.py),
-- so a vote cast from a mirrored panel in either guild lands in the same
-- discord_roast_arena_votes rows as one cast in the shared battleground —
-- no vote-counting changes needed, just more places the same panel lives.
--
-- Two new nullable BIGINT columns per side, mirroring the existing
-- battleground_channel_id / panel_message_id pair so all three locations
-- (battleground + challenger mirror + challenged mirror) are tracked the
-- same way and can each be independently missing (e.g. the bot has no
-- postable channel in one of the two guilds).

ALTER TABLE discord_roast_arena_challenges
    ADD COLUMN IF NOT EXISTS challenger_panel_channel_id BIGINT,
    ADD COLUMN IF NOT EXISTS challenger_panel_message_id BIGINT,
    ADD COLUMN IF NOT EXISTS challenged_panel_channel_id BIGINT,
    ADD COLUMN IF NOT EXISTS challenged_panel_message_id BIGINT;
