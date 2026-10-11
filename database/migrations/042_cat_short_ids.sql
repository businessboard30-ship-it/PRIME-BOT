-- Cat trade IDs become short (6 digits + a check letter, like 142574M) instead of 7 words. Additive, idempotent.
-- Existing 7-word IDs are KEPT and still work when typed (nobody's old ID breaks); everyone gets a short ID the next
-- time they open My cats. New owners only get a short ID, so trade_id can now be empty.
ALTER TABLE cat_owner_ids ADD COLUMN IF NOT EXISTS short_id TEXT;
ALTER TABLE cat_owner_ids ALTER COLUMN trade_id DROP NOT NULL;
-- One person per short ID across the main bot and every clone (rows without one don't count).
CREATE UNIQUE INDEX IF NOT EXISTS ux_cat_owner_ids_short ON cat_owner_ids (short_id) WHERE short_id IS NOT NULL;
