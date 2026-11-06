-- path: database/migrations/098_premium_trial.sql

-- Premium free trial (one per server, ever): the trial is a normal Premium row that is flagged is_trial, so every
-- existing Premium check works untouched. Paying (any plan) clears the flag and starts the paid time from NOW, so a
-- trial's leftover days never stack on top of paid days. Additive / idempotent.
ALTER TABLE discord_guild_subscriptions ADD COLUMN IF NOT EXISTS is_trial BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE discord_guild_subscriptions ADD COLUMN IF NOT EXISTS trial_started_at TIMESTAMPTZ;
