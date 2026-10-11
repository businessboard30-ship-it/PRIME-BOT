-- Cat pace: a server can ask for FEWER cats than its plan's daily amount (100 = the full amount, 50 = half, ...).
-- Additive, idempotent. Used by modules/cat_setup.py and the Cat setup wizard.
ALTER TABLE cat_guild_config ADD COLUMN IF NOT EXISTS pace_percent INTEGER NOT NULL DEFAULT 100;
