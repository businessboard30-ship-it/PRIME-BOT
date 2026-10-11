-- Cat wallets become ONE per person for every bot (main bot + all clones). Each person's per-bot balances are summed
-- into their main (clone_id IS NULL) wallet and the per-bot rows are zeroed (kept, never deleted). Idempotent: running
-- it again changes nothing, because the zeroed rows add nothing.
INSERT INTO cat_wallets (owner_id, clone_id, coins, diamonds)
SELECT owner_id, NULL, SUM(coins), SUM(diamonds) FROM cat_wallets GROUP BY owner_id
ON CONFLICT (owner_id, (COALESCE(clone_id, -1))) DO UPDATE SET
    coins = EXCLUDED.coins, diamonds = EXCLUDED.diamonds, updated_at = NOW();
UPDATE cat_wallets SET coins = 0, diamonds = 0, updated_at = NOW() WHERE clone_id IS NOT NULL AND (coins <> 0 OR diamonds <> 0);
