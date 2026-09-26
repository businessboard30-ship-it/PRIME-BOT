-- Reference copy of the schema database.py's _create_tables() creates
-- automatically on cold start (see database/migrations/019_godhood.sql, the
-- actual file that gets executed). You do not need to run this by hand
-- against a normal deployment.

-- path: database/migrations/019_godhood.sql

-- ============================================================================
-- Migration 019: Godhood trials (5-god ascension gauntlet)
-- ============================================================================
-- On hitting level 10, 11, 20, 21, 30, or 31 (modules/godhood_cards.py's
-- GODHOOD_TRIGGER_LEVELS / GODHOOD_TRIGGER_TIERS), a member has a
-- GODHOOD_CHOSEN_CHANCE_PERCENT (20%) independent chance per level of being
-- "chosen" by one of the 5 gods (VORATH, AZRAK, ASAFRAT, MORLAI, NEMESIS —
-- see modules/godhood_cards.py's GODHOOD_CARDS) and entering a 5-trial
-- gauntlet (modules/godhood_cards.py's GODHOOD_TRIALS), each trial under a
-- 3-day deadline (GODHOOD_TRIAL_DEADLINE_DAYS). Trial 1 is a combined
-- activity-volume check (messages + XP gained); trials 2-5 are deliberately
-- NOT tied to heists or the coin economy — they're pure, increasingly high
-- level-reached checkpoints (25 -> 40 -> 60 -> 100) on the same
-- discord_xp.total_xp pool leveling.py already tracks.
--
-- Failing a trial's deadline does NOT re-roll at the next trigger level in
-- the same tier (failing at level 10 does not retry at 11) — the member is
-- blocked from a re-roll until they reach the NEXT tier's pair of levels
-- (10/11 -> 20/21 -> 30/31), see modules/godhood_cards.py's
-- eligible_for_reroll(). Trial 5 ends in becoming a clan chief (reusing
-- modules/clan_cards.py's is_chief crown badge), but the actual end goal is
-- the permanent slot in discord_godhood_hall_of_fame — a global top-5 board,
-- capped at 5 rows by the app layer (oldest bumped on a 6th completion).
--
-- Required by:
--   database.py -> get_or_assign_godhood_trial() / record_godhood_completion()
--   discord_bot/cogs/leveling.py -> on_message (godhood chosen-roll + progress hooks)
--   modules/godhood_cards.py -> GODHOOD_CARDS / GODHOOD_TRIALS (the roster + ladder)
-- ============================================================================

CREATE TABLE IF NOT EXISTS discord_godhood_trials (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    clone_id INTEGER,
    user_id BIGINT NOT NULL,
    god_filename TEXT NOT NULL,           -- one of modules/godhood_cards.py's GODHOOD_CARD_FILENAMES
    chosen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    chosen_at_level INTEGER NOT NULL,     -- 10, 11, 20, 21, 30, or 31 — which trigger level fired the roll
    chosen_at_tier INTEGER NOT NULL,      -- 0 = 10/11, 1 = 20/21, 2 = 30/31 (index into GODHOOD_TRIGGER_TIERS)
    current_trial_no INTEGER NOT NULL DEFAULT 0,  -- 0 = chosen but trial 1 not yet started
    trial_target_type TEXT,               -- 'activity_combined' (trial 1) or 'level_reached' (trials 2-5) — set from GODHOOD_TRIALS when a trial starts
    trial_target_amount BIGINT,
    trial_progress_amount BIGINT NOT NULL DEFAULT 0,
    trial_started_at TIMESTAMPTZ,
    trial_deadline_at TIMESTAMPTZ,         -- trial_started_at + 3 days (GODHOOD_TRIAL_DEADLINE_DAYS)
    failed_at TIMESTAMPTZ,                 -- set if a deadline is missed; blocks reroll until the next tier
    completed_at TIMESTAMPTZ,              -- set when trial 5 clears (full gauntlet done)
    is_chief BOOLEAN NOT NULL DEFAULT FALSE  -- mirrors clan_cards.py's is_chief badge once trial 5 clears
);

-- Not a hard uniqueness constraint on (guild_id, user_id) alone — a member
-- can accumulate multiple rows over time (one per tier they were chosen at,
-- including failed ones from earlier tiers). get_or_assign_godhood_trial()
-- always looks at the member's MOST RECENT row (ORDER BY chosen_at DESC) to
-- decide whether a new roll is allowed, gated on tier via
-- modules/godhood_cards.py's eligible_for_reroll(), not just on failed_at
-- being set.
CREATE INDEX IF NOT EXISTS idx_discord_godhood_trials_user
    ON discord_godhood_trials (guild_id, clone_id, user_id, chosen_at DESC);

-- Fast "does this member have a currently-active gauntlet" lookup, used by
-- the message/voice XP hook (both trial types read off the same
-- discord_xp.total_xp progression) before it bothers computing anything.
CREATE INDEX IF NOT EXISTS idx_discord_godhood_trials_active
    ON discord_godhood_trials (guild_id, clone_id, user_id)
    WHERE failed_at IS NULL AND completed_at IS NULL;

-- Permanent record once a member clears all 5 trials. This table IS the
-- actual goal ("enter top five") — capped at 5 rows per (guild, clone) by
-- the app layer (record_godhood_completion() bumps the oldest completion
-- when a 6th member completes the gauntlet), not by a DB constraint, same
-- as discord_clan_chiefs' 5-seat cap is app-enforced (018_clan_chiefs.sql).
CREATE TABLE IF NOT EXISTS discord_godhood_hall_of_fame (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    clone_id INTEGER,
    user_id BIGINT NOT NULL,
    god_filename TEXT NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_discord_godhood_hof_guild
    ON discord_godhood_hall_of_fame (guild_id, clone_id, completed_at ASC);

-- ============================================================================
-- Migration Complete — this file is auto-applied on bot startup by
-- database.py's init routine (same pattern as 001-018 in this folder).
-- Remember: SCHEMA_VERSION in database.py must be bumped in the same change
-- that wires this file into the startup runner, or it silently never runs
-- (see the comment block above SCHEMA_VERSION in that file).
-- ============================================================================
