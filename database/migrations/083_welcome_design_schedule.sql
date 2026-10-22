-- Welcome Card Studio Phase 7a: scheduled designs (a saved design can switch on for a date range, then the live look returns).
-- Additive and idempotent. Dates are UTC. For a yearly schedule only the month and day are used (the stored year is ignored).
-- design_schedule_on is a cheap flag on the config row so the join render does not touch the designs table for servers
-- that never scheduled anything (it is kept in step by the db methods, never by hand).
ALTER TABLE discord_welcome_designs ADD COLUMN IF NOT EXISTS sched_from   DATE;
ALTER TABLE discord_welcome_designs ADD COLUMN IF NOT EXISTS sched_to     DATE;
ALTER TABLE discord_welcome_designs ADD COLUMN IF NOT EXISTS sched_yearly BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE discord_welcome_config  ADD COLUMN IF NOT EXISTS design_schedule_on BOOLEAN NOT NULL DEFAULT FALSE;
