-- Cat ranks + trade IDs that are unique across the main bot and every clone. Additive, idempotent.
--
-- 1) Ranks: coins can be spent to rank a cat up (0 = where every cat starts, 5 = the top). Every existing cat gets
--    rank 0, so old and new cats are treated the same. The rank stays with the cat when it is traded or sold.
ALTER TABLE cat_adoptions ADD COLUMN IF NOT EXISTS cat_rank SMALLINT NOT NULL DEFAULT 0 CHECK (cat_rank BETWEEN 0 AND 5);

-- 2) Trade IDs (the 7-word names) used to be unique per bot only. Trading by ID now looks across every bot, so an ID
--    must belong to exactly one person everywhere. Every ID that exists today is kept; if the same ID was ever held
--    by two different people on two bots (practically impossible, but possible) the earliest keeps it and the other
--    simply gets a fresh ID the next time they open My cats.
DELETE FROM cat_owner_ids a
 USING cat_owner_ids b
 WHERE a.trade_id = b.trade_id
   AND (a.created_at, a.owner_id, COALESCE(a.clone_id, -1)) > (b.created_at, b.owner_id, COALESCE(b.clone_id, -1));
CREATE UNIQUE INDEX IF NOT EXISTS ux_cat_owner_ids_trade_global ON cat_owner_ids (trade_id);
